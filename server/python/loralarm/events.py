"""Alarm events log (migration spec §7.6): every fire/clear, settings frame and
later downlink gets one row. SQLite first; Postgres/Timescale only if fleet
dashboards ever need it. Thread-safe: MQTT callbacks and the watchdog tick
run in different threads.

Timestamps are stored as fixed-width UTC strings
(`...T15:33:14.527766+00:00`, always with microseconds and an explicit UTC
offset) so text comparison in SQL orders rows correctly. `device_state` keeps
the per-device alarm snapshot (active kinds, last_seen, tilt baseline) so a
restart can seed the watchdog instead of starting blind.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    dev_eui     TEXT NOT NULL,
    device_name TEXT NOT NULL DEFAULT '',
    kind        TEXT NOT NULL,
    active      INTEGER NOT NULL,
    detail      TEXT NOT NULL DEFAULT ''
);
-- the AUTOINCREMENT primary key already indexes id; the query pattern is
-- "recent events", so the useful index is on ts
DROP INDEX IF EXISTS idx_events_id_desc;
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE TABLE IF NOT EXISTS device_state (
    dev_eui  TEXT PRIMARY KEY,
    snapshot TEXT NOT NULL,
    updated  TEXT NOT NULL
);
"""

_INSERT = ("INSERT INTO events (ts, dev_eui, device_name, kind, active, detail) "
           "VALUES (?, ?, ?, ?, ?, ?)")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class EventLog:
    def __init__(self, path: str | None):
        # ':memory:' keeps replay/tests dependency-free; the service passes a file
        self._conn = sqlite3.connect(path or ":memory:", check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    @staticmethod
    def _utc(ts: datetime) -> str:
        # naive input is taken as UTC; tz-aware input is converted.
        # timespec="microseconds" keeps the width fixed so text ordering works.
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts.astimezone(timezone.utc).isoformat(timespec="microseconds")

    def log(self, ts: datetime, dev_eui: str, device_name: str,
            kind: str, active: bool, detail: str = "") -> None:
        with self._lock:
            self._conn.execute(_INSERT, (self._utc(ts), dev_eui, device_name,
                                         kind, int(active), detail))
            self._conn.commit()

    def recent(self, hours: float = 24.0, limit: int = 500) -> list[tuple]:
        since = utc_now() - timedelta(hours=hours)
        with self._lock:
            return self._conn.execute(
                "SELECT ts, device_name, kind, active, detail FROM events "
                "WHERE ts >= ? ORDER BY id DESC LIMIT ?",
                (self._utc(since), limit)).fetchall()

    def last_seen(self, dev_eui: str) -> datetime | None:
        """Most recent event timestamp for a device, or None."""
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(ts) FROM events WHERE dev_eui = ?", (dev_eui,)).fetchone()
        if not row or not row[0]:
            return None
        return datetime.fromisoformat(row[0])

    def save_state(self, dev_eui: str, snapshot: dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO device_state (dev_eui, snapshot, updated) VALUES (?, ?, ?) "
                "ON CONFLICT(dev_eui) DO UPDATE SET snapshot=excluded.snapshot, updated=excluded.updated",
                (dev_eui, json.dumps(snapshot), self._utc(utc_now())))
            self._conn.commit()

    def known_devices(self) -> list[str]:
        """devEUIs that have a saved state snapshot (seen by an earlier run)."""
        with self._lock:
            return [r[0] for r in self._conn.execute("SELECT dev_eui FROM device_state")]

    def load_state(self, dev_eui: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT snapshot FROM device_state WHERE dev_eui = ?", (dev_eui,)).fetchone()
        return json.loads(row[0]) if row else None
