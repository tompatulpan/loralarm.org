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

    def test_muted_device_is_not_pushed_to_that_user(self):
        add_endpoint(self.log, 1, "https://push.example/aaa")
        self.log.set_muted(1, [DEV_EUI])
        calls = []
        w = self.make_worker(lambda *a: calls.append(a) or 201)
        w.fan_out(self.cfg, "motion", True, "t", "m", 1)
        self.drain(w)
        self.assertEqual(calls, [])

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


if __name__ == "__main__":
    unittest.main()
