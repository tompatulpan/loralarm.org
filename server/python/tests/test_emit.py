"""Phase 0 (PWA prerequisites): _emit logs before it notifies, and
Engine.snapshot() is a lock-protected in-memory copy for the web API."""
from __future__ import annotations

import sqlite3
import unittest

from loralarm.alarms import AlarmKind
from loralarm.events import EventLog
from loralarm.service import Engine

from .common import DEV_EUI, T0, make_config, make_uplink, sensor_obj


class RecordingNotifier:
    def __init__(self):
        self.calls = []

    def notify(self, cfg, kind, active, detail, registry=None, event_id=None):
        self.calls.append({"dev_eui": cfg.dev_eui, "kind": kind, "active": active,
                           "detail": detail, "event_id": event_id})


class EmitOrderTest(unittest.TestCase):
    def test_event_is_logged_before_notify_and_id_is_passed(self):
        log = EventLog(None)
        notifier = RecordingNotifier()
        engine = Engine(make_config(), log, notifier)

        seen_at_notify = {}

        original_notify = notifier.notify

        def checking_notify(cfg, kind, active, detail, registry=None, event_id=None):
            # at notify time the row must already exist, with exactly this id
            row = log._conn.execute(
                "select kind, active from events where id = ?", (event_id,)).fetchone()
            seen_at_notify["row"] = row
            original_notify(cfg, kind, active, detail, registry, event_id)

        notifier.notify = checking_notify
        engine.on_uplink(make_uplink(obj=sensor_obj(motion_alert=1)))

        self.assertEqual(len(notifier.calls), 1)
        call = notifier.calls[0]
        self.assertEqual(call["kind"], "motion")
        self.assertIsInstance(call["event_id"], int)
        self.assertEqual(seen_at_notify["row"], ("motion", 1))
        # and the row is the one in the log
        self.assertEqual(
            log._conn.execute("select count(*) from events").fetchone()[0], 1)

    def test_db_failure_still_notifies_with_event_id_none(self):
        log = EventLog(None)
        log._conn.close()  # every write now raises sqlite3.Error
        notifier = RecordingNotifier()
        engine = Engine(make_config(defaults={"motion_mode": "level"}), log, notifier)
        engine.on_uplink(make_uplink(obj=sensor_obj(motion_alert=1)))
        self.assertEqual(len(notifier.calls), 1)
        self.assertIsNone(notifier.calls[0]["event_id"])
        # the alarm state was still evaluated and kept
        _, state = engine.devices[DEV_EUI]
        self.assertIn(AlarmKind.MOTION, state.active)

    def test_log_event_returns_none_without_event_log(self):
        engine = Engine(make_config(), None, RecordingNotifier())
        self.assertIsNone(engine._log_event(T0, DEV_EUI, "sensor3", "motion", True, "x"))


class SnapshotTest(unittest.TestCase):
    def test_snapshot_is_a_copy_of_fleet_state(self):
        engine = Engine(make_config(defaults={"motion_mode": "level"}), EventLog(None))
        engine.on_uplink(make_uplink(obj=sensor_obj(motion_alert=1)))
        snap = engine.snapshot()
        self.assertIn(DEV_EUI, snap)
        entry = snap[DEV_EUI]
        self.assertEqual(entry["name"], "sensor3")
        self.assertEqual(entry["family"], "RAK4631")
        self.assertIn(AlarmKind.MOTION, entry["state"]["active"])
        # mutating the snapshot must not touch the engine
        entry["state"]["active"].clear()
        entry["name"] = "hacked"
        _, state = engine.devices[DEV_EUI]
        self.assertIn(AlarmKind.MOTION, state.active)
        self.assertEqual(engine.snapshot()[DEV_EUI]["name"], "sensor3")

    def test_snapshot_empty_engine(self):
        engine = Engine(make_config())
        self.assertEqual(engine.snapshot(), {})


if __name__ == "__main__":
    unittest.main()
