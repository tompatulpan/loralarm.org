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

    def notify(self, cfg, kind, active, detail, registry=None, event_id=None):
        self.calls.append((cfg.dev_eui, kind, active, detail))


class NonSensorFrameTest(unittest.TestCase):
    def test_settings_frame_does_not_clear_alarms_but_updates_last_seen(self):
        engine = Engine(make_config(defaults={"motion_mode": "level"}))
        engine.on_uplink(make_uplink(obj=sensor_obj(motion_alert=1)))
        _, state = engine.devices[DEV_EUI]
        self.assertIn(AlarmKind.MOTION, state.active)

        later = T0 + timedelta(hours=3)
        engine.on_uplink(make_uplink(f_port=4, obj={}, time=later,
                                     data_b64=base64.b64encode(b"016").decode()))
        self.assertEqual(state.active, {AlarmKind.MOTION})   # nothing cleared
        self.assertEqual(state.last_seen, later)             # watchdog stays quiet

    def test_empty_object_frame_is_not_evaluated(self):
        engine = Engine(make_config(defaults={"motion_mode": "level"}))
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

    def test_seed_includes_unconfigured_devices_from_saved_state(self):
        log = EventLog(None)
        log.save_state(OTHER_EUI, {"active": [], "last_seen": (utc_now() - timedelta(hours=30)).isoformat()})
        engine = Engine(make_config(), event_log=log, notifier=RecordingNotifier())
        engine.seed()
        self.assertIn(OTHER_EUI, engine.devices)
        self.assertEqual(engine.watchdog_tick(), [OTHER_EUI])

    def test_seed_restores_name_and_family_of_unconfigured_device(self):
        log = EventLog(None)
        engine = Engine(make_config(), event_log=log, notifier=RecordingNotifier())
        engine.on_uplink(make_uplink(dev_eui=OTHER_EUI, device_name="Door1", profile="RAK3172",
                                     obj=sensor_obj(), time=utc_now() - timedelta(hours=30)))
        restarted = Engine(make_config(), event_log=log, notifier=RecordingNotifier())
        restarted.seed()
        cfg, _ = restarted.devices[OTHER_EUI]
        self.assertEqual((cfg.name, cfg.family), ("Door1", "RAK3172"))
        self.assertEqual(restarted.watchdog_tick(), [OTHER_EUI])

    def test_seed_falls_back_to_name_from_event_history(self):
        log = EventLog(None)
        log.log(utc_now() - timedelta(hours=30), OTHER_EUI, "Door1", "stale", True, "x")
        log.save_state(OTHER_EUI, {"active": [], "last_seen": (utc_now() - timedelta(hours=30)).isoformat()})
        engine = Engine(make_config(), event_log=log)
        engine.seed()
        self.assertEqual(engine.devices[OTHER_EUI][0].name, "Door1")

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
            def notify(self, *a, **kw):
                raise RuntimeError("queue exploded")
        engine = Engine(make_config(), notifier=BadNotifier())
        engine.on_uplink(make_uplink(obj=sensor_obj(motion_alert=1)))  # must not raise
        _, state = engine.devices[DEV_EUI]
        self.assertEqual(len(state.motion_events), 1)  # detection was recorded (event mode)


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

    def _write(self, path: Path, text: str, bump_s: int) -> None:
        path.write_text(text)
        os.utime(path, ns=(path.stat().st_atime_ns, path.stat().st_mtime_ns + bump_s * 10**9))

    def test_broken_config_keeps_previous_config_and_still_processes_uplinks(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "devices.json"
            good = '{"devices": {"%s": {"name": "sensor3", "ntfy_topic": "one"}}}' % DEV_EUI
            path.write_text(good)
            notifier = RecordingNotifier()
            engine = Engine(Config.load(path), notifier=notifier, config_path=path)
            self._write(path, "{ broken", 1)
            with self.assertLogs("loralarm.service", level="ERROR"):
                engine.on_uplink(make_uplink(obj=sensor_obj(motion_alert=1)))
            self.assertEqual(len(notifier.calls), 1)                 # alarm still raised
            self.assertEqual(engine.devices[DEV_EUI][0].ntfy_topic, "one")
            # reported once, not on every uplink
            with self.assertNoLogs("loralarm.service", level="ERROR"):
                engine.on_uplink(make_uplink(obj=sensor_obj(motion_alert=1), f_cnt=2))
            # fixing the file recovers
            self._write(path, good.replace("one", "three"), 2)
            engine.on_uplink(make_uplink(obj=sensor_obj(), f_cnt=3))
            self.assertEqual(engine.devices[DEV_EUI][0].ntfy_topic, "three")

    def test_watchdog_tick_uses_reloaded_config(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "devices.json"
            path.write_text('{"devices": {"%s": {"name": "sensor3", "watchdog_hours": 24}}}' % DEV_EUI)
            log = EventLog(None)
            log.log(utc_now() - timedelta(hours=5), DEV_EUI, "sensor3", "temp", True)
            engine = Engine(Config.load(path), event_log=log, notifier=RecordingNotifier(),
                            config_path=path)
            engine.seed()
            self.assertEqual(engine.watchdog_tick(), [])
            self._write(path, '{"devices": {"%s": {"name": "sensor3", "watchdog_hours": 1}}}' % DEV_EUI, 1)
            self.assertEqual(engine.watchdog_tick(), [DEV_EUI])


class CorruptSnapshotTest(unittest.TestCase):
    def test_unknown_alarm_kind_in_snapshot_starts_fresh(self):
        log = EventLog(None)
        log.save_state(DEV_EUI, {"active": ["no_such_kind"], "last_seen": T0.isoformat()})
        engine = Engine(make_config(), event_log=log)
        with self.assertLogs("loralarm.service", level="ERROR"):
            engine.seed()
        _, state = engine.devices[DEV_EUI]
        self.assertEqual(state.active, set())
        self.assertIsNotNone(state.last_seen)


if __name__ == "__main__":
    unittest.main()
