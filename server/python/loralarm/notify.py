"""Alarm notifications, one function / two channels (migration spec §7.5):
ntfy first (self-hosted), Pushover per-device when `pushover_user` /
`pushover_token` are configured. stdlib only - no httpx dependency for this.
"""
from __future__ import annotations

import urllib.parse
import urllib.request

from .alarms import LABELS
from .config import DeviceConfig

PUSHOVER_API = "https://api.pushover.net/1/messages.json"

# Example notification texts per alarm kind, fire / clear: {name} and {detail}
# are substituted (detail carries the measured values, e.g. "3.57 V ≤ warn 3.5 V").
# Keep in sync with alarms.LABELS when adding a kind; tuned during cutover
# (checklist step 9) until the phone shows the same as Node-RED did.
EXAMPLE_TEXT: dict[str, tuple[str, str]] = {
    "temp":          ("❄️ TEMPERATURALARM: {name} — {detail}",
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
                      "✔️ Kontakt åter: {name} — {detail}"),
}


def message_text(kind: str, name: str, active: bool, detail: str,
                 fire: dict | None = None, clear: dict | None = None) -> str:
    """Config templates (per kind) win over the EXAMPLE_TEXT built-ins.
    Available substitutions: {name} {detail} {label}."""
    templates = (fire or {}) if active else (clear or {})
    template = templates.get(kind)
    if not template:
        fire_tpl, clear_tpl = EXAMPLE_TEXT.get(kind, ("", ""))
        template = fire_tpl if active else clear_tpl
    if not template:
        state = "LARM" if active else "kvitterad"
        return f"{name}: {LABELS.get(kind, kind)} {state} — {detail}"
    return template.format(name=name, detail=detail, label=LABELS.get(kind, kind))


class Notifier:
    def __init__(self, ntfy_url: str = "https://ntfy.sh", dry_run: bool = False,
                 timeout: float = 10.0):
        self.ntfy_url = ntfy_url.rstrip("/")
        self.dry_run = dry_run
        self.timeout = timeout

    def notify(self, cfg: DeviceConfig, kind: str, active: bool, detail: str) -> None:
        name = cfg.name or cfg.dev_eui
        message = message_text(kind, name, active, detail,
                               cfg.notify_fire, cfg.notify_clear)
        title = cfg.notify_title.format(name=name, kind=kind, detail=detail)

        if self.dry_run:
            print(f"    [dry-run] ntfy {cfg.ntfy_topic}: {message}")
        else:
            self._ntfy(cfg.ntfy_topic, title, message, active)

        if cfg.pushover_user and cfg.pushover_token:
            if self.dry_run:
                print(f"    [dry-run] pushover {name}: {message}")
            else:
                self._pushover(cfg.pushover_token, cfg.pushover_user,
                               title, message, active)

    def _ntfy(self, topic: str, title: str, message: str, active: bool) -> None:
        # same shape as the Node-RED flows: plain-text body, metadata in headers
        # (ntfy JSON bodies render as raw metadata on some clients)
        req = urllib.request.Request(
            f"{self.ntfy_url}/{topic}", data=message.encode("utf-8"),
            headers={
                "Content-Type": "text/plain; charset=utf-8",
                "Title": title,
                "Tags": "rotating_light" if active else "white_check_mark",
                "Priority": "5" if active else "3",
            })
        try:
            urllib.request.urlopen(req, timeout=self.timeout).read()
        except OSError as e:
            print(f"    [ntfy error] topic {topic}: {e}", flush=True)

    def _pushover(self, token: str, user: str, title: str, message: str,
                  active: bool) -> None:
        data = urllib.parse.urlencode({
            "token": token, "user": user, "title": title, "message": message,
            "priority": 1 if active else 0,
        }).encode()
        req = urllib.request.Request(PUSHOVER_API, data=data)
        try:
            urllib.request.urlopen(req, timeout=self.timeout).read()
        except OSError as e:
            print(f"    [pushover error] {e}", flush=True)
