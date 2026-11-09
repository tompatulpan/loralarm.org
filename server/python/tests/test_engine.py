"""Tests for the Engine: non-sensor frames, watchdog seeding, error isolation."""
from __future__ import annotations

import base64
import os
import sqlite3
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from loralarm.alarms import AlarmKind
from loralarm.config import Config
from loralarm.events import EventLog, utc_now
from loralarm.service import Engine

from .common import DEV_EUI, OTHER_EUI, T0, make_config, make_uplink, sensor_obj


class RecordingNotifier:
    def __init__(self):
        self.calls = []

    def notify(self, cfg, kind, active, detail):
        self.calls.append((cfg.dev_eui, kind, active, detail))


class NonSensorFrameTest(unittest.TestCase):
    def test_settings_frame_does_not_clear_alarms_but_updates_last_seen(self):
        engine = Engine(make_config())
        engine.on_uplink(make_uplink(obj=sensor_obj(motion_alert=1)))
        _, state = engine.devices[DEV_EUI]
        self.assertIn(AlarmKind.MOTION, state.active)

        later = T0 + timedelta(hours=3)
        engine.on_uplink(make_uplink(f_port=4, obj={}, time=later,
                                     data_b64=base64.b64encode(b"016").decode()))
        self.assertEqual(state.active, {AlarmKind.MOTION})   # nothing cleared
        self.assertEqual(state.last_seen, later)             # watchdog stays quiet

    def test_empty_object_frame_is_not_evaluated(self):
        engine = Engine(make_config())
        engine.on_uplink(make_uplink(obj=sensor_obj(motion_alert=1)))
        engine.on_uplink(make_uplink(obj={}))  # codec failure -> empty object
        _, state = engine.devices[DEV_EUI]
        self.assertEqual(state.active, {AlarmKind.MOTION})


class SeedTest(unittest.TestCase):
    def test_seed_creates_all_configured_devices(self):
        cfg = make_config(devices={DEV_EUI: {"name": "sensor3"},
                                   OTHER_EUI: {"name": "sensor16"}})
        engine = Engine(cfg)
        now = utc_now()
        engine.seed(now)
        self.assertEqual(set(engine.devices), {DEV_EUI, OTHER_EUI})
        for _cfg, state in engine.devices.values():
            self.assertEqual(state.last_seen, now)

    def test_seed_uses_event_history_when_no_snapshot(self):
        log = EventLog(None)
        old = utc_now() - timedelta(hours=30)
        log.log(old, DEV_EUI, "sensor3", "temp", True, "x")
        engine = Engine(make_config(), event_log=log)
        engine.seed()
        _, state = engine.devices[DEV_EUI]
        self.assertEqual(state.last_seen, old)  # dead sensor is visible immediately

    def test_seed_restores_persisted_active_alarms(self):
        log = EventLog(None)
        log.save_state(DEV_EUI, {"active": ["motion"], "last_seen": T0.isoformat(),
                                 "accel_baseline": {}, "last_detail": {}, "tilt_streak": 0})
        engine = Engine(make_config(), event_log=log)
        engine.seed()
        _, state = engine.devices[DEV_EUI]
        self.assertEqual(state.active, {AlarmKind.MOTION})

    def test_seeded_stale_device_alarms_on_first_tick(self):
        log = EventLog(None)
        log.log(utc_now() - timedelta(hours=48), DEV_EUI, "sensor3", "temp", True)
        engine = Engine(make_config(), event_log=log, notifier=RecordingNotifier())
        engine.seed()
        alarmed = engine.watchdog_tick()
        self.assertEqual(alarmed, [DEV_EUI])
        self.assertEqual(engine.notifier.calls[0][1], "stale")


class ErrorIsolationTest(unittest.TestCase):
    def test_db_failure_does_not_suppress_notification(self):
        engine = Engine(make_config(), event_log=EventLog(None),
                        notifier=RecordingNotifier())
        engine.event_log.log = lambda *a, **k: (_ for _ in ()).throw(sqlite3.Error("disk full"))
        engine.on_uplink(make_uplink(obj=sensor_obj(motion_alert=1)))
        self.assertEqual(engine.notifier.calls[0][1], "motion")  # notify still happened

    def test_notify_failure_does_not_break_uplink(self):
        class BadNotifier:
            def notify(self, *a):
                raise RuntimeError("queue exploded")
        engine = Engine(make_config(), notifier=BadNotifier())
        engine.on_uplink(make_uplink(obj=sensor_obj(motion_alert=1)))  # must not raise
        _, state = engine.devices[DEV_EUI]
        self.assertIn(AlarmKind.MOTION, state.active)


class ConfigReloadTest(unittest.TestCase):
    def test_config_reloaded_when_file_changes(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "devices.json"
            path.write_text('{"devices": {"%s": {"name": "sensor3", "ntfy_topic": "one"}}}' % DEV_EUI)
            engine = Engine(Config.load(path), config_path=path)
            engine.on_uplink(make_uplink(obj=sensor_obj()))
            self.assertEqual(engine.devices[DEV_EUI][0].ntfy_topic, "one")

            path.write_text('{"devices": {"%s": {"name": "sensor3", "ntfy_topic": "two"}}}' % DEV_EUI)
            os.utime(path, ns=(path.stat().st_atime_ns, path.stat().st_mtime_ns + 10**9))
            engine.on_uplink(make_uplink(obj=sensor_obj()))
            self.assertEqual(engine.devices[DEV_EUI][0].ntfy_topic, "two")


if __name__ == "__main__":
    unittest.main()
