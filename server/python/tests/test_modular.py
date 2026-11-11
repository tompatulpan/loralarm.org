"""Extension points for new devices/sensors: alarm rule registry, device
profile registry, device `kind` gating, config `params`, notification fallback.
Everything a third device needs must work without touching alarms/downlink code."""
from __future__ import annotations

import unittest

from loralarm.alarms import AlarmKind, AlarmRule
from loralarm.definitions import builtin_registry
from loralarm.downlink import desired_frames, parse_command_message, parse_frequency, wire_to_hz
from loralarm.notify import message_text
from loralarm.profiles import ACC_SETTINGS, DeviceProfile, SettingSpec

from .common import (DEV_EUI, REG, AlarmState, clear_detail, evaluate, make_cfg, make_config,
                     make_uplink, sensor_obj)

get_profile = REG.get_profile


def _leak_rule(**kw) -> AlarmRule:
    return AlarmRule(
        AlarmKind("leak"), "Vattenläcka",
        lambda cfg, r, obj: "vatten upptäckt" if obj.get("leak") else None,
        lambda cfg, r: "torrt", device_kinds=frozenset({"water"}), **kw)


class CustomRuleTest(unittest.TestCase):
    def setUp(self):
        # a private registry: custom rules never leak into other tests
        self.reg = builtin_registry().fork()
        self.reg.register_rule(_leak_rule(notify_fire="💧 {label}: {name} — {detail}",
                                          notify_clear="✔️ {name} torrt"))

    def test_registering_does_not_touch_the_builtin_registry(self):
        self.assertIsNone(REG.rule_for("leak"))
        self.assertIsNotNone(self.reg.rule_for("leak"))

    def test_rule_fires_only_for_its_device_kind(self):
        up = make_uplink(obj=sensor_obj(leak=1))
        _, water = evaluate(make_cfg(kind="water"), up, self.reg)
        _, sensor = evaluate(make_cfg(kind="sensor"), up, self.reg)
        self.assertEqual([t.kind for t in water], [AlarmKind("leak")])
        self.assertEqual(sensor, [])

    def test_fire_once_clear_once_and_label(self):
        cfg, st = make_cfg(kind="water"), AlarmState()
        _, t1 = st.update(cfg, make_uplink(obj=sensor_obj(leak=1)), self.reg)
        _, t2 = st.update(cfg, make_uplink(obj=sensor_obj(leak=1)), self.reg)
        _, t3 = st.update(cfg, make_uplink(obj=sensor_obj(leak=0)), self.reg)
        self.assertEqual([(t.kind, t.active, t.detail) for t in t1],
                         [("leak", True, "vatten upptäckt")])
        self.assertEqual(t2, [])
        self.assertEqual([(t.kind, t.active, t.detail) for t in t3], [("leak", False, "torrt")])
        self.assertEqual(self.reg.label_for("leak"), "Vattenläcka")

    def test_snapshot_round_trip_with_custom_kind(self):
        cfg, st = make_cfg(kind="water"), AlarmState()
        st.update(cfg, make_uplink(obj=sensor_obj(leak=1)), self.reg)
        restored = AlarmState()
        restored.restore(st.snapshot(), self.reg)
        self.assertEqual(restored.active, {AlarmKind("leak")})
        with self.assertRaises(ValueError):      # the built-in registry has no such rule
            AlarmState().restore(st.snapshot(), REG)

    def test_rule_texts_reach_the_notification(self):
        self.assertEqual(message_text("leak", "källare", True, "vatten upptäckt", registry=self.reg),
                         "💧 Vattenläcka: källare — vatten upptäckt")
        self.assertEqual(message_text("leak", "källare", False, "torrt", registry=self.reg),
                         "✔️ källare torrt")

    def test_rule_with_params(self):
        self.reg.register_rule(AlarmRule(
            AlarmKind("humidity"), "Fuktlarm",
            lambda cfg, r, obj: (f"{obj['humidity']} % > {cfg.params['humidity_max']} %"
                                 if obj.get("humidity", 0) > cfg.params.get("humidity_max", 100)
                                 else None)))
        cfg = make_cfg(params={"humidity_max": 80})
        _, fired = evaluate(cfg, make_uplink(obj=sensor_obj(humidity=91)), self.reg)
        self.assertEqual([(t.kind, t.detail) for t in fired], [("humidity", "91 % > 80 %")])
        self.assertEqual(clear_detail(AlarmKind("humidity"), cfg, evaluate(cfg, make_uplink(
            obj=sensor_obj()), self.reg)[0], self.reg), "kvitterad")


class GenericNotificationTest(unittest.TestCase):
    def test_unregistered_kind_gets_generic_text(self):
        self.assertEqual(message_text("whatever", "n", True, "d"), "n: whatever LARM — d")
        self.assertEqual(message_text("whatever", "n", False, "d"), "n: whatever kvitterad — d")


class DeviceKindTest(unittest.TestCase):
    def test_door_contact_skips_motion_and_tilt_keeps_circuit(self):
        cfg, st = make_cfg(kind="door", circuit_mode="level", motion_mode="level"), AlarmState()
        st.update(cfg, make_uplink(obj=sensor_obj()))
        _, t = st.update(cfg, make_uplink(obj=sensor_obj(
            motion_alert=1, motion_switch=1, accelerometer={"x": 5.0, "y": 5.0, "z": 5.0})))
        self.assertEqual([(x.kind, x.active) for x in t], [(AlarmKind.CIRCUIT, True)])

    def test_door_event_mode_motion_pulse_ignored(self):
        cfg, st = make_cfg(kind="door"), AlarmState()   # motion_mode defaults to event
        _, t = st.update(cfg, make_uplink(obj=sensor_obj(motion_alert=1)))
        self.assertEqual(t, [])

    def test_sensor_kind_unchanged(self):
        _, fired = evaluate(make_cfg(), make_uplink(obj=sensor_obj(motion_alert=1, motion_switch=1)))
        self.assertEqual({t.kind for t in fired}, {AlarmKind.MOTION, AlarmKind.CIRCUIT})

    def test_unknown_kind_warns_once_and_runs_universal_rules(self):
        cfg = make_cfg(kind="doro")
        with self.assertLogs("loralarm.alarms", level="WARNING") as cm:
            _, fired = evaluate(cfg, make_uplink(obj=sensor_obj(temperature=0.0)))
            evaluate(cfg, make_uplink(obj=sensor_obj()))
        self.assertEqual(len(cm.records), 1)
        self.assertEqual([t.kind for t in fired], [AlarmKind.TEMP])

    def test_door_kind_gets_no_auto_downlinks(self):
        cfg = make_cfg(threshold=8, frequency=10, duration=1)
        self.assertEqual(len(desired_frames(cfg)), 3)
        self.assertEqual(desired_frames(make_cfg(kind="door", threshold=8)), [])


class ProfileRegistryTest(unittest.TestCase):
    def test_builtin_lookup_is_case_insensitive_with_aliases(self):
        self.assertEqual(get_profile("rak4631").name, "RAK4631")
        self.assertEqual(get_profile("Android").name, "RAK4631")
        self.assertEqual(get_profile("RUI3").name, "RAK3172")
        self.assertEqual(get_profile("").name, "")
        self.assertEqual(get_profile("nope").name, "")

    def test_family_defaults_come_from_profiles(self):
        dc = make_config().for_event(DEV_EUI, "x", "RAK3172")
        self.assertEqual((dc.tk_correction_c, dc.batt_full_v, dc.circuit_mode), (0.0, 3.1, "level"))

    def test_new_profile_without_settings_and_own_defaults(self):
        reg = builtin_registry().fork()
        reg.register_profile(DeviceProfile(name="DOORX", aliases=("doorx",), settings=(),
                                           defaults=dict(kind="door", batt_full_v=3.0)))
        dc = make_config(registry=reg).for_event(DEV_EUI, "x", "DOORX")
        self.assertEqual((dc.kind, dc.batt_full_v), ("door", 3.0))
        self.assertEqual(desired_frames(make_cfg(family="DOORX", kind="sensor", threshold=5), reg), [])
        with self.assertRaises(ValueError):
            parse_command_message({"threshold": 5}, "DOORX", registry=reg)

    def test_new_profile_with_own_ports_frequency_and_readback(self):
        reg = builtin_registry().fork()
        reg.register_profile(DeviceProfile(
            name="NEWDEV",
            settings=(SettingSpec("interval", 10, 1, 60, "report_interval"),
                      SettingSpec("frequency", 11, obj_field="odr", kind="frequency")),
            freq_table={0: 1, 1: 10, 2: 50}, freq_label="step"))
        frames = parse_command_message({"interval": 15, "frequency": "hz:50"}, "NEWDEV", registry=reg)
        self.assertEqual([(f.f_port, f.data) for f in frames], [(10, b"015"), (11, b"002")])
        # desired values for profile-specific keys come from cfg.params
        cfg = make_cfg(family="NEWDEV", params={"interval": 30})
        self.assertEqual([(f.f_port, f.data) for f in desired_frames(cfg, reg)], [(10, b"030")])
        self.assertEqual(wire_to_hz(1, "NEWDEV", reg), 10.0)
        with self.assertRaises(ValueError):
            parse_frequency("hz:25", "NEWDEV", reg)      # not in this table
        with self.assertRaises(ValueError):
            parse_command_message({"interval": 99}, "NEWDEV", registry=reg)   # range from the spec
        # the shared built-in registry never saw the new profile
        self.assertEqual(REG.get_profile("NEWDEV").name, "")

    def test_existing_frequency_translation_matrix(self):
        for family, value, wire in [("RAK4631", 50, 79), ("RAK4631", "index:3", 63),
                                    ("RAK3172", 50, 4), ("RAK3172", "code:47", 2),
                                    ("RAK3172", 400, 7), ("RAK3172", 4, 4),
                                    ("RAK4631", 95, 95), ("", "code:31", 31), ("", 2, 2)]:
            self.assertEqual(parse_frequency(value, family), wire, (family, value))
        for family, value in [("RAK4631", 200), ("RAK4631", "index:7"), ("", 50), ("RAK3172", "hz:3")]:
            with self.assertRaises(ValueError, msg=(family, value)):
                parse_frequency(value, family)

    def test_acc_settings_are_the_generic_default(self):
        self.assertEqual(get_profile("unknown").settings, ACC_SETTINGS)


class ConfigParamsTest(unittest.TestCase):
    def test_params_merge_key_by_key_across_layers(self):
        cfg = make_config(
            defaults={"params": {"a": 1, "b": 2}},
            families={"RAK4631": {"params": {"b": 3}}},
            devices={DEV_EUI: {"params": {"c": 4}}}).for_event(DEV_EUI, "x", "RAK4631")
        self.assertEqual(cfg.params, {"a": 1, "b": 3, "c": 4})

    def test_params_wrong_type_ignored(self):
        with self.assertLogs("loralarm.config", level="WARNING"):
            cfg = make_config(devices={DEV_EUI: {"params": "nope"}}).for_event(DEV_EUI, "x", "")
        self.assertEqual(cfg.params, {})


if __name__ == "__main__":
    unittest.main()
