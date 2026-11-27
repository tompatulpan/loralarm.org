"""Web Push worker: own queue/thread, per-endpoint delivery log by endpoint_id
only, pruning on 404/410. send_fn is injected, so no network and no pywebpush."""
from __future__ import annotations

import json
import unittest

from loralarm.events import EventLog
from loralarm.webpush import WebPushError, WebPushWorker

from .common import DEV_EUI, make_cfg


def add_endpoint(log: EventLog, user_id: int, endpoint: str, eid: int | None = None) -> int:
    log._conn.execute(
        "INSERT INTO push_endpoints (user_id, endpoint, p256dh, auth, created_at) "
        "VALUES (?, ?, 'p256dh', 'auth', '2026-11-09T00:00:00.000000+00:00')",
        (user_id, endpoint))
    log._conn.commit()
    return log._conn.execute("select id from push_endpoints where endpoint = ?",
                             (endpoint,)).fetchone()[0]


class WebPushWorkerTest(unittest.TestCase):
    def setUp(self):
        self.log = EventLog(None)
        self.log.seed_users([{"name": "anna", "token": "t"}])   # id 1, follows all
        self.cfg = make_cfg()

    def make_worker(self, send_fn, **kw):
        w = WebPushWorker(self.log, send_fn=send_fn, dry_run=False, **kw)
        w.start()
        self.addCleanup(w.close)
        return w

    def drain(self, w):
        w._queue.join()  # worker processes in order; join waits for all jobs

    def test_fan_out_delivers_to_each_subscribed_endpoint_and_logs_by_id(self):
        e1 = add_endpoint(self.log, 1, "https://push.example/aaa")
        e2 = add_endpoint(self.log, 1, "https://push.example/bbb")
        sent = []

        def send_fn(sub_info, payload, urgency, ttl, timeout):
            sent.append((sub_info["endpoint"], json.loads(payload), urgency, ttl))
            return 201

        w = self.make_worker(send_fn)
        w.fan_out(self.cfg, "motion", True, "RÖRELSE", "rörelse upptäckt", 42)
        self.drain(w)

        self.assertEqual(len(sent), 2)
        for endpoint, payload, urgency, ttl in sent:
            self.assertEqual(payload["event_id"], 42)
            self.assertEqual(payload["dev_eui"], DEV_EUI)
            self.assertTrue(payload["active"])
            self.assertEqual(urgency, "high")     # fire = high urgency
            self.assertEqual(ttl, 3600)
        rows = self.log._conn.execute(
            "select event_id, endpoint_id, status, http_code from webpush_log").fetchall()
        self.assertEqual(sorted(r[1] for r in rows), sorted([e1, e2]))
        self.assertTrue(all(r[0] == 42 and r[2] == "sent" and r[3] == 201 for r in rows))
        # no raw endpoint anywhere in the log
        self.assertNotIn("push.example", str(rows))
        # endpoints marked ok
        self.assertEqual(self.log._conn.execute(
            "select fail_count from push_endpoints").fetchall(), [(0,), (0,)])

    def test_test_push_goes_to_the_calling_users_own_endpoints_only(self):
        e1 = add_endpoint(self.log, 1, "https://push.example/aaa")
        self.log.seed_users([{"name": "bosse", "token": "t2"}])
        add_endpoint(self.log, 2, "https://push.example/bbb")
        sent = []

        def send_fn(sub_info, payload, urgency, ttl, timeout):
            sent.append((sub_info["endpoint"], json.loads(payload)))
            return 201

        w = self.make_worker(send_fn)
        n = w.test_push(1, "Test", "hej")
        self.drain(w)
        self.assertEqual(n, 1)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0], "https://push.example/aaa")
        self.assertEqual(sent[0][1]["kind"], "test")
        self.assertIsNone(sent[0][1]["event_id"])
        rows = self.log._conn.execute(
            "select event_id, endpoint_id, status from webpush_log").fetchall()
        self.assertEqual(rows, [(None, e1, "sent")])

    def test_404_prunes_endpoint_and_410_too(self):
        e1 = add_endpoint(self.log, 1, "https://push.example/gone")
        add_endpoint(self.log, 1, "https://push.example/ok")

        def send_fn(sub_info, payload, urgency, ttl, timeout):
            if "gone" in sub_info["endpoint"]:
                raise WebPushError(404)
            return 201

        w = self.make_worker(send_fn)
        w.fan_out(self.cfg, "door", True, "DÖRR", "öppen", 7)
        self.drain(w)

        remaining = [r[0] for r in self.log._conn.execute("select endpoint from push_endpoints")]
        self.assertEqual(remaining, ["https://push.example/ok"])
        rows = self.log._conn.execute(
            "select endpoint_id, status, http_code from webpush_log order by id").fetchall()
        self.assertIn((e1, "pruned", 404), rows)

    def test_other_failure_counts_but_keeps_endpoint(self):
        e1 = add_endpoint(self.log, 1, "https://push.example/flaky")

        def send_fn(sub_info, payload, urgency, ttl, timeout):
            raise WebPushError(500)

        w = self.make_worker(send_fn)
        w.fan_out(self.cfg, "motion", True, "t", "m", 9)
        self.drain(w)
        self.assertEqual(self.log._conn.execute(
            "select fail_count from push_endpoints where id = ?", (e1,)).fetchone()[0], 1)
        row = self.log._conn.execute(
            "select status, http_code from webpush_log").fetchone()
        self.assertEqual(row, ("failed", 500))

    def test_failure_logs_the_push_services_reason(self):
        """Apple answers 403 with {"reason": ...}; that text is the diagnosis,
        so it must reach the journal (and stay out of the DB)."""
        e1 = add_endpoint(self.log, 1, "https://web.push.apple.com/xyz")

        def send_fn(sub_info, payload, urgency, ttl, timeout):
            raise WebPushError(403, '{"reason":"BadJwtToken"}')

        w = self.make_worker(send_fn)
        with self.assertLogs("loralarm.webpush", level="WARNING") as cm:
            w.fan_out(self.cfg, "motion", True, "t", "m", 9)
            self.drain(w)
        self.assertIn("BadJwtToken", cm.records[0].getMessage())
        self.assertEqual(self.log._conn.execute(
            "select status, http_code from webpush_log").fetchone(), ("failed", 403))
        self.assertEqual(self.log._conn.execute(
            "select fail_count from push_endpoints where id = ?", (e1,)).fetchone()[0], 1)

    def test_bookkeeping_error_does_not_stop_remaining_endpoints(self):
        add_endpoint(self.log, 1, "https://push.example/aaa")
        add_endpoint(self.log, 1, "https://push.example/bbb")
        sent = []

        def send_fn(sub_info, payload, urgency, ttl, timeout):
            sent.append(sub_info["endpoint"])
            return 201

        def boom(*a, **kw):
            raise RuntimeError("db gone")
        self.log.endpoint_ok = boom
        w = self.make_worker(send_fn)
        w.fan_out(self.cfg, "motion", True, "t", "m", 1)
        self.drain(w)
        self.assertEqual(len(sent), 2)

    def test_clear_uses_normal_urgency(self):
        add_endpoint(self.log, 1, "https://push.example/aaa")
        seen = []
        w = self.make_worker(lambda si, p, u, t, to: seen.append(u) or 201)
        w.fan_out(self.cfg, "door", False, "t", "stängd", 5)
        self.drain(w)
        self.assertEqual(seen, ["normal"])

    def test_device_without_followers_logs_warning_not_crash(self):
        log2 = EventLog(None)   # no users at all
        w = WebPushWorker(log2, send_fn=lambda *a: 201)
        w.start()
        self.addCleanup(w.close)
        w.fan_out(self.cfg, "motion", True, "t", "m", 1)
        w._queue.join()
        self.assertEqual(log2._conn.execute("select count(*) from webpush_log").fetchone()[0], 0)

    def test_grouped_device_is_pushed_only_to_members_and_admins(self):
        self.log.seed_users([{"name": "anna", "token": "t", "groups": ["kund-a"]},
                             {"name": "bosse", "token": "u"},
                             {"name": "chef", "token": "v", "role": "admin"}])
        ids = {n: self.log._conn.execute("select id from users where name=?", (n,)).fetchone()[0]
               for n in ("anna", "bosse", "chef")}
        for i, n in enumerate(("anna", "bosse", "chef")):
            add_endpoint(self.log, ids[n], f"https://push.example/{n}")
        seen = []
        w = self.make_worker(lambda si, p, u, t, to: seen.append(si["endpoint"]) or 201)
        w.fan_out(make_cfg(group="kund-a"), "motion", True, "t", "m", 1)
        self.drain(w)
        self.assertEqual(sorted(e.rsplit("/", 1)[1] for e in seen), ["anna", "chef"])

    def test_muted_device_is_not_pushed_to_that_user(self):
        add_endpoint(self.log, 1, "https://push.example/aaa")
        self.log.set_muted(1, [DEV_EUI])
        calls = []
        w = self.make_worker(lambda *a: calls.append(a) or 201)
        w.fan_out(self.cfg, "motion", True, "t", "m", 1)
        self.drain(w)
        self.assertEqual(calls, [])

    def test_followers_without_any_endpoint_are_logged_not_silent(self):
        """Followers exist but none has a subscribed browser: the skip must be
        visible in the journal — a silent one reads as 'push is broken'."""
        self.log.seed_users([{"name": "bosse", "token": "u2"}])   # 2 followers, 0 endpoints
        sent = []
        w = self.make_worker(lambda si, p, u, t, to: sent.append(si) or 201)
        with self.assertLogs("loralarm.webpush", level="INFO") as cm:
            w.fan_out(self.cfg, "motion", True, "t", "m", 1)
            self.drain(w)
        self.assertEqual(sent, [])
        self.assertIn("none with a push endpoint", cm.records[0].getMessage())

    def test_dry_run_logs_without_sending(self):
        e1 = add_endpoint(self.log, 1, "https://push.example/aaa")
        calls = []
        w = WebPushWorker(self.log, send_fn=lambda *a: calls.append(a) or 201, dry_run=True)
        w.start()
        self.addCleanup(w.close)
        w.fan_out(self.cfg, "motion", True, "t", "m", 3)
        w._queue.join()
        self.assertEqual(calls, [])   # send_fn never called
        self.assertEqual(self.log._conn.execute(
            "select status from webpush_log").fetchone()[0], "dry-run")


class WorkerResilienceTest(unittest.TestCase):
    """A dead worker thread is invisible: pushes keep being enqueued and are
    never sent, with no log line, until the service restarts. Both the per-job
    handler and fan_out have to keep the thread alive."""

    def setUp(self):
        self.log = EventLog(None)
        self.log.seed_users([{"name": "anna", "token": "t"}])   # id 1, follows all
        self.cfg = make_cfg()
        add_endpoint(self.log, 1, "https://push.example/aaa")

    @staticmethod
    def wait(w, timeout=2.0):
        """Poll instead of queue.join(): a dead thread would hang the join."""
        import time
        deadline = time.time() + timeout
        while w._queue.unfinished_tasks and time.time() < deadline:
            time.sleep(0.01)

    def make_worker(self, sent):
        w = WebPushWorker(self.log, send_fn=lambda si, p, u, t, to:
                          sent.append(si["endpoint"]) or 201)
        self.addCleanup(w.close)
        return w

    def test_a_failing_test_job_does_not_kill_the_worker(self):
        # a test job has no "cfg": the old handler read job["cfg"].dev_eui and
        # raised KeyError from inside `except`, killing the thread for good
        sent = []
        w = self.make_worker(sent)
        w.start()
        real = w._deliver

        def boom(job):
            if job.get("test"):
                raise RuntimeError("poisoned test job")
            return real(job)

        w._deliver = boom
        with self.assertLogs("loralarm.webpush", level="ERROR") as cm:
            w.test_push(1, "t", "b")
            w.fan_out(self.cfg, "motion", True, "t", "m", 1)
            self.wait(w)
        self.assertIn("test push", cm.records[0].getMessage())
        self.assertTrue(w._worker.is_alive())
        self.assertEqual(sent, ["https://push.example/aaa"])

    def test_a_dead_worker_thread_is_revived_by_fan_out(self):
        import threading
        sent = []
        w = self.make_worker(sent)          # deliberately never started
        dead = threading.Thread(target=lambda: None)
        dead.start()
        dead.join()
        w._worker = dead                    # the shape of a worker that died
        with self.assertLogs("loralarm.webpush", level="ERROR") as cm:
            w.fan_out(self.cfg, "motion", True, "t", "m", 1)
            self.wait(w)
        self.assertIn("worker thread is gone", cm.records[0].getMessage())
        self.assertEqual(sent, ["https://push.example/aaa"])
        self.assertTrue(w._worker.is_alive())


class StaleEndpointTest(unittest.TestCase):
    """iOS keeps answering 201 for a subscription whose app installation is
    gone, so 404/410 pruning never fires and the missing ack is the only
    signal. Report only, never delete."""

    def setUp(self):
        self.log = EventLog(None)
        self.log.seed_users([{"name": "anna", "token": "t"}])   # id 1
        self.eid = add_endpoint(self.log, 1, "https://web.push.apple.com/xyz")

    def worker(self):
        return WebPushWorker(self.log, send_fn=lambda *a: 201)

    def test_delivered_but_unacked_is_reported_once(self):
        self.log.webpush_log(None, self.eid, "sent", 201)
        w = self.worker()
        with self.assertLogs("loralarm.webpush", level="WARNING") as cm:
            self.assertEqual(w.report_stale(days=30), 1)
        self.assertIn("no receipt", cm.records[0].getMessage())
        self.assertNotIn("web.push.apple.com", cm.records[0].getMessage())  # secret
        self.assertEqual(self.log._conn.execute(
            "select status from webpush_log order by id desc limit 1").fetchone()[0], "stale")
        # once per endpoint per process: the watchdog calls this every few minutes
        self.assertEqual(w.report_stale(days=30), 0)

    def test_an_endpoint_that_acks_is_not_stale(self):
        from loralarm.events import utc_now
        ev = self.log.log(utc_now(), DEV_EUI, "sensor3", "motion", True, "x")
        self.log.webpush_log(ev, self.eid, "sent", 201)
        self.assertTrue(self.log.log_push_ack(1, ev, self.eid))
        self.assertEqual(self.worker().report_stale(days=30), 0)

    def test_deliveries_outside_the_window_do_not_count(self):
        from datetime import timedelta
        from loralarm.events import utc_now
        old = self.log._utc(utc_now() - timedelta(days=40))
        self.log._conn.execute(
            "INSERT INTO webpush_log (event_id, endpoint_id, status, http_code, ts) "
            "VALUES (NULL, ?, 'sent', 201, ?)", (self.eid, old))
        self.log._conn.commit()
        self.assertEqual(self.worker().report_stale(days=30), 0)


class ClaimsIsolationTest(unittest.TestCase):
    """pywebpush writes `aud`/`exp` into the claims dict it is given. The worker
    holds one dict for the whole process, so a shared dict pins the first push
    service's audience and every later push to another service is rejected as a
    bad JWT (Apple: 403 BadJwtToken, while Mozilla — contacted first — keeps
    working). This guards the copy per send.
    """

    def test_each_send_gets_its_own_copy_of_the_claims(self):
        import sys
        import types
        from unittest import mock
        from urllib.parse import urlsplit

        from loralarm.webpush import _pywebpush_send

        seen = []
        fake = types.ModuleType("pywebpush")

        class WebPushException(Exception):
            response = None

        def webpush(subscription_info, data, vapid_private_key, vapid_claims,
                    headers=None, ttl=0, timeout=0):
            # the mutation pywebpush 2.x performs, restricted to `aud`
            if not vapid_claims.get("aud"):
                parts = urlsplit(subscription_info["endpoint"])
                vapid_claims["aud"] = f"{parts.scheme}://{parts.netloc}"
            seen.append(vapid_claims["aud"])
            return types.SimpleNamespace(status_code=201)

        fake.WebPushException = WebPushException
        fake.webpush = webpush
        claims = {"sub": "mailto:me@example.org"}
        with mock.patch.dict(sys.modules, {"pywebpush": fake}):
            _pywebpush_send("key", claims,
                            {"endpoint": "https://updates.push.services.mozilla.com/a"},
                            "{}", "high", 60, 5)
            _pywebpush_send("key", claims,
                            {"endpoint": "https://web.push.apple.com/b"},
                            "{}", "high", 60, 5)

        self.assertEqual(seen, ["https://updates.push.services.mozilla.com",
                                "https://web.push.apple.com"])
        self.assertNotIn("aud", claims)      # the worker's own dict stays clean


if __name__ == "__main__":
    unittest.main()
