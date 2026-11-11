"""Dragino LDS02 door switch: real decoded uplinks (fPort 10, profile
"Dragino Switch LDS02") through the profile field map, the `door` rule and the Engine."""
from __future__ import annotations

import unittest
from datetime import timedelta

from loralarm.alarms import AlarmKind
from loralarm.downlink import desired_frames
from loralarm.event import Uplink
from loralarm.frame import build_values
from loralarm.service import Engine

from .common import REG, T0, make_cfg, make_config
from .test_engine import RecordingNotifier

get_profile = REG.get_profile

LDS02_EUI = "a84041a5d7598dd0"
PROFILE = "Dragino Switch LDS02"

# decoded objects from captured Door1 events (open at 20:42:02, closed at 20:42:06)
OPEN = {"DOOR_OPEN_STATUS": 1, "Node_type": "LDS02", "BAT_V": 2.952, "DOOR_OPEN_TIMES": 15119,
        "ALARM": 0, "LAST_DOOR_OPEN_DURATION": 2, "MOD": 1}
CLOSED = {"LAST_DOOR_OPEN_DURATION": 0, "ALARM": 0, "Node_type": "LDS02",
          "DOOR_OPEN_TIMES": 15119, "MOD": 1, "DOOR_OPEN_STATUS": 0, "BAT_V": 2.94}


def lds02(obj: dict, seconds: int = 0, f_port: int = 10, f_cnt: int = 1) -> Uplink:
    return Uplink(dev_eui=LDS02_EUI, device_name="Door1", application_name="Larm",
                  device_profile_name=PROFILE, f_port=f_port, f_cnt=f_cnt,
                  time=T0 + timedelta(seconds=seconds), data_b64="", obj=obj, rssi=-68, snr=9.0)


class Lds02ProfileTest(unittest.TestCase):
    def test_profile_lookup_and_defaults(self):
        p = get_profile(PROFILE)
        self.assertEqual(p.name, PROFILE)
        self.assertEqual(get_profile("lds02"), p)
        self.assertTrue(p.is_data_port(10))
        self.assertFalse(p.is_data_port(2))
        dc = make_config(devices={}).for_event(LDS02_EUI, "Door1", PROFILE)
        self.assertEqual((dc.kind, dc.batt_warn_v), ("door", 2.5))   # family layer beats defaults

    def test_fields_map_the_codec_names_and_keep_the_rest(self):
        cfg = make_config(devices={}).for_event(LDS02_EUI, "Door1", PROFILE)
        values = build_values(get_profile(PROFILE).fields, OPEN, cfg, REG)
        self.assertEqual((values["door_open"], values["battery_voltage"]), (1, 2.952))
        self.assertEqual(values["DOOR_OPEN_TIMES"], 15119)          # undeclared fields pass through
        self.assertEqual(values["battery_status"], "ok")            # standard derived field
        self.assertNotIn("temperature", values)                     # the device has none

    def test_no_downlink_settings(self):
        self.assertEqual(desired_frames(make_cfg(family=PROFILE, kind="door", threshold=8)), [])


class Lds02EngineTest(unittest.TestCase):
    def setUp(self):
        self.notifier = RecordingNotifier()
        self.engine = Engine(make_config(devices={}, defaults={"kind": "sensor"}),
                             notifier=self.notifier)

    def test_open_alarms_once_and_closed_clears(self):
        self.engine.on_uplink(lds02(CLOSED, 0))
        self.engine.on_uplink(lds02(OPEN, 10))
        self.engine.on_uplink(lds02(OPEN, 20))          # heartbeat while still open
        self.assertEqual(self.notifier.calls,
                         [(LDS02_EUI, "door", True, "dörren är öppen")])
        _, state = self.engine.devices[LDS02_EUI]
        self.assertEqual(state.active, {AlarmKind("door")})
        self.engine.on_uplink(lds02(CLOSED, 30))
        self.assertEqual(self.notifier.calls[-1], (LDS02_EUI, "door", False, "dörren är stängd"))
        self.assertEqual(state.active, set())

    def test_battery_uses_normalized_voltage(self):
        self.engine.on_uplink(lds02(dict(CLOSED, BAT_V=2.4)))
        self.assertEqual([c[1] for c in self.notifier.calls], ["battery_low"])

    def test_healthy_battery_and_closed_door_is_quiet(self):
        self.engine.on_uplink(lds02(CLOSED))
        self.assertEqual(self.notifier.calls, [])

    def test_other_ports_only_refresh_last_seen(self):
        self.engine.on_uplink(lds02(OPEN, 0, f_port=2))
        self.assertEqual(self.notifier.calls, [])


if __name__ == "__main__":
    unittest.main()
