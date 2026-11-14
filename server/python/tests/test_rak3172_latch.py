"""RAK3172 firmware v32: motion frames carry battery voltage + the low-battery latch
flag (channel 13, `low_battery_shutdown`), and the family raises an alarm while it is set."""
from __future__ import annotations

import unittest
from datetime import timedelta

from loralarm.alarms import AlarmKind
from loralarm.service import Engine

from .common import T0, make_config, make_uplink
from .test_engine import RecordingNotifier

EUI = "ac1f09fffe12da63"
KIND = AlarmKind("battery_shutdown")


def rak3172(obj: dict, seconds: int = 0):
    return make_uplink(dev_eui=EUI, device_name="sensor16", profile="RAK3172",
                       obj=obj, time=T0 + timedelta(seconds=seconds))


class LatchFlagTest(unittest.TestCase):
    def setUp(self):
        self.notifier = RecordingNotifier()
        self.engine = Engine(make_config(devices={EUI: {"name": "sensor16", "family": "RAK3172"}}),
                             notifier=self.notifier)

    def test_latched_motion_frame_fires_and_next_heartbeat_clears(self):
        self.engine.on_uplink(rak3172({"battery_voltage": 3.0, "low_battery_shutdown": 0}))
        self.assertEqual(self.notifier.calls, [])
        self.engine.on_uplink(rak3172({"battery_voltage": 2.45, "low_battery_shutdown": 1,
                                       "motion_alert": 0}, 60))
        kinds = [(c[1], c[2]) for c in self.notifier.calls]
        self.assertIn(("battery_shutdown", True), kinds)
        self.assertIn(("battery_empty", True), kinds)       # 2.45 V < batt_empty_v 2.5
        _, state = self.engine.devices[EUI]
        self.assertIn(KIND, state.active)
        self.engine.on_uplink(rak3172({"battery_voltage": 2.9, "low_battery_shutdown": 0}, 120))
        self.assertNotIn(KIND, state.active)

    def test_healthy_cell_at_2v7_raises_nothing(self):
        # v30/v31 latched here (2.8 V cutoff); with 2.5 V it is only a warning level
        self.engine.on_uplink(rak3172({"battery_voltage": 2.7, "low_battery_shutdown": 0}))
        self.assertNotIn("battery_empty", [c[1] for c in self.notifier.calls])
        self.assertNotIn("battery_shutdown", [c[1] for c in self.notifier.calls])


if __name__ == "__main__":
    unittest.main()
