"""Tests for ChirpStack uplink parsing, incl. the real captured logs."""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from loralarm.event import Uplink

from .common import DEV_EUI

LOG_DIR = Path(__file__).resolve().parent.parent.parent / "chirpstack" / "fixtures"


class FromJsonTest(unittest.TestCase):
    def test_missing_fport_defaults_to_zero(self):
        ev = {
            "time": "2026-11-09T15:33:14.527766598+00:00",
            "deviceInfo": {"devEui": "AC1F09FFFE1003AE", "deviceName": "sensor3",
                           "applicationName": "Larm", "deviceProfileName": "RAK4631"},
            "data": "Czk=",
            "object": {"temperature": 32},
        }
        up = Uplink.from_json(json.dumps(ev))
        self.assertEqual(up.f_port, 0)
        self.assertFalse(up.is_sensor_data)
        self.assertEqual(up.dev_eui, DEV_EUI)  # lowercased

    def test_settings_text_fports(self):
        import base64
        ev = {
            "time": "2026-11-09T15:33:14+00:00",
            "deviceInfo": {"devEui": DEV_EUI, "deviceName": "sensor3"},
            "fPort": 4, "data": base64.b64encode(b"016").decode(),
        }
        up = Uplink.from_json(ev)
        self.assertEqual(up.settings_text(), "016")
        up2 = Uplink.from_json({**ev, "fPort": 2})
        self.assertIsNone(up2.settings_text())

    def test_real_captured_alarm_log(self):
        up = Uplink.from_file(LOG_DIR / "single-log(rak4631 alarm).json")
        self.assertEqual(up.dev_eui, DEV_EUI)
        self.assertTrue(up.is_sensor_data)
        self.assertEqual(up.obj["motion_alert"], 1)
        self.assertEqual(up.rssi, -35)
        self.assertEqual(up.device_profile_name, "RAK4631")


if __name__ == "__main__":
    unittest.main()
