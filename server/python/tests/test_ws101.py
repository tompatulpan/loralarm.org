"""Milesight WS101 smart button: the three real captured uplinks (fPort 85, profile
"Smart Button [Milesight]") through the field map, the latching `button` rule and
the Engine. Short press = ON, long press = OFF, double press = not used."""
from __future__ import annotations

import unittest
from datetime import timedelta
from pathlib import Path

from loralarm.alarms import AlarmKind
from loralarm.downlink import desired_frames
from loralarm.event import Uplink
from loralarm.frame import build_values
from loralarm.service import Engine

from .common import REG, T0, make_cfg, make_config
from .test_engine import RecordingNotifier

get_profile = REG.get_profile

LOG_DIR = Path(__file__).resolve().parent.parent.parent / "chirpstack" / "fixtures"
LOGS = {"double": "double press", "long": "long press", "short": "short press"}
BTN_EUI = "24e124535d243458"
PROFILE = "Smart Button [Milesight]"
BUTTON = AlarmKind("button")


def real(name: str) -> Uplink:
    return Uplink.from_file(LOG_DIR / f"single-log(ws101 {name}).json")


def press(status: str, seconds: int = 0) -> Uplink:
    return Uplink(dev_eui=BTN_EUI, device_name="Button 1", application_name="Buttons",
                  device_profile_name=PROFILE, f_port=85, f_cnt=1,
                  time=T0 + timedelta(seconds=seconds), data_b64="",
                  obj={"button_event": {"status": status, "msgid": 123456}}, rssi=-61, snr=9.3)


def battery(pct: int, seconds: int = 0) -> Uplink:
    up = press("short press", seconds)
    up.obj = {"battery": pct}
    return up


class Ws101ProfileTest(unittest.TestCase):
    def test_real_logs_parse(self):
        for name, status in LOGS.items():
            up = real(name)
            self.assertEqual(up.obj["button_event"]["status"], status)
            self.assertEqual((up.f_port, up.device_profile_name, up.dev_eui),
                             (85, PROFILE, BTN_EUI))

    def test_profile_lookup_and_defaults(self):
        p = get_profile(PROFILE)
        self.assertEqual(p.name, PROFILE)
        self.assertEqual(get_profile("ws101"), p)
        self.assertTrue(p.is_data_port(85))
        self.assertFalse(p.is_data_port(2))
        dc = make_config(devices={}).for_event(BTN_EUI, "Button 1", PROFILE)
        self.assertEqual(dc.kind, "button")

    def test_fields(self):
        cfg = make_config(devices={}).for_event(BTN_EUI, "Button 1", PROFILE)
        values = build_values(get_profile(PROFILE).fields, real("short").obj, cfg, REG)
        self.assertEqual(values["button"], "short press")
        values = build_values(get_profile(PROFILE).fields, {"battery": 87}, cfg, REG)
        self.assertEqual(values["battery_pct"], 87)
        self.assertNotIn("button", values)

    def test_no_downlink_settings(self):
        self.assertEqual(desired_frames(make_cfg(family=PROFILE, kind="button", threshold=8)), [])


class Ws101EngineTest(unittest.TestCase):
    def setUp(self):
        self.notifier = RecordingNotifier()
        self.engine = Engine(make_config(devices={}, defaults={"kind": "sensor"}),
                             notifier=self.notifier)

    def active(self):
        return self.engine.devices[BTN_EUI][1].active

    def test_short_press_is_on_and_long_press_is_off(self):
        self.engine.on_uplink(press("short press", 0))
        self.assertEqual(self.notifier.calls,
                         [(BTN_EUI, "button", True, "knapp PÅ (kort tryck)")])
        self.assertEqual(self.active(), {BUTTON})
        self.engine.on_uplink(press("long press", 30))
        self.assertEqual(self.notifier.calls[-1], (BTN_EUI, "button", False, "knapp AV (långt tryck)"))
        self.assertEqual(self.active(), set())

    def test_on_stays_latched_through_double_press_and_battery_frames(self):
        self.engine.on_uplink(press("short press", 0))
        self.engine.on_uplink(press("double press", 10))
        self.engine.on_uplink(battery(90, 20))
        self.engine.on_uplink(press("short press", 30))       # already ON: no repeat
        self.assertEqual(len(self.notifier.calls), 1)
        self.assertEqual(self.active(), {BUTTON})

    def test_off_and_double_press_while_off_are_quiet(self):
        self.engine.on_uplink(press("long press", 0))
        self.engine.on_uplink(press("double press", 10))
        self.engine.on_uplink(battery(90, 20))
        self.assertEqual(self.notifier.calls, [])
        self.assertEqual(self.active(), set())

    def test_real_logs_in_sequence(self):
        # logged order: double, long (both quiet), then short -> ON
        for name in ("double", "long", "short"):
            self.engine.on_uplink(real(name))
        self.assertEqual([(c[1], c[2]) for c in self.notifier.calls], [("button", True)])


if __name__ == "__main__":
    unittest.main()
