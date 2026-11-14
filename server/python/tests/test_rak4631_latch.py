"""RAK4631 (Android firmware ver32): status bit3 = low-battery latch, decoded by the codec as
`low_battery_shutdown`. The `battery_shutdown` rule (defined in families/rak3172.json) reads
that field for every device kind "sensor", so a RAK4631 needs no rule of its own."""
from __future__ import annotations

import unittest
from datetime import timedelta

from loralarm.alarms import AlarmKind
from loralarm.service import Engine

from .common import DEV_EUI, T0, make_config, make_uplink, sensor_obj
from .test_engine import RecordingNotifier

KIND = AlarmKind("battery_shutdown")


def rak4631(seconds: int = 0, **overrides):
    return make_uplink(dev_eui=DEV_EUI, device_name="sensor3", profile="RAK4631",
                       obj=sensor_obj(**overrides), time=T0 + timedelta(seconds=seconds))


class BatteryLatchTest(unittest.TestCase):
    def setUp(self):
        self.notifier = RecordingNotifier()
        self.engine = Engine(make_config(devices={DEV_EUI: {"name": "sensor3", "family": "RAK4631"}}),
                             notifier=self.notifier)

    def kinds(self):
        return [(c[1], c[2]) for c in self.notifier.calls]

    def test_latch_fires_once_and_clears(self):
        self.engine.on_uplink(rak4631(0, battery_voltage=3.9, low_battery_shutdown=0))
        self.assertEqual(self.notifier.calls, [])
        self.engine.on_uplink(rak4631(300, battery_voltage=2.8, low_battery_shutdown=1))
        self.engine.on_uplink(rak4631(600, battery_voltage=2.8, low_battery_shutdown=1))
        self.assertEqual(self.kinds().count(("battery_shutdown", True)), 1)   # one alarm, not per frame
        _, state = self.engine.devices[DEV_EUI]
        self.assertIn(KIND, state.active)
        self.engine.on_uplink(rak4631(900, battery_voltage=3.3, low_battery_shutdown=0))
        self.assertNotIn(KIND, state.active)
        self.assertIn(("battery_shutdown", False), self.kinds())

    def test_frames_without_the_field_never_fire_it(self):
        # old firmware / codec before ver32 carries no such field
        self.engine.on_uplink(rak4631(0, battery_voltage=3.9))
        self.assertNotIn("battery_shutdown", [k for k, _ in self.kinds()])


if __name__ == "__main__":
    unittest.main()
