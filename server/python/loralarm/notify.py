"""Alarm notifications, one function / two channels (migration spec §7.5):
ntfy first (self-hosted), Pushover per-device when `pushover_user` /
`pushover_token` are configured. stdlib only - no httpx dependency for this.
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request

from .alarms import LABELS
from .config import DeviceConfig

PUSHOVER_API = "https://api.pushover.net/1/messages.json"


class Notifier:
    def __init__(self, ntfy_url: str = "https://ntfy.sh", dry_run: bool = False,
                 timeout: float = 10.0):
        self.ntfy_url = ntfy_url.rstrip("/")
        self.dry_run = dry_run
        self.timeout = timeout

    def notify(self, cfg: DeviceConfig, kind: str, active: bool, detail: str) -> None:
        name = cfg.name or cfg.dev_eui
        state = "LARM" if active else "kvitterad"
        message = f"{name}: {LABELS.get(kind, kind)} {state} — {detail}"

        if self.dry_run:
            print(f"    [dry-run] ntfy {cfg.ntfy_topic}: {message}")
        else:
            self._ntfy(cfg.ntfy_topic, f"loralarm {name}", message, active)

        if cfg.pushover_user and cfg.pushover_token:
            if self.dry_run:
                print(f"    [dry-run] pushover {name}: {message}")
            else:
                self._pushover(cfg.pushover_token, cfg.pushover_user,
                               f"loralarm {name}", message, active)

    def _ntfy(self, topic: str, title: str, message: str, active: bool) -> None:
        body = json.dumps({
            "title": title,
            "message": message,
            "tags": ["rotating_light"] if active else ["white_check_mark"],
            "priority": "urgent" if active else "default",
        }).encode()
        req = urllib.request.Request(f"{self.ntfy_url}/{topic}", data=body,
                                     headers={"Content-Type": "application/json"})
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
