"""Alarm window: notifications for windowed alarm kinds only inside the device's
daily window; the engine keeps evaluating and logging either way.

Reference times (UTC -> Stockholm, winter): T0 = 15:33 UTC = 16:33 local.
Window "21:00-09:00" is therefore closed at T0 and open at T0 + 5 h (21:33 local)."""
from __future__ import annotations

import unittest
from datetime import timedelta
from unittest import mock

from loralarm.alarms import AlarmKind
from loralarm.events import EventLog
from loralarm.service import Engine

from .common import DEV_EUI, REG, T0, AlarmState, make_config, make_uplink, sensor_obj
from .test_engine import RecordingNotifier
from .test_lds02 import CLOSED, LDS02_EUI, OPEN, PROFILE, lds02

NIGHT = "21:00-09:00"
OUTSIDE = 0                      # 16:33 local
INSIDE = 5 * 3600                # 21:33 local
CLOSING_OUTSIDE = 16 * 3600 + 57 * 60 + 0   # 08:30 UTC next day = 09:30 local, window just closed


def door_engine(window: str | None = NIGHT, **device) -> tuple[Engine, RecordingNotifier, EventLog]:
    entry = {"name": "Door1", "family": PROFILE, **device}
    if window is not None:
        entry["alarm_window"] = window
    notifier, log = RecordingNotifier(), EventLog(None)
    engine = Engine(make_config(devices={LDS02_EUI: entry}), event_log=log, notifier=notifier)
    return engine, notifier, log


def rows(log: EventLog) -> list[tuple]:
    with log._lock:
        return log._conn.execute("select kind, active, detail from events order by id").fetchall()


def sent(notifier: RecordingNotifier) -> list[tuple]:
    return [(c[1], c[2]) for c in notifier.calls]


class WindowTest(unittest.TestCase):
    def test_fire_inside_the_window_is_sent_and_logged(self):
        engine, notifier, log = door_engine()
        engine.on_uplink(lds02(CLOSED, INSIDE - 60))
        engine.on_uplink(lds02(OPEN, INSIDE))
        self.assertEqual(sent(notifier), [("door", True)])
        self.assertEqual(rows(log), [("door", 1, "dörren är öppen")])

    def test_fire_outside_is_dropped_but_logged_and_still_active(self):
        engine, notifier, log = door_engine()
        engine.on_uplink(lds02(CLOSED, OUTSIDE - 60))
        engine.on_uplink(lds02(OPEN, OUTSIDE))
        self.assertEqual(notifier.calls, [])
        [(kind, active, detail)] = rows(log)
        self.assertEqual((kind, active), ("door", 1))
        self.assertIn("utanför larmtid 21:00-09:00, ej skickat", detail)
        _, state = engine.devices[LDS02_EUI]
        self.assertEqual(state.active, {AlarmKind("door")})        # evaluation is unaffected
        self.assertEqual(state.suppressed, {AlarmKind("door")})

    def test_clear_of_a_suppressed_fire_is_not_sent(self):
        engine, notifier, log = door_engine()
        engine.on_uplink(lds02(CLOSED, OUTSIDE - 60))
        engine.on_uplink(lds02(OPEN, OUTSIDE))
        engine.on_uplink(lds02(CLOSED, OUTSIDE + 30))
        self.assertEqual(notifier.calls, [])
        self.assertEqual([(r[0], r[1]) for r in rows(log)], [("door", 1), ("door", 0)])
        self.assertIn("ej skickat", rows(log)[1][2])
        _, state = engine.devices[LDS02_EUI]
        self.assertEqual((state.active, state.suppressed), (set(), set()))

    def test_clear_of_a_sent_fire_is_sent_even_after_the_window_closed(self):
        engine, notifier, _ = door_engine()
        engine.on_uplink(lds02(CLOSED, 16 * 3600))                  # 08:33 local
        engine.on_uplink(lds02(OPEN, 16 * 3600 + 60))              # 08:34 local: inside
        engine.on_uplink(lds02(CLOSED, CLOSING_OUTSIDE + 3 * 60))  # 09:33 local: outside
        self.assertEqual(sent(notifier), [("door", True), ("door", False)])

    def test_fire_again_after_the_window_opens_is_sent(self):
        engine, notifier, _ = door_engine()
        engine.on_uplink(lds02(CLOSED, OUTSIDE - 60))
        engine.on_uplink(lds02(OPEN, OUTSIDE))
        engine.on_uplink(lds02(CLOSED, OUTSIDE + 30))
        engine.on_uplink(lds02(OPEN, INSIDE))
        self.assertEqual(sent(notifier), [("door", True)])

    def test_no_window_means_always(self):
        engine, notifier, _ = door_engine(window=None)
        engine.on_uplink(lds02(CLOSED, OUTSIDE - 60))
        engine.on_uplink(lds02(OPEN, OUTSIDE))
        self.assertEqual(sent(notifier), [("door", True)])

    def test_empty_device_value_overrides_a_window_from_defaults(self):
        cfg = make_config(devices={LDS02_EUI: {"name": "Door1", "family": PROFILE, "alarm_window": ""}},
                          defaults={"alarm_window": NIGHT})
        notifier = RecordingNotifier()
        engine = Engine(cfg, notifier=notifier)
        engine.on_uplink(lds02(CLOSED, OUTSIDE - 60))
        engine.on_uplink(lds02(OPEN, OUTSIDE))
        self.assertEqual(sent(notifier), [("door", True)])

    def test_window_from_defaults_applies_to_every_device(self):
        cfg = make_config(devices={LDS02_EUI: {"name": "Door1", "family": PROFILE}},
                          defaults={"alarm_window": NIGHT})
        notifier = RecordingNotifier()
        engine = Engine(cfg, notifier=notifier)
        engine.on_uplink(lds02(CLOSED, OUTSIDE - 60))
        engine.on_uplink(lds02(OPEN, OUTSIDE))
        self.assertEqual(notifier.calls, [])


class KindsTest(unittest.TestCase):
    def test_health_alarms_ignore_the_window(self):
        engine, notifier, _ = door_engine()
        low_battery = {**CLOSED, "BAT_V": 2.4}
        engine.on_uplink(lds02(CLOSED, OUTSIDE - 60))
        engine.on_uplink(lds02(low_battery, OUTSIDE))
        self.assertEqual(sent(notifier), [("battery_low", True)])

    def test_stale_ignores_the_window(self):
        engine, notifier, _ = door_engine()
        engine.on_uplink(lds02(CLOSED, OUTSIDE))
        with mock.patch("loralarm.service.utc_now", lambda: T0 + timedelta(hours=30)):
            engine.watchdog_tick()
        self.assertEqual(sent(notifier), [("stale", True)])

    def test_alarm_window_kinds_replaces_the_default_list(self):
        # only temperature is windowed on this device: the door is no longer
        engine, notifier, _ = door_engine(alarm_window_kinds=["temp"])
        engine.on_uplink(lds02(CLOSED, OUTSIDE - 60))
        engine.on_uplink(lds02(OPEN, OUTSIDE))
        self.assertEqual(sent(notifier), [("door", True)])

    def test_a_health_kind_can_be_windowed_explicitly(self):
        engine, notifier, _ = door_engine(alarm_window_kinds=["battery_low"])
        engine.on_uplink(lds02({**CLOSED, "BAT_V": 2.4}, OUTSIDE))
        self.assertEqual(notifier.calls, [])

    def test_declarative_rule_is_windowed_unless_it_says_otherwise(self):
        from loralarm.definitions import rule_from_dict
        base = {"kind": "k", "label": "K", "field": "f", "when": "truthy"}
        self.assertTrue(rule_from_dict(base).windowed)
        self.assertFalse(rule_from_dict({**base, "windowed": False}).windowed)
        with self.assertRaises(ValueError):
            rule_from_dict({**base, "windowed": "no"})

    def test_builtin_health_rules_are_not_windowed(self):
        self.assertEqual({k: REG.rule_for(k).windowed for k in
                          ("temp", "battery_low", "battery_empty", "stale",
                           "motion", "circuit", "tilt", "door")},
                         {"temp": False, "battery_low": False, "battery_empty": False, "stale": False,
                          "motion": True, "circuit": True, "tilt": True, "door": True})

    def test_pulse_alarms_outside_the_window_are_dropped_and_leave_no_state(self):
        cfg = make_config(devices={DEV_EUI: {"name": "sensor3", "family": "RAK4631",
                                              "alarm_window": NIGHT}})
        notifier = RecordingNotifier()
        engine = Engine(cfg, notifier=notifier)
        engine.on_uplink(make_uplink(obj=sensor_obj(motion_alert=1), time=T0))                  # 16:33
        engine.on_uplink(make_uplink(obj=sensor_obj(motion_alert=1), time=T0 + timedelta(hours=5)))  # 21:33
        self.assertEqual(sent(notifier), [("motion", True)])
        _, state = engine.devices[DEV_EUI]
        self.assertEqual(state.suppressed, set())                   # fire-only: nothing to remember


class FailOpenTest(unittest.TestCase):
    def _run(self, window: str, tz: str | None = None):
        extra = {"timezone": tz} if tz else {}
        engine, notifier, _ = door_engine(window=window, **extra)
        with self.assertLogs("loralarm.service", level="ERROR") as cm:
            engine.on_uplink(lds02(CLOSED, OUTSIDE - 60))
            engine.on_uplink(lds02(OPEN, OUTSIDE))
            engine.on_uplink(lds02(CLOSED, OUTSIDE + 30))
            engine.on_uplink(lds02(OPEN, OUTSIDE + 60))
        return notifier, cm

    def test_bad_window_text_sends_everything_and_reports_once(self):
        notifier, cm = self._run("evenings")
        self.assertEqual(sent(notifier), [("door", True), ("door", False), ("door", True)])
        self.assertEqual(len(cm.records), 1)
        self.assertIn("HH:MM-HH:MM", cm.output[0])

    def test_unknown_timezone_sends_everything_and_reports_once(self):
        notifier, cm = self._run(NIGHT, tz="Mars/Olympus_Mons")
        self.assertEqual(len(sent(notifier)), 3)
        self.assertEqual(len(cm.records), 1)
        self.assertIn("time zone", cm.output[0])

    def test_wrong_type_for_alarm_window_kinds_is_ignored_with_a_warning(self):
        engine, notifier, _ = door_engine(alarm_window_kinds="door")
        with self.assertLogs("loralarm.config", level="WARNING"):
            engine.on_uplink(lds02(CLOSED, OUTSIDE - 60))
            engine.on_uplink(lds02(OPEN, OUTSIDE))
        self.assertEqual(notifier.calls, [])          # default list: door is windowed


class PersistenceTest(unittest.TestCase):
    def test_suppressed_survives_a_restart_so_the_clear_stays_quiet(self):
        engine, _, log = door_engine()
        engine.on_uplink(lds02(CLOSED, OUTSIDE - 60))
        engine.on_uplink(lds02(OPEN, OUTSIDE))
        notifier = RecordingNotifier()
        restarted = Engine(make_config(devices={LDS02_EUI: {"name": "Door1", "family": PROFILE,
                                                            "alarm_window": NIGHT}}),
                           event_log=log, notifier=notifier)
        restarted.seed()
        _, state = restarted.devices[LDS02_EUI]
        self.assertEqual(state.suppressed, {AlarmKind("door")})
        restarted.on_uplink(lds02(CLOSED, OUTSIDE + 30))
        self.assertEqual(notifier.calls, [])

    def test_old_snapshot_without_suppressed_restores_and_its_clear_is_sent(self):
        log = EventLog(None)
        log.save_state(LDS02_EUI, {"active": ["door"], "last_seen": T0.isoformat()})
        notifier = RecordingNotifier()
        engine = Engine(make_config(devices={LDS02_EUI: {"name": "Door1", "family": PROFILE,
                                                         "alarm_window": NIGHT}}),
                        event_log=log, notifier=notifier)
        engine.seed()
        engine.on_uplink(lds02(CLOSED, OUTSIDE))
        self.assertEqual(sent(notifier), [("door", False)])       # unknown = notify (fail safe)

    def test_snapshot_round_trip(self):
        st = AlarmState()
        st.suppressed = {AlarmKind("door")}
        st.active = {AlarmKind("door")}
        restored = AlarmState()
        restored.restore(st.snapshot())
        self.assertEqual(restored.suppressed, {AlarmKind("door")})


if __name__ == "__main__":
    unittest.main()
