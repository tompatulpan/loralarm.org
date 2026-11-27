"""Web Push channel for the PWA (PWA design §5/§6): its own queue and worker
thread, fully separate from the ntfy/Pushover worker — a slow or dead push
endpoint must never delay an alarm notification on the other channels.

pywebpush is an optional dependency (requirements-web.txt): without it the
worker logs and drops jobs, the alarm path is unaffected. Delivery is logged
to webpush_log by endpoint_id only; the raw endpoint is a secret and is never
logged. Endpoints are pruned on HTTP 404/410 (gone for good).

Payload: {"title", "body", "dev_eui", "kind", "active", "event_id"} — the same
Swedish text as the other channels; the service worker uses tag=dev_eui:kind
so a clear replaces its fire notification, and reports delivery via push_ack.
"""
from __future__ import annotations

import json
import logging
import queue
import threading
from typing import Any, Callable

from .events import EventLog

log = logging.getLogger(__name__)

PUSH_TTL_S = 3600  # a fire push arriving hours later is noise


def _job_label(job: Any) -> str:
    """Name a queue item for a log line without assuming its shape: alarm jobs
    carry `cfg`/`kind`, test jobs carry neither."""
    if not isinstance(job, dict):
        return "unknown job"
    if job.get("test"):
        return "test push"
    cfg = job.get("cfg")
    return f"{getattr(cfg, 'dev_eui', '?')} {job.get('kind', '?')}"


class WebPushError(Exception):
    """A push service answered with an error status (e.g. 404/410 = prune).

    `body` is the service's own explanation (Apple answers 403 with
    `{"reason": "BadJwtToken"}` etc.); it goes to the journal, never to the DB,
    because it is the difference between "403, guess again" and a diagnosis.
    """

    def __init__(self, status: int, body: str = ""):
        super().__init__(f"push service status {status}")
        self.status = status
        self.body = (body or "").strip()


def _pywebpush_send(vapid_private_key: str, vapid_claims: dict,
                    subscription_info: dict, data: str, urgency: str,
                    ttl: int, timeout: float) -> int:
    """Default send_fn: pywebpush (optional dependency). Returns the HTTP status.

    pywebpush writes `aud` and `exp` into the claims dict it receives, and only
    when they are missing (pywebpush/__init__.py: `vapid_claims["aud"] = aud`).
    The worker keeps one dict for the process, so handing over the live dict
    pins the first push service's audience and every later push to another
    service is rejected as a bad JWT — Apple answers 403 BadJwtToken while
    Mozilla, contacted first, keeps working. Each send therefore gets its own
    copy: the audience is per endpoint, not per process.
    """
    from pywebpush import WebPushException, webpush
    try:
        resp = webpush(subscription_info=subscription_info, data=data,
                       vapid_private_key=vapid_private_key,
                       vapid_claims=dict(vapid_claims),
                       headers={"Urgency": urgency}, ttl=ttl, timeout=timeout)
        return getattr(resp, "status_code", 201) or 201
    except WebPushException as e:
        status = e.response.status_code if e.response is not None else 0
        body = getattr(e.response, "text", "") if e.response is not None else ""
        raise WebPushError(status, body[:300]) from e


class WebPushWorker:
    """fan_out() only enqueues; one worker thread resolves the device's
    followers -> endpoints and sends, with per-endpoint bookkeeping."""

    def __init__(self, event_log: EventLog, vapid_private_key: str | None = None,
                 vapid_claims: dict | None = None, dry_run: bool = False,
                 timeout: float = 10.0,
                 send_fn: Callable[..., int] | None = None):
        self.event_log = event_log
        self.vapid_private_key = vapid_private_key
        self.vapid_claims = vapid_claims or {"sub": "mailto:admin@loralarm.org"}
        self.dry_run = dry_run
        self.timeout = timeout
        self._send_fn = send_fn or (lambda si, data, urgency, ttl, timeout:
                                    _pywebpush_send(vapid_private_key, self.vapid_claims,
                                                    si, data, urgency, ttl, timeout))
        self._queue: queue.Queue = queue.Queue()
        self._worker: threading.Thread | None = None
        self._closed = False
        self._no_pywebpush_warned = False
        self._stale_reported: set[int] = set()   # endpoints already reported this run
        if not dry_run and send_fn is None and not vapid_private_key:
            log.warning("Web Push has no VAPID private key "
                        "(LORALARM_VAPID_PRIVATE_KEY); every push will fail")

    def start(self) -> None:
        if self._worker is None:
            self._worker = threading.Thread(target=self._run, name="loralarm-webpush",
                                            daemon=True)
            self._worker.start()

    def _ensure_worker(self) -> bool:
        """True when a live worker drains the queue. A thread that died (a bug
        in the per-job handler, e.g. the KeyError that a test job without "cfg"
        used to cause) would otherwise leave every later push queued forever —
        silent, no log line, no delivery, until the service restarts."""
        if self._closed:
            return False
        if self._worker is None:
            self.start()
        elif not self._worker.is_alive():
            log.error("webpush worker thread is gone; starting a new one "
                      "(queued pushes are kept)")
            self._worker = None
            self.start()
        return True

    def report_stale(self, days: float = 30.0) -> int:
        """Log endpoints that are delivered to but never acknowledge. The push
        service answers 201 regardless, so nothing else notices: on iOS the
        notification is either suppressed before the service worker runs (the
        device's Focus mode does not allow the app, or notifications are off) or
        the installation is gone and the subscription outlives it. Report only —
        the row stays until the device re-subscribes or the service answers
        404/410. Once per endpoint per process, so the watchdog may call this
        every few minutes."""
        try:
            rows = self.event_log.unacked_endpoints(days)
        except Exception:
            log.exception("could not check for unacknowledged push endpoints")
            return 0
        fresh = [r for r in rows if r["id"] not in self._stale_reported]
        for r in fresh:
            self._stale_reported.add(r["id"])
            log.warning("push endpoint #%d (%s): %d push(es) delivered, no receipt "
                        "in %.0f days — check the device's Focus mode and "
                        "notification permission, and whether push needs enabling "
                        "again", r["id"], r["name"], r["deliveries"], days)
            try:
                self.event_log.webpush_log(None, r["id"], "stale")
            except Exception:
                log.exception("could not record the stale endpoint #%d", r["id"])
        return len(fresh)

    def close(self) -> None:
        self._closed = True
        if self._worker is not None:
            self._queue.put(None)
            self._worker.join(timeout=30)
            self._worker = None

    def fan_out(self, cfg, kind: str, active: bool, title: str, message: str,
                event_id: int | None) -> None:
        """Enqueue one alarm transition for the device's followers. Never blocks."""
        job = {"cfg": cfg, "kind": kind, "active": active,
               "title": title, "message": message, "event_id": event_id}
        if self._ensure_worker():
            self._queue.put(job)
        else:
            self._deliver(job)

    def test_push(self, user_id: int, title: str, body: str) -> int:
        """Test notification to one user's own endpoints (PWA settings page),
        regardless of which devices they follow. Returns the endpoint count."""
        try:
            endpoints = self.event_log.endpoints_for_users([user_id])
        except Exception:
            log.exception("could not resolve endpoints for test push")
            endpoints = []
        job = {"test": True, "endpoints": endpoints, "title": title,
               "body": body, "event_id": None}
        if self._ensure_worker():
            self._queue.put(job)
        else:
            self._deliver(job)
        return len(endpoints)

    def _run(self) -> None:
        while True:
            job = self._queue.get()
            try:
                if job is None:
                    return
                self._deliver(job)
            except Exception:
                # last resort: the worker thread must never die silently, and a
                # push problem must never touch the alarm path. The label must
                # not assume the job's shape — a test job has no "cfg", and
                # raising in here is what used to kill the thread for good.
                log.exception("webpush worker failed for %s", _job_label(job))
            finally:
                self._queue.task_done()

    def _deliver(self, job: dict[str, Any]) -> None:
        if job.get("test"):
            payload = json.dumps({"title": job["title"], "body": job["body"],
                                  "dev_eui": "", "kind": "test", "active": True,
                                  "event_id": job["event_id"]})
            self._send_to_endpoints(payload, "high", job["event_id"], job["endpoints"])
            return
        cfg = job["cfg"]
        try:
            user_ids = self.event_log.subscribers(cfg.dev_eui, cfg.group)
        except Exception:
            log.exception("could not resolve subscribers for %s", cfg.dev_eui)
            return
        if not user_ids:
            log.warning("device %s (%s) has no followers; alarm not pushed",
                        cfg.name, cfg.dev_eui)
        try:
            endpoints = self.event_log.endpoints_for_users(user_ids)
        except Exception:
            log.exception("could not resolve push endpoints for %s", cfg.dev_eui)
            return
        if user_ids and not endpoints:
            # every follower of this device has push switched off, or none of
            # them has a browser subscribed: nothing to send, and without this
            # line the skip is completely silent (looks like "push is broken")
            log.info("device %s (%s): %d follower(s), none with a push endpoint",
                     cfg.name, cfg.dev_eui, len(user_ids))
            return
        payload = json.dumps({"title": job["title"], "body": job["message"],
                              "dev_eui": cfg.dev_eui, "kind": job["kind"],
                              "active": job["active"], "event_id": job["event_id"]})
        urgency = "high" if job["active"] else "normal"   # Android Doze
        self._send_to_endpoints(payload, urgency, job["event_id"], endpoints)

    def _send_to_endpoints(self, payload: str, urgency: str, event_id: int | None,
                           endpoints: list[dict[str, Any]]) -> None:
        for ep in endpoints:
            sub_info = {"endpoint": ep["endpoint"],
                        "keys": {"p256dh": ep["p256dh"], "auth": ep["auth"]}}
            if self.dry_run:
                log.info("[dry-run] webpush endpoint #%d: %s", ep["id"], payload)
                self.event_log.webpush_log(event_id, ep["id"], "dry-run")
                continue
            try:
                code = self._send_fn(sub_info, payload, urgency, PUSH_TTL_S, self.timeout)
            except WebPushError as e:
                self._record(event_id, ep, "failed", e.status, e.body)
            except Exception as e:
                if self._is_missing_dependency(e):
                    if not self._no_pywebpush_warned:
                        log.error("pywebpush is not installed; Web Push disabled "
                                  "(pip install -r requirements-web.txt)")
                        self._no_pywebpush_warned = True
                    return
                log.exception("push to endpoint #%d failed", ep["id"])
                self._record(event_id, ep, "failed", None)
            else:
                self._record(event_id, ep, "sent", code)

    def _record(self, event_id: int | None, ep: dict[str, Any], status: str,
                code: int | None, detail: str = "") -> None:
        """Per-endpoint bookkeeping; a DB error here must not stop delivery to
        the remaining endpoints."""
        try:
            if status == "sent":
                self.event_log.endpoint_ok(ep["id"])
            else:
                if self.event_log.endpoint_failed(ep["id"], code):
                    status = "pruned"
                    log.info("pruned push endpoint #%d (status %s)", ep["id"], code)
                elif code is not None:
                    log.warning("push to endpoint #%d failed (status %s)%s",
                                ep["id"], code, f": {detail}" if detail else "")
            self.event_log.webpush_log(event_id, ep["id"], status, code)
        except Exception:
            log.exception("webpush bookkeeping failed for endpoint #%d", ep["id"])

    @staticmethod
    def _is_missing_dependency(e: BaseException) -> bool:
        return isinstance(e, ImportError) and "pywebpush" in str(e)
