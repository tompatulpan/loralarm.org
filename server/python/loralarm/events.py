"""Alarm events log (migration spec §7.6): every fire/clear, settings frame and
later downlink gets one row. SQLite first; Postgres/Timescale only if fleet
dashboards ever need it. Thread-safe: MQTT callbacks and the watchdog tick
run in different threads.
"""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timedelta, timezone

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
CREATE INDEX IF NOT EXISTS idx_events_id_desc ON events(id DESC);
"""

_INSERT = ("INSERT INTO events (ts, dev_eui, device_name, kind, active, detail) "
           "VALUES (?, ?, ?, ?, ?, ?)")


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
        # naive input is taken as UTC; tz-aware input is converted
        return (ts.astimezone(timezone.utc) if ts.tzinfo else ts).isoformat()

    def log(self, ts: datetime, dev_eui: str, device_name: str,
            kind: str, active: bool, detail: str = "") -> None:
        with self._lock:
            self._conn.execute(_INSERT, (self._utc(ts), dev_eui, device_name,
                                         kind, int(active), detail))
            self._conn.commit()

    def recent(self, hours: float = 24.0, limit: int = 500) -> list[tuple]:
        since = datetime.utcnow() - timedelta(hours=hours)
        with self._lock:
            return self._conn.execute(
                "SELECT ts, device_name, kind, active, detail FROM events "
                "WHERE ts >= ? ORDER BY id DESC LIMIT ?",
                (since.isoformat(), limit)).fetchall()
