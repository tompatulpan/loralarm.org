"""Tests for notification text rendering and the queued, retrying Notifier."""
from __future__ import annotations

import logging
import unittest

from loralarm.notify import DEFAULT_TEXT, Notifier, encode_header, message_text

from .common import make_cfg


class MessageTextTest(unittest.TestCase):
    def test_config_template_wins_and_supports_label(self):
        cfg = make_cfg(notify_fire={"motion": "{label} på {name}"},
                       notify_clear={"motion": "{label} kvitterad på {name}"})
        fire = message_text("motion", cfg.name, True, "rörelse upptäckt",
                            cfg.notify_fire, cfg.notify_clear)
        clear = message_text("motion", cfg.name, False, "ingen rörelse",
                             cfg.notify_fire, cfg.notify_clear)
        self.assertEqual(fire, "Rörelselarm på sensor3")
        self.assertEqual(clear, "Rörelselarm kvitterad på sensor3")

    def test_builtin_default_text(self):
        fire = message_text("temp", "sensor3", True, "1.0 °C ≤ gräns 2.0 °C")
        clear = message_text("temp", "sensor3", False, "12.0 °C > gräns 2.0 °C")
        self.assertEqual(fire, DEFAULT_TEXT["temp"][0].format(name="sensor3", detail="1.0 °C ≤ gräns 2.0 °C", label="Temperaturalarm"))
        self.assertIn("Temperatur OK", clear)
        self.assertIn("12.0 °C > gräns 2.0 °C", clear)

    def test_unknown_kind_fallback(self):
        text = message_text("mystery", "sensor3", True, "d")
        self.assertIn("mystery", text)
        self.assertIn("LARM", text)


class HeaderEncodingTest(unittest.TestCase):
    def test_non_ascii_title_is_rfc2047(self):
        enc = encode_header("Rörelselarm på sensor3")
        enc.encode("latin-1")  # must be latin-1 safe for http.client
        self.assertIn("=?utf-8?", enc)
        self.assertNotIn("ö", enc)


class NotifierTest(unittest.TestCase):
    def setUp(self):
        logging.basicConfig(level=logging.CRITICAL)  # keep test output clean

    def test_dry_run_inline_does_not_send(self):
        n = Notifier(ntfy_url="http://127.0.0.1:1", dry_run=True)
        n.notify(make_cfg(), "motion", True, "rörelse")  # must not raise / hit network

    def test_worker_delivers_queued_notification(self):
        n = Notifier(ntfy_url="http://ntfy.invalid", dry_run=False, retries=1)
        sent = []
        n._ntfy = lambda *a: sent.append(a)
        n._pushover = lambda *a: sent.append(a)
        n.start()
        n.notify(make_cfg(), "motion", True, "rörelse")
        n.close()
        self.assertEqual(len(sent), 1)  # ntfy only: no pushover creds configured

    def test_retry_then_success(self):
        n = Notifier(ntfy_url="http://ntfy.invalid", retries=3)
        calls = []

        def flaky(*a):
            calls.append(1)
            if len(calls) < 3:
                raise OSError("boom")
        n._ntfy = flaky
        self.assertTrue(n._send(n._ntfy, "topic", "t", "m", True, label="ntfy", target="topic"))
        self.assertEqual(len(calls), 3)

    def test_all_attempts_fail_returns_false_and_never_raises(self):
        n = Notifier(ntfy_url="http://ntfy.invalid", retries=2)

        def always(*a):
            raise ValueError("not even an OSError")
        n._ntfy = always
        with self.assertLogs("loralarm.notify", level="ERROR"):
            ok = n._send(n._ntfy, "topic", "t", "m", True, label="ntfy", target="topic")
        self.assertFalse(ok)

    def test_notify_inline_never_raises_on_channel_error(self):
        n = Notifier(ntfy_url="http://127.0.0.1:1", dry_run=False, timeout=0.2, retries=1)
        n.notify(make_cfg(), "stale", True, "ingen uplink")  # connection refused -> logged, not raised


if __name__ == "__main__":
    unittest.main()
