"""Browan TBWL100 water-leak sensor: real decoded uplinks (fPort 106, profile
"Browan Water sensor", captured from water2) through the profile field map, the
`water_leak` rule and the Engine."""
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

TBWL_EUI = "e8e1e100010a7100"
PROFILE = "Browan Water sensor"

DRY = {"humidityChanged": 0, "temperatureBoard": 23, "battery": 3.6, "humidity": 55,
       "leakInterrupt": 0, "temperature": 23, "status": 0, "temperatureChanged": 0}
WET = dict(DRY, leakInterrupt=1, status=1)


def tbwl(obj: dict, seconds: int = 0, f_port: int = 106, f_cnt: int = 1) -> Uplink:
    return Uplink(dev_eui=TBWL_EUI, device_name="water2", application_name="Larm",
                  device_profile_name=PROFILE, f_port=f_port, f_cnt=f_cnt,
                  time=T0 + timedelta(seconds=seconds), data_b64="", obj=obj, rssi=-46, snr=11.5)


class Tbwl100ProfileTest(unittest.TestCase):
    def test_profile_lookup_and_defaults(self):
        p = get_profile(PROFILE)
        self.assertEqual(p.name, PROFILE)
        self.assertEqual(get_profile("tbwl100"), p)
        self.assertTrue(p.is_data_port(106))
        self.assertFalse(p.is_data_port(2))
        dc = make_config(devices={}).for_event(TBWL_EUI, "water2", PROFILE)
        self.assertEqual((dc.kind, dc.batt_warn_v), ("water", 3.4))

    def test_fields(self):
        cfg = make_config(devices={}).for_event(TBWL_EUI, "water2", PROFILE)
        values = build_values(get_profile(PROFILE).fields, WET, cfg, REG)
        self.assertEqual((values["water_leak"], values["humidity"]), (1, 55))
        self.assertEqual((values["battery_voltage"], values["temperature"]), (3.6, 23))
        self.assertEqual(values["battery_status"], "ok")
        self.assertEqual(values["temperatureBoard"], 23)            # undeclared fields pass through

    def test_no_downlink_settings(self):
        self.assertEqual(desired_frames(make_cfg(family=PROFILE, kind="water", threshold=8)), [])


class Tbwl100EngineTest(unittest.TestCase):
    def setUp(self):
        self.notifier = RecordingNotifier()
        self.engine = Engine(make_config(devices={}, defaults={"kind": "sensor"}),
                             notifier=self.notifier)

    def test_leak_alarms_once_and_dry_clears(self):
        self.engine.on_uplink(tbwl(DRY, 0))
        self.engine.on_uplink(tbwl(WET, 80))
        self.engine.on_uplink(tbwl(WET, 90))            # sensor re-sends while wet
        self.assertEqual(self.notifier.calls, [(TBWL_EUI, "water_leak", True, "vatten upptäckt")])
        _, state = self.engine.devices[TBWL_EUI]
        self.assertEqual(state.active, {AlarmKind("water_leak")})
        self.engine.on_uplink(tbwl(DRY, 400))
        self.assertEqual(self.notifier.calls[-1], (TBWL_EUI, "water_leak", False, "torrt"))
        self.assertEqual(state.active, set())

    def test_battery_uses_family_limits(self):
        self.engine.on_uplink(tbwl(dict(DRY, battery=3.3)))
        self.assertEqual([c[1] for c in self.notifier.calls], ["battery_low"])

    def test_healthy_dry_sensor_is_quiet(self):
        self.engine.on_uplink(tbwl(DRY))
        self.assertEqual(self.notifier.calls, [])

    def test_other_ports_only_refresh_last_seen(self):
        self.engine.on_uplink(tbwl(WET, 0, f_port=204))
        self.assertEqual(self.notifier.calls, [])


if __name__ == "__main__":
    unittest.main()
