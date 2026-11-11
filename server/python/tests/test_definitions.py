"""Data-driven definitions: built-in families/*.json and the "profiles" section
of devices.json (declarative profile + rules, hot reload, bad input)."""
from __future__ import annotations

import unittest

from loralarm.alarms import AlarmKind
from loralarm.config import Config
from loralarm.definitions import FAMILIES_DIR, builtin_registry, rule_from_dict
from loralarm.downlink import parse_frequency
from loralarm.service import Engine

from .common import REG, AlarmState, evaluate, make_cfg, make_config, make_uplink, sensor_obj
from .test_engine import RecordingNotifier

get_profile = REG.get_profile
rule_for = REG.rule_for

NEW_EUI = "0102030405060708"

HUMIDITY_DEF = {
    "aliases": ["hum1"],
    "data_ports": [3],
    "fields": {"humidity": {"from": "RH"}, "battery_voltage": {"from": "VBAT", "type": "number"}},
    "settings": [{"key": "interval", "port": 10, "lo": 1, "hi": 60, "obj_field": "report_interval"}],
    "defaults": {"kind": "humidity", "batt_warn_v": 3.3, "ntfy_topic": "fukt"},
    "rules": [{"kind": "humidity", "label": "Fuktlarm", "field": "humidity", "when": "gt",
               "param": "humidity_max", "value": 80,
               "fire": "{value} % > {limit} %", "clear": "under {limit} %",
               "device_kinds": ["humidity"]}],
}


class BuiltinFamiliesTest(unittest.TestCase):
    def test_one_json_file_per_family(self):
        names = sorted(p.name for p in FAMILIES_DIR.glob("*.json"))
        self.assertEqual(names, ["browan_tbwl100.json", "dragino_lds02.json", "rak3172.json",
                                  "rak4631.json"])

    def test_builtin_profiles_loaded_from_json(self):
        rak = get_profile("rak4631")
        self.assertEqual((rak.name, rak.freq_label, rak.defaults["circuit_mode"]),
                         ("RAK4631", "code", "event"))
        self.assertEqual(parse_frequency(50, "RAK4631"), 79)
        self.assertEqual(parse_frequency(50, "RAK3172"), 4)
        self.assertEqual(get_profile("RUI3").name, "RAK3172")
        self.assertEqual(get_profile("lds02").data_ports, frozenset({10}))
        self.assertIsNotNone(rule_for("door"))


class UserProfilesTest(unittest.TestCase):
    def _config(self, profiles=None) -> Config:
        data = {"defaults": {}, "devices": {NEW_EUI: {"name": "fukt1", "family": "HUM-1",
                                                      "params": {"humidity_max": 70}}}}
        if profiles is not None:
            data["profiles"] = profiles
        return Config(data)

    def test_new_device_defined_only_in_json(self):
        conf = self._config({"HUM-1": HUMIDITY_DEF})
        cfg = conf.for_event(NEW_EUI, "fukt1", "HUM-1")
        self.assertEqual((cfg.kind, cfg.ntfy_topic, cfg.batt_warn_v), ("humidity", "fukt", 3.3))
        self.assertEqual(conf.registry.get_profile("hum1").name, "HUM-1")
        up = make_uplink(dev_eui=NEW_EUI, profile="HUM-1", f_port=3,
                         obj={"RH": 75, "VBAT": 3.6})
        _, fired = evaluate(cfg, up, conf.registry)
        self.assertEqual([(t.kind, t.detail) for t in fired], [("humidity", "75 % > 70 %")])
        _, quiet = evaluate(cfg, make_uplink(obj={"humidity": 60, "battery_voltage": 3.6}),
                            conf.registry)
        self.assertEqual(quiet, [])

    def test_profile_settings_and_clear_text(self):
        conf = self._config({"HUM-1": HUMIDITY_DEF})
        cfg = conf.for_event(NEW_EUI, "fukt1", "HUM-1")
        self.assertEqual(conf.registry.get_profile("HUM-1").setting("interval").port, 10)
        st = AlarmState()
        st.update(cfg, make_uplink(obj={"humidity": 75}), conf.registry)
        _, t = st.update(cfg, make_uplink(obj={"humidity": 60}), conf.registry)
        self.assertEqual([(x.kind, x.active, x.detail) for x in t], [("humidity", False, "under 70 %")])

    def test_each_config_has_its_own_registry(self):
        with_section = self._config({"HUM-1": HUMIDITY_DEF})
        self.assertEqual(with_section.registry.get_profile("HUM-1").name, "HUM-1")
        self.assertIsNotNone(with_section.registry.rule_for("humidity"))
        reloaded = self._config()                        # hot reload without "profiles"
        self.assertEqual(reloaded.registry.get_profile("HUM-1").name, "")
        self.assertIsNone(reloaded.registry.rule_for("humidity"))
        # nothing leaked into the shared built-in registry
        self.assertEqual(REG.get_profile("HUM-1").name, "")
        self.assertIsNone(REG.rule_for("humidity"))

    def test_user_definition_overrides_builtin_only_in_its_config(self):
        override = {"data_ports": [2, 10], "defaults": {"batt_warn_v": 2.0}, "settings": []}
        conf = self._config({"Dragino Switch LDS02": override})
        cfg = conf.for_event(NEW_EUI, "x", "Dragino Switch LDS02")
        self.assertEqual(conf.registry.get_profile("Dragino Switch LDS02").data_ports, frozenset({2, 10}))
        self.assertEqual(cfg.batt_warn_v, 2.0)
        self.assertEqual(REG.get_profile("Dragino Switch LDS02").data_ports, frozenset({10}))
        self.assertEqual(self._config().for_event(NEW_EUI, "x", "Dragino Switch LDS02").batt_warn_v, 2.5)

    def test_user_rule_replacing_builtin_rule_stays_in_its_config(self):
        door = {"rules": [{"kind": "door", "label": "Port", "field": "x", "when": "truthy"}]}
        conf = self._config({"Dragino Switch LDS02": door})
        self.assertEqual(conf.registry.rule_for("door").label, "Port")
        self.assertEqual(self._config().registry.rule_for("door").label, "Dörrlarm")
        self.assertEqual(REG.rule_for("door").label, "Dörrlarm")

    def test_bad_definitions_are_skipped_with_an_error(self):
        bad = {"A": {"typo_key": 1}, "B": {"rules": [{"kind": "k", "label": "L", "field": "f",
                                                      "when": "bigger"}]},
               "C": "not an object", "D": {"frequency": {"table": {"x": 1}}}}
        with self.assertLogs("loralarm.definitions", level="ERROR") as cm:
            conf = self._config({**bad, "HUM-1": HUMIDITY_DEF})
        self.assertEqual(len(cm.records), 4)
        self.assertEqual(conf.registry.get_profile("HUM-1").name, "HUM-1")   # the good one still applied
        self.assertEqual(conf.registry.get_profile("A").name, "")

    def test_profiles_section_of_wrong_type_is_ignored(self):
        with self.assertLogs("loralarm.definitions", level="ERROR"):
            self._config([1, 2])

    def test_engine_hot_reload_swaps_the_registry(self):
        engine = Engine(self._config({"HUM-1": HUMIDITY_DEF}))
        self.assertEqual(engine.registry.get_profile("HUM-1").name, "HUM-1")
        engine.config = self._config()                  # what a reload does
        self.assertEqual(engine.registry.get_profile("HUM-1").name, "")
        self.assertIs(engine.config.base_registry, builtin_registry())


class RuleOperatorsTest(unittest.TestCase):
    def _fires(self, when, field_value, value=None):
        d = {"kind": "t", "label": "T", "field": "f", "when": when}
        if value is not None:
            d["value"] = value
        return rule_from_dict(d).check(make_cfg(), None, {"f": field_value}) is not None

    def test_operators(self):
        self.assertTrue(self._fires("truthy", 1))
        self.assertFalse(self._fires("truthy", 0))
        self.assertTrue(self._fires("falsy", 0))
        self.assertTrue(self._fires("eq", "open", "open"))
        self.assertTrue(self._fires("ne", 1, 0))
        self.assertTrue(self._fires("gt", 5, 4))
        self.assertFalse(self._fires("gt", 4, 4))
        self.assertTrue(self._fires("ge", 4, 4))
        self.assertTrue(self._fires("lt", 3, 4))
        self.assertTrue(self._fires("le", 4, 4))

    def test_missing_or_non_numeric_never_fires(self):
        r = rule_from_dict({"kind": "t", "label": "T", "field": "f", "when": "gt", "value": 1})
        self.assertIsNone(r.check(make_cfg(), None, {}))
        self.assertIsNone(r.check(make_cfg(), None, {"f": None}))
        self.assertIsNone(r.check(make_cfg(), None, {"f": "high"}))

    def test_comparison_without_limit_is_rejected(self):
        with self.assertRaises(ValueError):
            rule_from_dict({"kind": "t", "label": "T", "field": "f", "when": "gt"})


class UnconfiguredDeviceTest(unittest.TestCase):
    def test_warns_once_for_a_device_missing_from_the_config(self):
        engine = Engine(make_config(), notifier=RecordingNotifier())
        stranger = make_uplink(dev_eui="1111111111111111", device_name="Door9")
        with self.assertLogs("loralarm.service", level="WARNING") as cm:
            engine.on_uplink(stranger)
            engine.on_uplink(stranger)
        self.assertEqual(len(cm.records), 1)
        self.assertIn("not in the config", cm.output[0])
        self.assertIn("pushover off", cm.output[0])

    def test_configured_device_is_silent(self):
        engine = Engine(make_config(), notifier=RecordingNotifier())
        with self.assertNoLogs("loralarm.service", level="WARNING"):
            engine.on_uplink(make_uplink(obj=sensor_obj()))


if __name__ == "__main__":
    unittest.main()
