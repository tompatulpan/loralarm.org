"""Milesight WS101 smart button: the three real captured uplinks (fPort 85, profile
"Smart Button [Milesight]") through the field map, the fire-only `button` rule and
the Engine. Transparent: every press (short, long, double) raises one alarm,
nothing latches, nothing is ever cleared."""
from __future__ import annotations

import unittest
from datetime import timedelta
from pathlib import Path

from loralarm.alarms import AlarmKind
from loralarm.downlink import desired_frames
from loralarm.event import Uplink
from loralarm.frame import build_values
from loralarm.service import Engine

from .common import REG, T0, AlarmState, make_cfg, make_config
from .test_engine import RecordingNotifier

get_profile = REG.get_profile

LOG_DIR = Path(__file__).resolve().parent.parent.parent / "chirpstack" / "fixtures"
LOGS = {"double": "double press", "long": "long press", "short": "short press"}
BTN_EUI = "24e124535d243458"
PROFILE = "Smart Button [Milesight]"


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

    def test_every_press_notifies_and_nothing_latches(self):
        for i, status in enumerate(("short press", "long press", "double press")):
            self.engine.on_uplink(press(status, 10 * i))
            self.assertEqual(self.notifier.calls[-1],
                             (BTN_EUI, "button", True, f"knapptryck ({status})"))
            self.assertEqual(self.active(), set())       # fire-only: never latched
        self.engine.on_uplink(press("short press", 30))   # repeated press: fires again
        self.assertEqual(len(self.notifier.calls), 4)

    def test_battery_and_status_frames_are_quiet(self):
        self.engine.on_uplink(battery(90, 0))
        self.engine.on_uplink(battery(100, 10))
        self.assertEqual(self.notifier.calls, [])
        self.assertEqual(self.active(), set())

    def test_low_battery_alarms_once_and_presses_do_not_latch(self):
        self.engine.on_uplink(battery(80, 0))
        self.engine.on_uplink(battery(15, 10))
        self.engine.on_uplink(battery(14, 20))                 # still low: no repeat
        self.engine.on_uplink(press("short press", 30))        # a press frame has no battery
        self.assertEqual([(c[1], c[2]) for c in self.notifier.calls],
                         [("button_battery", True), ("button", True)])
        self.assertEqual(self.active(), {AlarmKind("button_battery")})
        self.engine.on_uplink(battery(100, 40))                # new battery
        self.assertEqual(self.notifier.calls[-1][1:3], ("button_battery", False))
        self.assertEqual(self.active(), set())

    def test_latched_snapshot_from_before_the_pulse_rule_is_dropped(self):
        conf = make_config(devices={}, defaults={"kind": "sensor"})
        state = AlarmState()
        state.restore({"active": ["button"]}, REG)
        engine = Engine(conf, notifier=self.notifier)
        engine.devices[BTN_EUI] = (conf.for_event(BTN_EUI, "Button 1", PROFILE), state)
        engine.on_uplink(press("double press", 0))              # fires, no orphan clear
        self.assertEqual([(c[1], c[2]) for c in self.notifier.calls], [("button", True)])
        self.assertEqual(engine.devices[BTN_EUI][1].active, set())

    def test_real_logs_in_sequence(self):
        # logged order: double, long, short -> one alarm per press
        for name in ("double", "long", "short"):
            self.engine.on_uplink(real(name))
        self.assertEqual([(c[1], c[2]) for c in self.notifier.calls],
                         [("button", True)] * 3)


if __name__ == "__main__":
    unittest.main()
