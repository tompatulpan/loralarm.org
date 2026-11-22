"""Config-driven actions (actions.py): a device event that sends a downlink or is
swallowed instead of raising its alarm, through the Engine."""
from __future__ import annotations

import logging
import unittest

from loralarm.actions import action_from_dict
from loralarm.events import EventLog
from loralarm.service import Engine

from .common import make_config
from .test_engine import RecordingNotifier
from .test_ws101 import BTN_EUI, PROFILE, press

RELAY_EUI = "24e124535d240001"
APP_ID = "app-1"
RELAY_ON = {"field": "button", "when": "eq", "value": "double press",
            "do": "downlink", "target": "relay1", "port": 10, "ascii": "001"}


def make(actions=None, family_actions=None, **device):
    sent = []

    class FakeSender:
        def send(self, app_id, dl):
            sent.append((app_id, dl))

    entry = {"name": "Button 1", "application_id": APP_ID, **device}
    if actions is not None:
        entry["actions"] = actions
    cfg = make_config(devices={BTN_EUI: entry,
                               RELAY_EUI: {"name": "relay1", "application_id": APP_ID}},
                      families={PROFILE: {"actions": family_actions}} if family_actions else None)
    log = EventLog(None)
    notifier = RecordingNotifier()
    return Engine(cfg, log, notifier, downlink_sender=FakeSender(), dl_topic="x/+"), log, notifier, sent


def kinds(notifier):
    return [c[1] for c in notifier.calls]


class ActionParseTest(unittest.TestCase):
    def test_valid_downlink(self):
        a = action_from_dict(RELAY_ON)
        self.assertEqual((a.do, a.target, a.frame.f_port, a.frame.data), ("downlink", "relay1", 10, b"001"))
        self.assertTrue(a.suppresses_alarm)
        self.assertFalse(action_from_dict({**RELAY_ON, "alarm": True}).suppresses_alarm)
        self.assertFalse(action_from_dict({"field": "button", "when": "truthy", "do": "alarm"}).suppresses_alarm)

    def test_hex_payload(self):
        a = action_from_dict({**{k: v for k, v in RELAY_ON.items() if k != "ascii"}, "hex": "0A01"})
        self.assertEqual(a.frame.data, b"\x0a\x01")

    def test_rejects_bad_entries(self):
        base = {k: v for k, v in RELAY_ON.items() if k != "ascii"}
        bad = [
            "x", {}, {**RELAY_ON, "typo": 1}, {**RELAY_ON, "do": "explode"},
            {**RELAY_ON, "when": "near"}, {**RELAY_ON, "port": 0}, {**RELAY_ON, "port": 224},
            {**RELAY_ON, "port": True}, {**RELAY_ON, "target": ""}, base,
            {**base, "ascii": "1", "hex": "01"}, {**base, "hex": "zz"}, {**base, "hex": ""},
            {**base, "hex": "00" * 52}, {**RELAY_ON, "alarm": "yes"},
            {"field": "button", "when": "eq", "do": "ignore"},
        ]
        for entry in bad:
            with self.subTest(entry=entry), self.assertRaises(ValueError):
                action_from_dict(entry)


class ActionEngineTest(unittest.TestCase):
    def test_unmapped_press_is_still_a_panic_alarm(self):
        engine, _, notifier, sent = make(actions=[RELAY_ON])
        engine.on_uplink(press("short press"))
        self.assertEqual(kinds(notifier), ["button"])
        self.assertEqual(sent, [])

    def test_mapped_press_sends_downlink_instead_of_alarm(self):
        engine, log, notifier, sent = make(actions=[RELAY_ON])
        engine.on_uplink(press("double press"))
        self.assertEqual(kinds(notifier), [])
        (app_id, dl), = sent
        self.assertEqual((app_id, dl.dev_eui, dl.f_port, dl.data), (APP_ID, RELAY_EUI, 10, b"001"))
        row = log._conn.execute("select device_name, active, detail from events where kind='downlink'").fetchone()
        self.assertEqual(row[:2], ("relay1", 1))
        self.assertIn("action button='double press'", row[2])
        self.assertEqual(log._conn.execute("select count(*) from events where kind='button'").fetchone()[0], 0)

    def test_alarm_true_keeps_the_alarm_too(self):
        engine, _, notifier, sent = make(actions=[{**RELAY_ON, "alarm": True}])
        engine.on_uplink(press("double press"))
        self.assertEqual((kinds(notifier), len(sent)), (["button"], 1))

    def test_ignore_swallows_the_press(self):
        engine, _, notifier, sent = make(actions=[
            {"field": "button", "when": "eq", "value": "long press", "do": "ignore"}])
        engine.on_uplink(press("long press"))
        self.assertEqual((kinds(notifier), sent), ([], []))
        engine.on_uplink(press("short press", 5))
        self.assertEqual(kinds(notifier), ["button"])

    def test_one_button_two_things(self):
        off = {**RELAY_ON, "value": "long press", "ascii": "000"}
        engine, _, notifier, sent = make(actions=[RELAY_ON, off])
        engine.on_uplink(press("double press"))
        engine.on_uplink(press("long press", 5))
        self.assertEqual([dl.data for _, dl in sent], [b"001", b"000"])
        self.assertEqual(kinds(notifier), [])

    def test_family_default_and_device_override(self):
        engine, _, notifier, sent = make(family_actions=[RELAY_ON])
        engine.on_uplink(press("double press"))
        self.assertEqual((len(sent), kinds(notifier)), (1, []))
        # a device list replaces the family's: double press is a panic alarm again
        engine, _, notifier, sent = make(actions=[], family_actions=[RELAY_ON])
        engine.on_uplink(press("double press"))
        self.assertEqual((len(sent), kinds(notifier)), (0, ["button"]))

    def test_unknown_target_is_logged_not_raised(self):
        engine, log, notifier, sent = make(actions=[{**RELAY_ON, "target": "nowhere"}])
        engine.on_uplink(press("double press"))
        self.assertEqual(sent, [])
        row = log._conn.execute("select active, detail from events where kind='downlink'").fetchone()
        self.assertEqual(row[0], 0)
        self.assertIn("nowhere", row[1])

    def test_missing_sender_is_logged_not_raised(self):
        engine, log, _, _ = make(actions=[RELAY_ON])
        engine.downlink_sender = None
        engine.on_uplink(press("double press"))
        self.assertEqual(log._conn.execute(
            "select active from events where kind='downlink'").fetchone()[0], 0)

    def test_invalid_entry_is_skipped_and_the_alarm_survives(self):
        engine, _, notifier, sent = make(actions=[{**RELAY_ON, "port": 999}])
        with self.assertLogs("loralarm.actions", logging.ERROR):
            engine.on_uplink(press("double press"))
        self.assertEqual((kinds(notifier), sent), (["button"], []))

    def test_non_list_actions_are_ignored_by_config(self):
        engine, _, notifier, _ = make(actions=None)
        engine.config.devices[BTN_EUI]["actions"] = "nope"
        with self.assertLogs("loralarm.config", logging.WARNING):
            engine.on_uplink(press("double press"))
        self.assertEqual(kinds(notifier), ["button"])

    def test_battery_frame_never_triggers_a_button_action(self):
        engine, _, _, sent = make(actions=[RELAY_ON])
        from .test_ws101 import battery
        engine.on_uplink(battery(50))
        self.assertEqual(sent, [])


if __name__ == "__main__":
    unittest.main()
