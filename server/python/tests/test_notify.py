"""Tests for notification text rendering and the queued, retrying Notifier."""
from __future__ import annotations

import logging
import unittest
from unittest import mock

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
        for kind, label in (("temp_low", "Lågtemperaturalarm"), ("temp_high", "Högtemperaturalarm")):
            fire = message_text(kind, "sensor3", True, "1.0 °C ≤ gräns 2.0 °C")
            clear = message_text(kind, "sensor3", False, "12.0 °C > gräns 2.0 °C")
            self.assertEqual(fire, DEFAULT_TEXT[kind][0].format(name="sensor3", detail="1.0 °C ≤ gräns 2.0 °C", label=label))
            self.assertIn("Temperatur OK", clear)
            self.assertIn("12.0 °C > gräns 2.0 °C", clear)

    def test_bad_template_falls_back_to_builtin_text(self):
        text = message_text("motion", "sensor3", True, "d",
                            fire={"motion": "{oops}"}, clear={})
        self.assertEqual(text, DEFAULT_TEXT["motion"][0].format(name="sensor3", detail="d"))

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

    def test_critical_fire_is_emergency_priority_and_clear_cancels_by_tag(self):
        import urllib.parse
        from loralarm.notify import PUSHOVER_API
        n = Notifier(ntfy_url="http://ntfy.invalid", retries=1)
        reqs = []

        class Resp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return b"{}"

        def fake_urlopen(req, timeout=None):
            reqs.append((req.full_url, urllib.parse.parse_qs(req.data.decode())))
            return Resp()

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            n._pushover("tok", "usr", "t", "m", True, "EUI-motion")
            n._pushover("tok", "usr", "t", "m", False, "EUI-motion")
            n._pushover("tok", "usr", "t", "m", True, "")
        fire, cancel, clear, plain = reqs
        self.assertEqual(fire[0], PUSHOVER_API)
        self.assertEqual(fire[1]["priority"], ["2"])
        self.assertEqual(fire[1]["tags"], ["EUI-motion"])
        self.assertIn("cancel_by_tag/EUI-motion.json", cancel[0])
        self.assertEqual(clear[1]["priority"], ["0"])
        self.assertEqual(plain[1]["priority"], ["1"])   # non-critical: no emergency

    def test_cancel_failure_does_not_block_clear_message(self):
        n = Notifier(ntfy_url="http://ntfy.invalid", retries=1)
        calls = []

        class Resp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return b"{}"

        def fake_urlopen(req, timeout=None):
            calls.append(req.full_url)
            if "cancel_by_tag" in req.full_url:
                raise OSError("down")
            return Resp()

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            n._pushover("tok", "usr", "t", "m", False, "EUI-motion")
        self.assertEqual(len(calls), 2)   # cancel attempted, clear still sent

    def test_webpush_enqueue_failure_does_not_block_ntfy(self):
        class BadWebPush:
            def fan_out(self, *a, **kw):
                raise RuntimeError("boom")
        n = Notifier(ntfy_url="http://ntfy.invalid", retries=1, webpush=BadWebPush())
        sent = []
        n._ntfy = lambda *a: sent.append(a)
        n.notify(make_cfg(), "motion", True, "rörelse")
        self.assertEqual(len(sent), 1)

    def test_retry_then_success(self):
        n = Notifier(ntfy_url="http://ntfy.invalid", retries=3, backoff=())
        calls = []

        def flaky(*a):
            calls.append(1)
            if len(calls) < 3:
                raise OSError("boom")
        n._ntfy = flaky
        self.assertTrue(n._send(n._ntfy, "topic", "t", "m", True, label="ntfy", target="topic"))
        self.assertEqual(len(calls), 3)

    def test_all_attempts_fail_returns_false_and_never_raises(self):
        n = Notifier(ntfy_url="http://ntfy.invalid", retries=2, backoff=())

        def always(*a):
            raise ValueError("not even an OSError")
        n._ntfy = always
        with self.assertLogs("loralarm.notify", level="ERROR"):
            ok = n._send(n._ntfy, "topic", "t", "m", True, label="ntfy", target="topic")
        self.assertFalse(ok)

    def test_notify_inline_never_raises_on_channel_error(self):
        n = Notifier(ntfy_url="http://127.0.0.1:1", dry_run=False, timeout=0.2, retries=1)
        n.notify(make_cfg(), "stale", True, "ingen uplink")  # connection refused -> logged, not raised

    def test_bad_template_still_delivers_alarm(self):
        n = Notifier(ntfy_url="http://ntfy.invalid", retries=1)
        sent = []
        n._ntfy = lambda *a: sent.append(a)
        n.notify(make_cfg(notify_fire={"motion": "{oops}"}, notify_title="{nope}"),
                 "motion", True, "d")
        self.assertEqual(len(sent), 1)
        topic, title, message, active = sent[0]
        self.assertEqual(title, "loralarm sensor3")
        self.assertIn("RÖRELSELARM", message)

    def test_backoff_between_retries(self):
        n = Notifier(ntfy_url="http://ntfy.invalid", retries=3, backoff=(1.0, 5.0))
        waits = []
        n._sleep = waits.append

        def always(*a):
            raise OSError("down")
        with self.assertLogs("loralarm.notify", level="ERROR"):
            self.assertFalse(n._send(always, label="ntfy", target="t"))
        self.assertEqual(waits, [1.0, 5.0])  # no sleep after the last attempt


class NtfyRequestTest(unittest.TestCase):
    def _request(self, **kwargs):
        captured = []

        def fake_urlopen(req, timeout=None):
            captured.append(req)
            return mock.MagicMock()
        n = Notifier(ntfy_url="http://ntfy.local:8075/", **kwargs)
        with mock.patch("loralarm.notify.urllib.request.urlopen", fake_urlopen):
            n._ntfy("sensor3", "Rörelselarm på sensor3", "msg", True)
        return captured[0]

    def test_bearer_token_header(self):
        req = self._request(ntfy_token="tk_secret")
        self.assertEqual(req.get_header("Authorization"), "Bearer tk_secret")
        self.assertEqual(req.full_url, "http://ntfy.local:8075/sensor3")
        self.assertEqual(req.get_header("Priority"), "5")

    def test_no_authorization_header_without_token(self):
        self.assertIsNone(self._request().get_header("Authorization"))

    def test_non_ascii_title_is_latin1_safe(self):
        self._request().get_header("Title").encode("latin-1")


if __name__ == "__main__":
    unittest.main()
