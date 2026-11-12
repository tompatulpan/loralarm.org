"""Arm/disarm (PWA): fan-out suppression only, snooze expiry in the watchdog
tick, TTL cache. Disarmed devices still evaluate and log every transition."""
from __future__ import annotations

import unittest
from datetime import timedelta

from loralarm.alarms import AlarmKind
from loralarm.events import EventLog, utc_now
from loralarm.service import ARMED_TTL_S, Engine

from .common import DEV_EUI, T0, make_config, make_uplink, sensor_obj


class RecordingNotifier:
    def __init__(self):
        self.calls = []

    def notify(self, cfg, kind, active, detail, registry=None, event_id=None):
        self.calls.append({"dev_eui": cfg.dev_eui, "kind": kind, "active": active,
                           "detail": detail, "event_id": event_id})


def make_engine(**cfg_kw):
    log = EventLog(None)
    notifier = RecordingNotifier()
    engine = Engine(make_config(defaults={"motion_mode": "level"}, **cfg_kw), log, notifier)
    return engine, log, notifier


class ArmedTest(unittest.TestCase):
    def test_disarmed_suppresses_notify_but_still_evaluates_and_logs(self):
        engine, log, notifier = make_engine()
        engine.on_uplink(make_uplink(obj=sensor_obj()))          # establish device
        notifier.calls.clear()
        engine.set_armed(DEV_EUI, False, user="tester")
        notifier.calls.clear()                                   # ignore the armed audit push

        engine.on_uplink(make_uplink(obj=sensor_obj(motion_alert=1), time=T0 + timedelta(seconds=10)))

        # alarm evaluated and kept ...
        _, state = engine.devices[DEV_EUI]
        self.assertIn(AlarmKind.MOTION, state.active)
        # ... event logged with the suppression marker ...
        motion_rows = log._conn.execute(
            "select detail from events where kind='motion'").fetchall()
        self.assertEqual(len(motion_rows), 1)
        self.assertIn("ej skickat: larmet är avstängt", motion_rows[0][0])
        # ... but nothing notified
        self.assertEqual([c for c in notifier.calls if c["kind"] == "motion"], [])

    def test_rearm_notifies_again_for_still_active_alarm(self):
        engine, log, notifier = make_engine()
        engine.on_uplink(make_uplink(obj=sensor_obj()))
        engine.set_armed(DEV_EUI, False, user="tester")
        engine.on_uplink(make_uplink(obj=sensor_obj(motion_alert=1), time=T0 + timedelta(seconds=10)))
        notifier.calls.clear()

        engine.set_armed(DEV_EUI, True, user="tester")

        kinds = [c["kind"] for c in notifier.calls]
        self.assertIn("armed", kinds)          # the re-arm notification
        self.assertIn("motion", kinds)         # still-active alarm re-notified
        audit = log._conn.execute(
            "select active, user from events where kind='armed'").fetchall()
        self.assertIn((1, "tester"), audit)    # armed=1 row, user recorded

    def test_snooze_expiry_in_watchdog_tick_rearms_and_notifies(self):
        engine, log, notifier = make_engine()
        engine.on_uplink(make_uplink(obj=sensor_obj()))
        engine.set_armed(DEV_EUI, False, until=T0 + timedelta(hours=1), user="tester")
        engine.on_uplink(make_uplink(obj=sensor_obj(motion_alert=1), time=T0 + timedelta(seconds=10)))
        notifier.calls.clear()

        # watchdog tick after the snooze expired — no web side involved
        import loralarm.service as svc
        real_utc = svc.utc_now
        svc.utc_now = lambda: T0 + timedelta(hours=1, seconds=1)
        try:
            engine.watchdog_tick()
        finally:
            svc.utc_now = real_utc

        self.assertTrue(engine.is_armed(DEV_EUI))
        kinds = [c["kind"] for c in notifier.calls]
        self.assertIn("armed", kinds)
        self.assertIn("motion", kinds)  # re-notified: condition still active
        # audit row: armed=1, user column = watchdog
        row = log._conn.execute(
            "select active, user from events where kind='armed' order by id desc limit 1"
        ).fetchone()
        self.assertEqual(row, (1, "watchdog"))

    def test_armed_cache_ttl_avoids_requery(self):
        engine, log, notifier = make_engine()
        engine.on_uplink(make_uplink(obj=sensor_obj()))
        engine.set_armed(DEV_EUI, False, user="tester")
        self.assertFalse(engine.is_armed(DEV_EUI))
        # change the row behind the engine's back: within the TTL the cache wins
        log.set_armed(DEV_EUI, True, None, "other")
        self.assertFalse(engine.is_armed(DEV_EUI))
        # after the TTL the DB is authoritative again
        engine._armed_cache[DEV_EUI] = (engine._armed_cache[DEV_EUI][0] - (ARMED_TTL_S + 1),
                                         False)
        self.assertTrue(engine.is_armed(DEV_EUI))

    def test_db_error_fails_safe_armed(self):
        engine, log, notifier = make_engine()
        engine.on_uplink(make_uplink(obj=sensor_obj()))
        log._conn.close()
        self.assertTrue(engine.is_armed(DEV_EUI))   # fail safe: armed

    def test_no_event_log_means_armed(self):
        engine = Engine(make_config(), None, RecordingNotifier())
        self.assertTrue(engine.is_armed(DEV_EUI))


if __name__ == "__main__":
    unittest.main()
