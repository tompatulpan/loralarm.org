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


class WebPushError(Exception):
    """A push service answered with an error status (e.g. 404/410 = prune)."""

    def __init__(self, status: int):
        super().__init__(f"push service status {status}")
        self.status = status


def _pywebpush_send(vapid_private_key: str, vapid_claims: dict,
                    subscription_info: dict, data: str, urgency: str,
                    ttl: int, timeout: float) -> int:
    """Default send_fn: pywebpush (optional dependency). Returns the HTTP status."""
    from pywebpush import WebPushException, webpush
    try:
        resp = webpush(subscription_info=subscription_info, data=data,
                       vapid_private_key=vapid_private_key, vapid_claims=vapid_claims,
                       headers={"Urgency": urgency}, ttl=ttl, timeout=timeout)
        return getattr(resp, "status_code", 201) or 201
    except WebPushException as e:
        status = e.response.status_code if e.response is not None else 0
        raise WebPushError(status) from e


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
        if not dry_run and send_fn is None and not vapid_private_key:
            log.warning("Web Push has no VAPID private key "
                        "(LORALARM_VAPID_PRIVATE_KEY); every push will fail")

    def start(self) -> None:
        if self._worker is None:
            self._worker = threading.Thread(target=self._run, name="loralarm-webpush",
                                            daemon=True)
            self._worker.start()

    def close(self) -> None:
        self._closed = True
        if self._worker is not None:
            self._queue.put(None)
            self._worker.join(timeout=30)
            self._worker = None

    def fan_out(self, cfg, kind: str, active: bool, title: str, message: str,
                event_id: int | None) -> None:
        """Enqueue one alarm transition for the device's followers. Never blocks."""
        if self._worker is not None and not self._closed:
            self._queue.put({"cfg": cfg, "kind": kind, "active": active,
                             "title": title, "message": message, "event_id": event_id})
        else:
            self._deliver({"cfg": cfg, "kind": kind, "active": active,
                           "title": title, "message": message, "event_id": event_id})

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
        if self._worker is not None and not self._closed:
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
                # push problem must never touch the alarm path
                log.exception("webpush worker failed for %s %s",
                              job["cfg"].dev_eui, job["kind"])
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
            user_ids = self.event_log.subscribers(cfg.dev_eui)
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
                self._record(event_id, ep, "failed", e.status)
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
                code: int | None) -> None:
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
                    log.warning("push to endpoint #%d failed (status %d)", ep["id"], code)
            self.event_log.webpush_log(event_id, ep["id"], status, code)
        except Exception:
            log.exception("webpush bookkeeping failed for endpoint #%d", ep["id"])

    @staticmethod
    def _is_missing_dependency(e: BaseException) -> bool:
        return isinstance(e, ImportError) and "pywebpush" in str(e)
