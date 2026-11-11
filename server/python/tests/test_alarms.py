"""Tests for the pure alarm logic in alarms.py (no I/O)."""
from __future__ import annotations

import unittest
from datetime import timedelta

from loralarm.alarms import AlarmKind, TILT_REBASELINE_AFTER, format_duration

from .common import AlarmState, T0, evaluate, make_cfg, make_uplink, sensor_obj


class EvaluateTest(unittest.TestCase):
    def test_temp_alarm_after_tk_correction(self):
        cfg = make_cfg()  # tk 8.0, limit 2.0
        frame, fired = evaluate(cfg, make_uplink(obj=sensor_obj(temperature=9.0)))
        self.assertEqual(frame.values["temperature"], 1.0)
        self.assertEqual([t.kind for t in fired], [AlarmKind.TEMP])
        self.assertIn("1.0 °C", fired[0].detail)

    def test_battery_percent_and_status(self):
        cfg = make_cfg()  # full 4.2, warn 3.5, empty 3.2
        frame, fired = evaluate(cfg, make_uplink(obj=sensor_obj(battery_voltage=3.85)))
        self.assertEqual(frame.values["battery_status"], "ok")
        self.assertAlmostEqual(frame.values["battery_pct"], 65.0, places=1)  # (3.85-3.2)/(4.2-3.2)
        self.assertEqual(fired, [])

    def test_battery_zero_span_no_zero_division(self):
        cfg = make_cfg(batt_full_v=3.2, batt_empty_v=3.2)
        frame, fired = evaluate(cfg, make_uplink(obj=sensor_obj(battery_voltage=3.0)))
        self.assertNotIn("battery_pct", frame.values)
        self.assertEqual(frame.values["battery_status"], "empty")
        self.assertEqual([t.kind for t in fired], [AlarmKind.BATTERY_EMPTY])

    def test_missing_or_invalid_sources_leave_no_value(self):
        cfg = make_cfg()
        frame, fired = evaluate(cfg, make_uplink(obj={"temperature": "n/a", "battery_voltage": None}))
        self.assertNotIn("temperature", frame.values)
        self.assertNotIn("battery_voltage", frame.values)
        self.assertNotIn("battery_pct", frame.values)
        self.assertEqual(frame.values["battery_status"], "unknown")
        self.assertEqual(fired, [])

    def test_motion_and_circuit(self):
        cfg = make_cfg()
        _, fired = evaluate(cfg, make_uplink(obj=sensor_obj(motion_alert=1, motion_switch=1)))
        self.assertEqual({t.kind for t in fired}, {AlarmKind.MOTION, AlarmKind.CIRCUIT})


class AlarmStateTest(unittest.TestCase):
    def test_fire_once_clear_once(self):
        cfg = make_cfg(motion_mode="level")  # level mode keeps fire-once/clear-once
        st = AlarmState()
        _, t1 = st.update(cfg, make_uplink(obj=sensor_obj(motion_alert=1)))
        _, t2 = st.update(cfg, make_uplink(obj=sensor_obj(motion_alert=1)))  # still active: no repeat
        _, t3 = st.update(cfg, make_uplink(obj=sensor_obj(motion_alert=0)))
        self.assertEqual([(t.kind, t.active) for t in t1], [(AlarmKind.MOTION, True)])
        self.assertEqual(t2, [])
        self.assertEqual([(t.kind, t.active) for t in t3], [(AlarmKind.MOTION, False)])

    def test_clear_detail_uses_current_reading(self):
        cfg = make_cfg()
        st = AlarmState()
        st.update(cfg, make_uplink(obj=sensor_obj(battery_voltage=3.4)))   # low -> fires
        _, transitions = st.update(cfg, make_uplink(obj=sensor_obj(battery_voltage=3.9)))
        self.assertEqual(len(transitions), 1)
        clear = transitions[0]
        self.assertFalse(clear.active)
        self.assertEqual(clear.kind, AlarmKind.BATTERY_LOW)
        # must describe the current reading, not the stale "≤ varningsgräns" fire text
        self.assertIn("3.90 V", clear.detail)
        self.assertIn(">", clear.detail)

    def test_battery_low_to_empty_is_escalation_not_clear(self):
        cfg = make_cfg()
        st = AlarmState()
        st.update(cfg, make_uplink(obj=sensor_obj(battery_voltage=3.4)))   # low
        _, transitions = st.update(cfg, make_uplink(obj=sensor_obj(battery_voltage=3.1)))  # empty
        kinds = [(t.kind, t.active) for t in transitions]
        self.assertIn((AlarmKind.BATTERY_EMPTY, True), kinds)
        self.assertNotIn((AlarmKind.BATTERY_LOW, False), kinds)  # no misleading "Batteri OK"
        self.assertEqual(st.active, {AlarmKind.BATTERY_EMPTY})

    def test_battery_empty_to_low_suppresses_clear(self):
        cfg = make_cfg()
        st = AlarmState()
        st.update(cfg, make_uplink(obj=sensor_obj(battery_voltage=3.1)))   # empty
        _, transitions = st.update(cfg, make_uplink(obj=sensor_obj(battery_voltage=3.4)))  # low
        kinds = [(t.kind, t.active) for t in transitions]
        self.assertIn((AlarmKind.BATTERY_LOW, True), kinds)
        self.assertNotIn((AlarmKind.BATTERY_EMPTY, False), kinds)
        self.assertEqual(st.active, {AlarmKind.BATTERY_LOW})

    def test_full_recovery_clears_normally(self):
        cfg = make_cfg()
        st = AlarmState()
        st.update(cfg, make_uplink(obj=sensor_obj(battery_voltage=3.4)))
        _, transitions = st.update(cfg, make_uplink(obj=sensor_obj(battery_voltage=3.9)))
        self.assertEqual([(t.kind, t.active) for t in transitions],
                         [(AlarmKind.BATTERY_LOW, False)])

    def test_touch_only_updates_last_seen(self):
        cfg = make_cfg(motion_mode="level")
        st = AlarmState()
        st.update(cfg, make_uplink(obj=sensor_obj(motion_alert=1)))
        later = T0.replace(hour=18)
        st.touch(make_uplink(f_port=4, obj={}, time=later, data_b64="MDE2"))
        self.assertEqual(st.last_seen, later)
        self.assertEqual(st.active, {AlarmKind.MOTION})  # nothing cleared

    def test_stale_watchdog_fire_and_clear_on_uplink(self):
        cfg = make_cfg()
        st = AlarmState()
        st.last_seen = T0
        later = T0 + timedelta(hours=25)
        fired = st.watchdog(cfg, later)
        self.assertEqual([(t.kind, t.active) for t in fired], [(AlarmKind.STALE, True)])
        self.assertIn("ingen uplink på 1 dag, 01:00:00", fired[0].detail)
        # a real uplink must not clear STALE from update(); the watchdog owns it
        _, transitions = st.update(cfg, make_uplink(obj=sensor_obj(), time=later))
        self.assertEqual(transitions, [])
        self.assertIn(AlarmKind.STALE, st.active)
        cleared = st.watchdog(cfg, later)
        self.assertEqual([(t.kind, t.active, t.detail) for t in cleared],
                         [(AlarmKind.STALE, False, "uplink mottagen")])

    def test_watchdog_disabled(self):
        cfg = make_cfg(watchdog_hours=0)
        st = AlarmState()
        st.last_seen = T0
        self.assertEqual(st.watchdog(cfg, T0 + timedelta(days=30)), [])

    def test_tilt_fires_then_rebaselines_after_sustained_tilt(self):
        cfg = make_cfg()
        st = AlarmState()
        st.update(cfg, make_uplink(obj=sensor_obj()))  # calm sample -> baseline
        self.assertEqual(st.accel_baseline, {"x": 0.1, "y": 0.0, "z": 1.0})
        tilted = sensor_obj(accelerometer={"x": 0.9, "y": 0.0, "z": 1.0})
        _, fired = st.update(cfg, make_uplink(obj=tilted))
        self.assertEqual([t.kind for t in fired], [AlarmKind.TILT])
        self.assertIn(AlarmKind.TILT, st.active)
        last = []
        for _ in range(TILT_REBASELINE_AFTER - 2):
            _, last = st.update(cfg, make_uplink(obj=tilted))
            self.assertIn(AlarmKind.TILT, st.active)  # still alarmed
        _, last = st.update(cfg, make_uplink(obj=tilted))  # streak hits the limit
        self.assertNotIn(AlarmKind.TILT, st.active)        # re-baselined -> cleared
        self.assertEqual([(t.kind, t.active) for t in last], [(AlarmKind.TILT, False)])
        self.assertEqual(st.accel_baseline["x"], 0.9)       # new orientation is the reference

    def test_snapshot_restore_roundtrip(self):
        cfg = make_cfg(motion_mode="level")
        st = AlarmState()
        st.update(cfg, make_uplink(obj=sensor_obj(motion_alert=1)))
        snap = st.snapshot()
        st2 = AlarmState()
        st2.restore(snap)
        self.assertEqual(st2.active, {AlarmKind.MOTION})
        self.assertEqual(st2.last_seen, st.last_seen)
        self.assertEqual(st2.last_detail, st.last_detail)


class CircuitModeTest(unittest.TestCase):
    """RAK4631 sends bit 0 only in the frame when the contact opens (nothing on close)."""

    def _kinds(self, transitions):
        return [(t.kind, t.active) for t in transitions]

    def test_level_mode_clears_when_bit_gone(self):
        cfg = make_cfg(circuit_mode="level")
        st = AlarmState()
        _, t1 = st.update(cfg, make_uplink(obj=sensor_obj(motion_switch=1)))
        _, t2 = st.update(cfg, make_uplink(obj=sensor_obj(), time=T0 + timedelta(minutes=5)))
        self.assertEqual(self._kinds(t1), [(AlarmKind.CIRCUIT, True)])
        self.assertEqual(self._kinds(t2), [(AlarmKind.CIRCUIT, False)])

    def test_event_mode_other_frames_never_clear(self):
        cfg = make_cfg(circuit_mode="event", motion_mode="level")
        st = AlarmState()
        _, t1 = st.update(cfg, make_uplink(obj=sensor_obj(motion_switch=1)))
        # ACC-alarm frame (status 0x02) and a timer frame both have motion_switch=0
        _, t2 = st.update(cfg, make_uplink(obj=sensor_obj(motion_alert=1),
                                           time=T0 + timedelta(seconds=5)))
        _, t3 = st.update(cfg, make_uplink(obj=sensor_obj(), time=T0 + timedelta(minutes=5)))
        self.assertEqual(self._kinds(t1), [(AlarmKind.CIRCUIT, True)])
        self.assertEqual(self._kinds(t2), [(AlarmKind.MOTION, True)])
        self.assertEqual(self._kinds(t3), [(AlarmKind.MOTION, False)])
        self.assertNotIn(AlarmKind.CIRCUIT, st.active)

    def test_event_mode_each_pulse_alarms_again(self):
        cfg = make_cfg(circuit_mode="event", circuit_hold_s=30)
        st = AlarmState()
        counts = []
        for secs in (0, 10, 120, 125):  # 10 s and 125 s are within the hold of the previous alarm
            _, t = st.update(cfg, make_uplink(obj=sensor_obj(motion_switch=1),
                                              time=T0 + timedelta(seconds=secs)))
            counts.append(len(t))
        self.assertEqual(counts, [1, 0, 1, 0])

    def test_event_state_survives_snapshot(self):
        cfg = make_cfg(circuit_mode="event")
        st = AlarmState()
        st.update(cfg, make_uplink(obj=sensor_obj(motion_switch=1)))
        restored = AlarmState()
        restored.restore(st.snapshot())
        _, t = restored.update(cfg, make_uplink(obj=sensor_obj(motion_switch=1),
                                                time=T0 + timedelta(seconds=5)))
        self.assertEqual(t, [])  # retransmit right after a restart is still deduplicated


class MotionModeTest(unittest.TestCase):
    """Default motion_mode="event": the firmware sends motion_alert=1 in one frame
    per accelerometer interrupt, so every detection must be reported (transparent),
    and no other frame ever clears it."""

    def _kinds(self, transitions):
        return [(t.kind, t.active) for t in transitions]

    def test_event_mode_every_detection_alarms_and_never_clears(self):
        cfg = make_cfg()  # motion_mode defaults to "event"
        st = AlarmState()
        fired = []
        for secs in (0, 5, 10):  # continuous shaking: back-to-back detections
            _, t = st.update(cfg, make_uplink(obj=sensor_obj(motion_alert=1),
                                              time=T0 + timedelta(seconds=secs)))
            fired.append(self._kinds(t))
        self.assertEqual(fired, [[(AlarmKind.MOTION, True)]] * 3)  # every detection visible
        _, t = st.update(cfg, make_uplink(obj=sensor_obj(), time=T0 + timedelta(minutes=5)))
        self.assertEqual(t, [])                    # heartbeat does not clear
        self.assertNotIn(AlarmKind.MOTION, st.active)
        _, t = st.update(cfg, make_uplink(obj=sensor_obj(motion_alert=1),
                                          time=T0 + timedelta(minutes=6)))
        self.assertEqual(self._kinds(t), [(AlarmKind.MOTION, True)])  # re-armed trivially: next pulse alarms

    def test_event_mode_detail_counts_detections_in_window(self):
        cfg = make_cfg(motion_window_s=30)
        st = AlarmState()
        _, t3 = st.update(cfg, make_uplink(obj=sensor_obj(motion_alert=1),
                                           time=T0 + timedelta(seconds=20)))
        for _ in range(2):
            _, t3 = st.update(cfg, make_uplink(obj=sensor_obj(motion_alert=1),
                                               time=T0 + timedelta(seconds=25)))
        self.assertIn("3 detekteringar inom 30 s", t3[0].detail)

    def test_window_expiry_resets_the_count(self):
        cfg = make_cfg(motion_window_s=30)
        st = AlarmState()
        st.update(cfg, make_uplink(obj=sensor_obj(motion_alert=1), time=T0))
        _, t = st.update(cfg, make_uplink(obj=sensor_obj(motion_alert=1),
                                          time=T0 + timedelta(seconds=31)))
        self.assertIn("1 detektering inom 30 s", t[0].detail)  # old one fell out of the window

    def test_min_detections_alarms_only_on_the_nth_within_window(self):
        cfg = make_cfg(motion_mode="event", motion_min_detections=3, motion_window_s=30)
        st = AlarmState()
        fired = []
        details = []
        for secs in (0, 5, 10, 12, 40, 45, 50):  # 3rd within window alarms, 4th is same burst
            _, t = st.update(cfg, make_uplink(obj=sensor_obj(motion_alert=1),
                                              time=T0 + timedelta(seconds=secs)))
            fired.append(self._kinds(t))
            details.extend(x.detail for x in t)
        self.assertEqual(fired, [[], [], [(AlarmKind.MOTION, True)], [], [], [], [(AlarmKind.MOTION, True)]])
        self.assertTrue(any("gräns 3" in d for d in details))

    def test_event_state_survives_snapshot(self):
        cfg = make_cfg(motion_mode="event", motion_min_detections=3, motion_window_s=30)
        st = AlarmState()
        st.update(cfg, make_uplink(obj=sensor_obj(motion_alert=1), time=T0))
        st.update(cfg, make_uplink(obj=sensor_obj(motion_alert=1), time=T0 + timedelta(seconds=5)))
        restored = AlarmState()
        restored.restore(st.snapshot())
        _, t = restored.update(cfg, make_uplink(obj=sensor_obj(motion_alert=1),
                                                time=T0 + timedelta(seconds=10)))
        self.assertEqual(self._kinds(t), [(AlarmKind.MOTION, True)])  # 3rd detection after restart still alarms

    def test_level_mode_still_fire_once_clear_once(self):
        cfg = make_cfg(motion_mode="level")
        st = AlarmState()
        _, t1 = st.update(cfg, make_uplink(obj=sensor_obj(motion_alert=1)))
        _, t2 = st.update(cfg, make_uplink(obj=sensor_obj(motion_alert=1)))
        _, t3 = st.update(cfg, make_uplink(obj=sensor_obj(motion_alert=0)))
        self.assertEqual(self._kinds(t1), [(AlarmKind.MOTION, True)])
        self.assertEqual(t2, [])
        self.assertEqual(self._kinds(t3), [(AlarmKind.MOTION, False)])


class FormatDurationTest(unittest.TestCase):
    def test_no_microseconds_swedish_days(self):
        td = timedelta(days=1, hours=2, minutes=3, seconds=4, microseconds=123456)
        self.assertEqual(format_duration(td), "1 dag, 02:03:04")
        self.assertEqual(format_duration(td - timedelta(days=1)), "02:03:04")
        self.assertEqual(format_duration(timedelta(days=2, seconds=5)), "2 dagar, 00:00:05")


if __name__ == "__main__":
    unittest.main()
