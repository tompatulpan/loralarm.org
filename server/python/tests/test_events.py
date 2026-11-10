"""Tests for the SQLite event log: timestamp format, queries, state persistence."""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from loralarm.events import EventLog, utc_now

from .common import DEV_EUI, T0


class TimestampFormatTest(unittest.TestCase):
    def test_fixed_width_utc_with_microseconds(self):
        ts = EventLog._utc(T0)
        self.assertTrue(ts.endswith("+00:00"), ts)
        # always 6-digit microseconds -> text ordering is consistent
        self.assertRegex(ts, r"\.\d{6}\+00:00$")
        self.assertTrue(ts.startswith("2026-11-09T15:33:14.527766"))
        # and it round-trips through the table (wide window: T0 is a fixed past ts)
        log = EventLog(None)
        log.log(T0, DEV_EUI, "sensor3", "temp", True, "1.0 °C")
        self.assertEqual(log.recent(hours=24 * 3650)[0][0], ts)

    def test_naive_ts_treated_as_utc(self):
        log = EventLog(None)
        log.log(datetime.now(timezone.utc).replace(tzinfo=None), DEV_EUI, "sensor3", "temp", True)
        ts = log.recent(hours=24)[0][0]
        self.assertTrue(ts.endswith("+00:00"), ts)

    def test_recent_filters_by_window(self):
        log = EventLog(None)
        log.log(utc_now() - timedelta(hours=48), DEV_EUI, "sensor3", "temp", True)
        log.log(utc_now() - timedelta(hours=1), DEV_EUI, "sensor3", "temp", False)
        rows = log.recent(hours=24)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][3], 0)  # active flag stored as int


class StatePersistenceTest(unittest.TestCase):
    def test_save_load_roundtrip(self):
        log = EventLog(None)
        snap = {"active": ["motion"], "last_seen": T0.isoformat(),
                "accel_baseline": {"x": 0.1}, "last_detail": {}, "tilt_streak": 0}
        log.save_state(DEV_EUI, snap)
        self.assertEqual(log.load_state(DEV_EUI), snap)
        self.assertIsNone(log.load_state("unknown"))

    def test_last_seen_is_max_ts(self):
        log = EventLog(None)
        log.log(T0, DEV_EUI, "sensor3", "temp", True)
        log.log(T0 + timedelta(hours=2), DEV_EUI, "sensor3", "temp", False)
        self.assertEqual(log.last_seen(DEV_EUI), T0 + timedelta(hours=2))
        self.assertIsNone(log.last_seen("unknown"))


if __name__ == "__main__":
    unittest.main()
