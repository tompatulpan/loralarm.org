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


class AlarmAckTest(unittest.TestCase):
    """The PWA "sweep left to delete" backing store: per (user, device, kind),
    everything up to the newest event ts is acknowledged; a newer event
    (a later fire/clear) makes the badge show again."""

    def test_ack_stamps_the_newest_event(self):
        log = EventLog(None)
        log.log(T0, DEV_EUI, "sensor3", "motion", True, "rörelse")
        log.log(T0 + timedelta(hours=1), DEV_EUI, "sensor3", "motion", True, "rörelse")
        acked = log.ack_alarm(1, DEV_EUI, "motion")
        self.assertEqual(acked, log.last_change(DEV_EUI, "motion"))
        self.assertEqual(log.alarms_acked(1), {(DEV_EUI, "motion"): acked})
        self.assertEqual(log.last_change(DEV_EUI, "temp"), "")   # nothing logged for it

        # a newer event arrives: last_change moves past the acknowledgment
        log.log(T0 + timedelta(hours=2), DEV_EUI, "sensor3", "motion", False)
        self.assertGreater(log.last_change(DEV_EUI, "motion"), acked)
        # re-acking covers it again and never moves backwards
        self.assertEqual(log.ack_alarm(1, DEV_EUI, "motion"),
                         log.last_change(DEV_EUI, "motion"))

    def test_ack_is_per_user_and_per_kind(self):
        log = EventLog(None)
        log.log(T0, DEV_EUI, "sensor3", "motion", True, "rörelse")
        log.ack_alarm(1, DEV_EUI, "motion")
        self.assertEqual(log.alarms_acked(2), {})    # the other user still sees the badge
        log.ack_alarm(1, DEV_EUI, "temp")
        acked = log.alarms_acked(1)
        self.assertEqual(set(acked), {(DEV_EUI, "motion"), (DEV_EUI, "temp")})

    def test_ack_without_events_stamps_now(self):
        log = EventLog(None)
        ts = log.ack_alarm(1, DEV_EUI, "motion")
        self.assertGreaterEqual(ts, EventLog._utc(utc_now() - timedelta(seconds=5)))
        self.assertEqual(log.last_change(DEV_EUI, "motion"), "")


if __name__ == "__main__":
    unittest.main()
