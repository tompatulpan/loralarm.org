"""Alarm notifications, one function / two channels (migration spec §7.5):
ntfy first (self-hosted), Pushover per-device when `pushover_user` /
`pushover_token` are configured. stdlib only - no httpx dependency for this.

Delivery is asynchronous: `notify()` only enqueues; a single worker thread
sends with retries, so a slow ntfy/Pushover endpoint can never stall the MQTT
callback thread or the keepalive. Every attempt is logged (and would be the
place to record delivery status in the events DB).
"""
from __future__ import annotations

import logging
import queue
import threading
import time
import urllib.parse
import urllib.request
from email.header import Header
from typing import Any

from .config import DeviceConfig
from .registry import Registry


def _default_registry() -> Registry:
    from .definitions import builtin_registry   # lazy: definitions imports config
    return builtin_registry()

log = logging.getLogger(__name__)

PUSHOVER_API = "https://api.pushover.net/1/messages.json"

# Built-in notification texts per alarm kind, fire / clear: {name}, {detail}
# and {label} are substituted (detail carries the measured values, e.g.
# "3.57 V ≤ varningsgräns 3.5 V"). Config templates win over these built-ins.
# A new kind needs no entry here: alarms.AlarmRule can carry notify_fire /
# notify_clear, else a generic text is used. Tuned during cutover
# (checklist step 9) until the phone shows the same as Node-RED did.
DEFAULT_TEXT: dict[str, tuple[str, str]] = {
    "temp_low":      ("❄️ LÅG TEMPERATUR: {name} — {detail}",
                      "🌡️ Temperatur OK: {name} — {detail}"),
    "temp_high":     ("🔥 HÖG TEMPERATUR: {name} — {detail}",
                      "🌡️ Temperatur OK: {name} — {detail}"),
    "battery_low":   ("🔋 BATTERI LÅGT: {name} — {detail}",
                      "💚 Batteri OK: {name} — {detail}"),
    "battery_empty": ("🪫 BATTERI TOMT: {name} — {detail}",
                      "🔌 Batteri OK: {name} — {detail}"),
    "motion":       ("🚨 RÖRELSELARM: {name} — {detail}",
                     "✔️ Rörelse kvitterad: {name} — {detail}"),
    "circuit":       ("⚠️ KRETSALARM (öppen krets): {name} — {detail}",
                      "✔️ Krets sluten: {name} — {detail}"),
    "tilt":         ("📐 TILTLARM: {name} — {detail}",
                     "✔️ Tilt kvitterad: {name} — {detail}"),
    "stale":         ("📵 INGEN KONTAKT: {name} — {detail}",
                      "✔️ Kommunikationen med {name} är återupptagen"),
    # PWA arm/disarm audit notification: active=True means "larm på" again
    "armed":         ("🛡️ LARM PÅ: {name} — {detail}",
                      "🛡️ LARM AV: {name} — {detail}"),
}

# kinds that must escalate to Pushover emergency priority (2), which requires
# retry/expire parameters; Pushover stays as backstop for these indefinitely
CRITICAL_KINDS = frozenset({"temp_low", "temp_high", "motion", "tilt", "circuit", "stale"})

# backwards-compatible alias; the texts are the live defaults, not an example
EXAMPLE_TEXT = DEFAULT_TEXT


def _safe_format(template: str, what: str, **fields: str) -> str | None:
    """str.format that returns None (and logs) on a bad template instead of raising."""
    try:
        return template.format(**fields)
    except (KeyError, IndexError, ValueError) as e:
        log.error("bad notification %s %r (%s: %s); using built-in text",
                  what, template, type(e).__name__, e)
        return None


def message_text(kind: str, name: str, active: bool, detail: str,
                 fire: dict | None = None, clear: dict | None = None,
                 registry: Registry | None = None) -> str:
    """Config templates (per kind) win over the DEFAULT_TEXT built-ins; a broken
    template falls back to the built-in so an alarm is never lost to a typo.
    Available substitutions: {name} {detail} {label}."""
    registry = registry or _default_registry()
    label = registry.label_for(kind)
    fields = dict(name=name, detail=detail, label=label)
    templates = (fire or {}) if active else (clear or {})
    template = templates.get(kind)
    if template:
        text = _safe_format(template, f"template for {kind}", **fields)
        if text is not None:
            return text
    fire_tpl, clear_tpl = DEFAULT_TEXT.get(kind, ("", ""))
    template = fire_tpl if active else clear_tpl
    if template:
        return template.format(**fields)
    rule = registry.rule_for(kind)       # a registered rule may carry its own texts
    template = (rule.notify_fire if active else rule.notify_clear) if rule else ""
    if template:
        text = _safe_format(template, f"rule text for {kind}", **fields)
        if text is not None:
            return text
    state = "LARM" if active else "kvitterad"
    return f"{name}: {label} {state} — {detail}"


def encode_header(value: str) -> str:
    """RFC 2047-encode a header value so non-ASCII titles survive http.client
    (which requires latin-1-encodable header bytes)."""
    return Header(value, "utf-8").encode()


class Notifier:
    """Queued notifier: notify() enqueues, one worker thread delivers with retry.

    Without start() (replay, tests) notify() sends inline, so behavior stays
    synchronous and easy to assert.
    """

    def __init__(self, ntfy_url: str, dry_run: bool = False,
                 timeout: float = 10.0, retries: int = 3,
                 ntfy_token: str | None = None,
                 backoff: tuple[float, ...] = (1.0, 5.0, 30.0),
                 webpush: Any = None):
        self.ntfy_url = ntfy_url.rstrip("/")
        self.dry_run = dry_run
        self.timeout = timeout
        self.retries = retries
        self.backoff = backoff  # seconds to wait before retry 2, 3, ...
        self.ntfy_token = ntfy_token  # Bearer token for self-hosted ntfy with auth
        # Web Push channel: its own queue/worker (webpush.WebPushWorker), so a
        # slow push endpoint can never delay ntfy/Pushover on this worker
        self.webpush = webpush
        self._queue: queue.Queue = queue.Queue()
        self._worker: threading.Thread | None = None
        self._closed = False

    def start(self) -> None:
        if self._worker is None:
            self._worker = threading.Thread(target=self._run, name="loralarm-notify",
                                            daemon=True)
            self._worker.start()

    def close(self) -> None:
        """Stop the worker after it has delivered everything still queued."""
        self._closed = True
        if self._worker is not None:
            self._queue.put(None)
            self._worker.join(timeout=2 * (self.timeout * self.retries + sum(self.backoff)) + 5)
            self._worker = None

    def notify(self, cfg: DeviceConfig, kind: str, active: bool, detail: str,
               registry: Registry | None = None, event_id: int | None = None) -> None:
        # event_id is the events-row id of this transition (None when the DB
        # write failed); the web-push phase logs delivery against it
        if self._worker is not None and not self._closed:
            self._queue.put((cfg, kind, active, detail, registry, event_id))
        else:
            self._deliver(cfg, kind, active, detail, registry, event_id)

    def _run(self) -> None:
        while True:
            job = self._queue.get()
            if job is None:
                return
            cfg, kind, active, detail, registry, event_id = job
            try:
                self._deliver(cfg, kind, active, detail, registry, event_id)
            except Exception:
                # _deliver already catches per-channel errors; this is a last
                # resort so the worker thread can never die silently
                log.exception("notification worker failed for %s %s", cfg.dev_eui, kind)

    def _deliver(self, cfg: DeviceConfig, kind: str, active: bool, detail: str,
                 registry: Registry | None = None, event_id: int | None = None) -> None:
        registry = registry or _default_registry()
        name = cfg.name or cfg.dev_eui
        message = message_text(kind, name, active, detail,
                               cfg.notify_fire, cfg.notify_clear, registry)
        title = _safe_format(cfg.notify_title, "title", name=name, kind=kind,
                             detail=detail, label=registry.label_for(kind)) \
            or f"loralarm {name}"

        if self.webpush is not None:
            # enqueue only — delivery happens on the webpush worker's own thread,
            # so a slow push endpoint can never delay ntfy/Pushover
            self.webpush.fan_out(cfg, kind, active, title, message, event_id)

        if self.dry_run:
            log.info("[dry-run] ntfy %s: %s | %s", cfg.ntfy_topic, title, message)
            if cfg.pushover_user and cfg.pushover_token:
                log.info("[dry-run] pushover %s: %s | %s", name, title, message)
            return

        self._send(self._ntfy, cfg.ntfy_topic, title, message, active,
                   label="ntfy", target=cfg.ntfy_topic)
        if cfg.pushover_user and cfg.pushover_token:
            self._send(self._pushover, cfg.pushover_token, cfg.pushover_user,
                       title, message, active, kind in CRITICAL_KINDS,
                       label="pushover", target=name)

    @staticmethod
    def _sleep(seconds: float) -> None:
        time.sleep(seconds)

    def _send(self, fn, *args, label: str, target: str) -> bool:
        """One channel with retry; returns True on success. Never raises."""
        for attempt in range(1, self.retries + 1):
            try:
                fn(*args)
                log.info("%s delivered to %s (attempt %d)", label, target, attempt)
                return True
            except Exception as e:  # OSError, http.client.HTTPException, UnicodeEncodeError, ...
                log.warning("%s attempt %d/%d to %s failed: %s", label, attempt,
                            self.retries, target, e)
                if attempt < self.retries and self.backoff:
                    self._sleep(self.backoff[min(attempt - 1, len(self.backoff) - 1)])
        log.error("%s FAILED after %d attempts to %s — alarm not delivered",
                  label, self.retries, target)
        return False

    def _ntfy(self, topic: str, title: str, message: str, active: bool) -> None:
        # same shape as the Node-RED flows: plain-text body, metadata in headers
        # (ntfy JSON bodies render as raw metadata on some clients)
        headers = {
            "Content-Type": "text/plain; charset=utf-8",
            "Title": encode_header(title),
            "Tags": "rotating_light" if active else "white_check_mark",
            "Priority": "5" if active else "3",
        }
        if self.ntfy_token:
            headers["Authorization"] = f"Bearer {self.ntfy_token}"
        req = urllib.request.Request(
            f"{self.ntfy_url}/{topic}", data=message.encode("utf-8"),
            headers=headers)
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            resp.read()

    def _pushover(self, token: str, user: str, title: str, message: str,
                  active: bool, critical: bool = False) -> None:
        fields = {
            "token": token, "user": user, "title": title, "message": message,
            "priority": 1 if active else 0,
        }
        if critical and active:
            # emergency priority requires retry/expire (Pushover API)
            fields.update(priority=2, retry=60, expire=3600)
        data = urllib.parse.urlencode(fields).encode()
        req = urllib.request.Request(PUSHOVER_API, data=data)
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            resp.read()
