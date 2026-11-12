"""The files in examples/ are copy-and-edit starting points: they must load
without warnings and resolve to what their comments in the README promise."""
from __future__ import annotations

import os
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from loralarm.config import Config
from loralarm.downlink import desired_frames
from loralarm.service import Engine

from .common import REG, make_uplink, sensor_obj
from .test_engine import RecordingNotifier
from .test_lds02 import CLOSED, OPEN, lds02
from .test_tbwl100 import DRY, WET, tbwl
from .test_ws101 import BTN_EUI, PROFILE as WS101_PROFILE, press

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
ENV = {"LORALARM_PUSHOVER_USER": "u", "LORALARM_PUSHOVER_TOKEN": "t",
       "LORALARM_CS_APP_LARM": "app-larm", "LORALARM_CS_APP_SUBLARM": "app-sublarm"}


def load(name: str) -> Config:
    with mock.patch.dict(os.environ, ENV):
        return Config.load(EXAMPLES / name)


class ExamplesLoadTest(unittest.TestCase):
    def test_every_example_loads_without_warnings(self):
        files = sorted(EXAMPLES.glob("*.json"))
        self.assertTrue(files)
        for f in files:
            with self.subTest(example=f.name), self.assertNoLogs("loralarm", level="WARNING"):
                load(f.name)


class Rak3172ExampleTest(unittest.TestCase):
    def test_resolved_config_and_settings(self):
        conf = load("rak3172.example.json")
        cfg = conf.for_event("ac1f09fffe12da63", "", "RAK3172")
        self.assertEqual((cfg.name, cfg.application_id, cfg.pushover_user),
                         ("sensor16", "app-sublarm", "u"))
        self.assertEqual((cfg.tk_correction_c, cfg.temp_alarm_min_c, cfg.circuit_mode),
                         (0.0, 2.0, "level"))
        # 16 / 10 Hz / 1 -> RUI3 ODR *index* 2 on fPort 6
        self.assertEqual([(f.f_port, f.data) for f in desired_frames(cfg, conf.registry)],
                         [(5, b"016"), (6, b"002"), (7, b"001")])

    def test_frost_alarm_uses_the_uncorrected_temperature(self):
        notifier = RecordingNotifier()
        engine = Engine(load("rak3172.example.json"), notifier=notifier)
        engine.on_uplink(make_uplink(dev_eui="ac1f09fffe12da63", device_name="sensor16",
                                     profile="RAK3172", obj=sensor_obj(temperature=1.5)))
        self.assertEqual([(c[1], c[2]) for c in notifier.calls], [("temp_low", True)])


class Rak4631ExampleTest(unittest.TestCase):
    def test_resolved_config_and_settings(self):
        conf = load("rak4631.example.json")
        cfg = conf.for_event("ac1f09fffe1003ae", "", "RAK4631")
        self.assertEqual((cfg.application_id, cfg.tk_correction_c, cfg.circuit_mode,
                          cfg.motion_min_detections), ("app-larm", 8.0, "event", 3))
        # 8 / 10 Hz / 1 -> RAK4631 register *code* 47 on fPort 6
        self.assertEqual([(f.f_port, f.data) for f in desired_frames(cfg, conf.registry)],
                         [(5, b"008"), (6, b"047"), (7, b"001")])

    def test_custom_texts_and_three_detections_rule(self):
        notifier = RecordingNotifier()
        engine = Engine(load("rak4631.example.json"), notifier=notifier)
        for s in (0, 5):                           # two detections: below the limit of 3
            engine.on_uplink(make_uplink(obj=sensor_obj(motion_alert=1),
                                         time=make_uplink().time.replace(second=s)))
        self.assertEqual(notifier.calls, [])
        engine.on_uplink(make_uplink(obj=sensor_obj(motion_alert=1),
                                     time=make_uplink().time.replace(second=10)))
        self.assertEqual([(c[1], c[2]) for c in notifier.calls], [("motion", True)])


class Lds02ExampleTest(unittest.TestCase):
    EUI = "a84041a5d7598dd0"

    def test_resolved_config_has_no_settings_to_push(self):
        conf = load("dragino_lds02.example.json")
        cfg = conf.for_event(self.EUI, "", "Dragino Switch LDS02")
        self.assertEqual((cfg.name, cfg.kind, cfg.batt_warn_v), ("Door1", "door", 2.5))
        self.assertEqual(desired_frames(cfg, conf.registry), [])

    def test_open_and_close_use_the_custom_texts(self):
        notifier = RecordingNotifier()
        engine = Engine(load("dragino_lds02.example.json"), notifier=notifier)
        engine.on_uplink(lds02(CLOSED, 0))
        engine.on_uplink(lds02(OPEN, 10))
        engine.on_uplink(lds02(CLOSED, 20))
        self.assertEqual([(c[1], c[2]) for c in notifier.calls], [("door", True), ("door", False)])
        self.assertEqual(REG.get_profile("Dragino Switch LDS02").data_ports, frozenset({10}))


class SharedFamilySettingsTest(unittest.TestCase):
    """Settings live once under "families"; the second device lists only its own."""

    def frames(self, conf, eui, family):
        cfg = conf.for_event(eui, "", family)
        return cfg, [(f.f_port, f.data) for f in desired_frames(cfg, conf.registry)]

    def test_rak3172_second_device_inherits_and_overrides(self):
        conf = load("rak3172.example.json")
        first, first_frames = self.frames(conf, "ac1f09fffe12da63", "RAK3172")
        second, second_frames = self.frames(conf, "ac1f09fffe12da64", "RAK3172")
        self.assertEqual((first.name, second.name), ("sensor16", "sensor17"))
        self.assertEqual((first.temp_alarm_min_c, second.temp_alarm_min_c), (2.0, 5.0))
        self.assertEqual((first.watchdog_hours, second.watchdog_hours), (0.5, 24))
        self.assertEqual(second.batt_warn_v, 2.9)                     # shared, from the family
        self.assertEqual(first_frames, [(5, b"016"), (6, b"002"), (7, b"001")])   # 10 Hz = index 2
        self.assertEqual(second_frames, [(5, b"016"), (6, b"003"), (7, b"001")])  # 25 Hz = index 3

    def test_rak4631_second_device_inherits_and_overrides(self):
        conf = load("rak4631.example.json")
        first, first_frames = self.frames(conf, "ac1f09fffe1003ae", "RAK4631")
        second, second_frames = self.frames(conf, "ac1f09fffe1003af", "RAK4631")
        self.assertEqual((first.name, second.name), ("sensor3", "sensor4"))
        self.assertEqual((first.temp_alarm_min_c, second.temp_alarm_min_c), (2.0, -18.0))
        self.assertEqual((first.motion_min_detections, second.motion_min_detections), (3, 1))
        self.assertEqual((first.application_id, second.application_id), ("app-larm", "app-larm"))
        self.assertEqual(second.notify_fire, first.notify_fire)        # shared texts
        self.assertEqual(second_frames, first_frames)                  # same settings pushed

    def test_lds02_second_device_overrides_watchdog_and_has_a_night_window(self):
        conf = load("dragino_lds02.example.json")
        first, _ = self.frames(conf, "a84041a5d7598dd0", "Dragino Switch LDS02")
        second, second_frames = self.frames(conf, "a84041e3d189503d", "Dragino Switch LDS02")
        self.assertEqual((first.name, second.name), ("Door1", "Door2"))
        self.assertEqual((first.watchdog_hours, second.watchdog_hours), (12, 2))
        self.assertEqual((first.alarm_window, second.alarm_window), ("", "21:00-09:00"))
        self.assertEqual(second.timezone, "Europe/Stockholm")
        self.assertEqual((first.ntfy_topic, second.ntfy_topic), ("door1", "door2"))
        self.assertEqual(second.notify_clear, first.notify_clear)
        self.assertEqual(second_frames, [])

    def test_lds02_example_window_in_action(self):
        # 16:33 local: Door2 (window 21:00-09:00) stays quiet, Door1 (no window) alarms
        notifier = RecordingNotifier()
        engine = Engine(load("dragino_lds02.example.json"), notifier=notifier)
        for eui, name in (("a84041e3d189503d", "Door2"), ("a84041a5d7598dd0", "Door1")):
            for obj, seconds in ((CLOSED, 0), (OPEN, 60)):
                engine.on_uplink(replace(lds02(obj, seconds), dev_eui=eui, device_name=name))
        self.assertEqual([(c[0], c[1]) for c in notifier.calls], [("a84041a5d7598dd0", "door")])


class Tbwl100ExampleTest(unittest.TestCase):
    EUIS = ("e8e1e100010a7173", "e8e1e100010a7100")
    PROFILE = "Browan Water sensor"

    def test_resolved_config_has_no_settings_to_push(self):
        conf = load("browan_tbwl100.example.json")
        for eui in self.EUIS:
            cfg = conf.for_event(eui, "", self.PROFILE)
            self.assertEqual((cfg.kind, cfg.watchdog_hours, cfg.batt_warn_v), ("water", 3, 3.4))
            self.assertEqual(desired_frames(cfg, conf.registry), [])

    def test_leak_and_dry_notify_the_device_topic(self):
        notifier = RecordingNotifier()
        engine = Engine(load("browan_tbwl100.example.json"), notifier=notifier)
        engine.on_uplink(tbwl(DRY, 0))
        engine.on_uplink(tbwl(WET, 10))
        engine.on_uplink(tbwl(DRY, 20))
        self.assertEqual([(c[1], c[2]) for c in notifier.calls],
                         [("water_leak", True), ("water_leak", False)])


class Ws101ExampleTest(unittest.TestCase):
    def test_resolved_config_and_on_off_notify_the_device_topic(self):
        conf = load("milesight_ws101.example.json")
        cfg = conf.for_event(BTN_EUI, "", WS101_PROFILE)
        self.assertEqual((cfg.kind, cfg.ntfy_topic), ("button", "button1"))
        self.assertEqual(desired_frames(cfg, conf.registry), [])
        notifier = RecordingNotifier()
        engine = Engine(conf, notifier=notifier)
        engine.on_uplink(press("short press", 0))
        engine.on_uplink(press("long press", 10))
        self.assertEqual([(c[1], c[2]) for c in notifier.calls],
                         [("button", True), ("button", False)])


class FullTemplateTest(unittest.TestCase):
    """devices.example.json: every section of the config in one template."""
    TEMPLATE = Path(__file__).resolve().parent.parent / "devices.example.json"
    CO2 = "0a0b0c0d0e0f1011"

    def conf(self) -> Config:
        with mock.patch.dict(os.environ, ENV):
            return Config.load(self.TEMPLATE)

    def test_loads_without_warnings_and_has_every_section(self):
        import json
        with self.assertNoLogs("loralarm", level="WARNING"):
            self.conf()
        data = json.loads(self.TEMPLATE.read_text(encoding="utf-8"))
        self.assertEqual({"profiles", "defaults", "families", "devices"} <= set(data), True)
        self.assertEqual(len(data["devices"]), 7)

    def test_pushover_is_set_once_in_defaults_and_reaches_every_device(self):
        conf = self.conf()
        for eui, entry in conf.devices.items():
            self.assertNotIn("pushover_user", entry)
            cfg = conf.for_event(eui, "", entry["family"])
            self.assertEqual((cfg.pushover_user, cfg.pushover_token), ("u", "t"), cfg.name)

    def test_sensor3_keeps_the_settings_the_downlink_tests_rely_on(self):
        conf = self.conf()
        cfg = conf.for_event("ac1f09fffe1003ae", "", "RAK4631")
        self.assertEqual([(f.f_port, f.data) for f in desired_frames(cfg, conf.registry)],
                         [(5, b"008"), (6, b"047"), (7, b"001")])
        self.assertEqual((cfg.tk_correction_c, cfg.motion_min_detections, cfg.circuit_mode),
                         (8.0, 3, "event"))

    def test_family_layer_and_device_override(self):
        conf = self.conf()
        door1 = conf.for_event("a84041a5d7598dd0", "", "Dragino Switch LDS02")
        door2 = conf.for_event("a84041e3d189503d", "", "Dragino Switch LDS02")
        self.assertEqual((door1.watchdog_hours, door2.watchdog_hours), (12, 2))
        self.assertEqual((door1.alarm_window, door2.alarm_window), ("", "21:00-09:00"))
        self.assertEqual(door1.timezone, "Europe/Stockholm")      # set once in defaults
        self.assertEqual(door1.kind, "door")                     # the type's own default
        self.assertEqual(conf.for_event("ac1f09fffe12da63", "", "RAK3172").temp_alarm_min_c, 2.0)
        water = conf.for_event("e8e1e100010a7173", "", "Browan Water sensor")
        self.assertEqual((water.kind, water.watchdog_hours, water.batt_empty_v), ("water", 3, 3.1))

    def test_custom_profile_params_and_alarm_end_to_end(self):
        conf = self.conf()
        cfg = conf.for_event(self.CO2, "", "CO2-1")
        self.assertEqual((cfg.kind, cfg.params, cfg.watchdog_hours),
                         ("air", {"co2_scale": 2, "co2_max": 1000}, 2))
        self.assertEqual(conf.registry.get_profile("co2").name, "CO2-1")
        self.assertEqual(REG.get_profile("CO2-1").name, "")        # built-ins stay untouched
        notifier = RecordingNotifier()
        engine = Engine(conf, notifier=notifier)
        for raw in (400, 700, 300):                               # 800, 1400, 600 ppm
            engine.on_uplink(make_uplink(dev_eui=self.CO2, device_name="co2-hall",
                                         profile="CO2-1", f_port=8, obj={"CO2RAW": raw}))
        self.assertEqual([(c[1], c[2], c[3]) for c in notifier.calls],
                         [("co2", True, "1400 ppm > 1000 ppm"), ("co2", False, "under 1000 ppm")])


if __name__ == "__main__":
    unittest.main()
