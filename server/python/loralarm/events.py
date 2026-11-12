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
    detail      TEXT NOT NULL DEFAULT '',
    user        TEXT NOT NULL DEFAULT ''
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
-- PWA phase: per-device arm/disarm (survives config hot-reload), per-user device
-- subscriptions, Web Push endpoints (endpoint is a secret: never logged) and the
-- delivery log, referenced by endpoint_id only
CREATE TABLE IF NOT EXISTS device_armed (
    dev_eui     TEXT PRIMARY KEY,
    armed       INTEGER NOT NULL DEFAULT 1,
    until       TEXT,                    -- UTC iso, snooze expiry; NULL = no expiry
    changed_by  TEXT NOT NULL DEFAULT '',
    changed_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS device_subs (
    user_id INTEGER NOT NULL,
    dev_eui TEXT NOT NULL,
    PRIMARY KEY (user_id, dev_eui)
);
CREATE TABLE IF NOT EXISTS push_endpoints (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL,
    endpoint     TEXT NOT NULL UNIQUE,
    p256dh       TEXT NOT NULL,
    auth         TEXT NOT NULL,
    ua           TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL,
    last_success TEXT,
    fail_count   INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS webpush_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id    INTEGER,                 -- events.id; NULL when that write failed
    endpoint_id INTEGER NOT NULL,        -- never the raw endpoint
    status      TEXT NOT NULL,           -- sent | failed | pruned | dry-run
    http_code   INTEGER,
    ts          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_webpush_log_event ON webpush_log(event_id);
"""

_INSERT = ("INSERT INTO events (ts, dev_eui, device_name, kind, active, detail, user) "
           "VALUES (?, ?, ?, ?, ?, ?, ?)")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class EventLog:
    def __init__(self, path: str | None):
        # ':memory:' keeps replay/tests dependency-free; the service passes a file
        self._conn = sqlite3.connect(path or ":memory:", check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            # migrate DBs created before the user column existed
            cols = {r[1] for r in self._conn.execute("PRAGMA table_info(events)")}
            if "user" not in cols:
                self._conn.execute("ALTER TABLE events ADD COLUMN user TEXT NOT NULL DEFAULT ''")
            self._conn.commit()

    @staticmethod
    def _utc(ts: datetime) -> str:
        # naive input is taken as UTC; tz-aware input is converted.
        # timespec="microseconds" keeps the width fixed so text ordering works.
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts.astimezone(timezone.utc).isoformat(timespec="microseconds")

    def log(self, ts: datetime, dev_eui: str, device_name: str,
            kind: str, active: bool, detail: str = "", user: str = "") -> int:
        """Append one row; returns its event id (lastrowid) for the delivery
        log of the web-push phase. Raises sqlite3.Error on failure — the caller
        decides whether that may suppress a notification (it must not)."""
        with self._lock:
            cur = self._conn.execute(_INSERT, (self._utc(ts), dev_eui, device_name,
                                               kind, int(active), detail, user))
            self._conn.commit()
            return int(cur.lastrowid)

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

    def last_name(self, dev_eui: str) -> str:
        """Most recent non-empty device name logged for a device, or ''."""
        with self._lock:
            row = self._conn.execute(
                "SELECT device_name FROM events WHERE dev_eui = ? AND device_name != '' "
                "ORDER BY id DESC LIMIT 1", (dev_eui,)).fetchone()
        return row[0] if row else ""

    def known_devices(self) -> list[str]:
        """devEUIs that have a saved state snapshot (seen by an earlier run)."""
        with self._lock:
            return [r[0] for r in self._conn.execute("SELECT dev_eui FROM device_state")]

    def load_state(self, dev_eui: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT snapshot FROM device_state WHERE dev_eui = ?", (dev_eui,)).fetchone()
        return json.loads(row[0]) if row else None

    # ---- PWA: arm/disarm, subscriptions, Web Push (all short queries; callers
    # hash tokens etc. outside this lock) ----

    def get_armed(self, dev_eui: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT armed, until, changed_by FROM device_armed WHERE dev_eui = ?",
                (dev_eui,)).fetchone()
        return {"armed": bool(row[0]), "until": row[1], "changed_by": row[2]} if row else None

    def set_armed(self, dev_eui: str, armed: bool, until: datetime | None,
                  changed_by: str = "") -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO device_armed (dev_eui, armed, until, changed_by, changed_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(dev_eui) DO UPDATE SET armed=excluded.armed, until=excluded.until, "
                "changed_by=excluded.changed_by, changed_at=excluded.changed_at",
                (dev_eui, int(armed), self._utc(until) if until else None,
                 changed_by, self._utc(utc_now())))
            self._conn.commit()

    def expired_snoozes(self, now: datetime) -> list[str]:
        """Devices disarmed with a snooze `until` that has passed (fixed-width
        UTC text compares correctly)."""
        with self._lock:
            return [r[0] for r in self._conn.execute(
                "SELECT dev_eui FROM device_armed "
                "WHERE armed = 0 AND until IS NOT NULL AND until <= ?",
                (self._utc(now),))]

    def subscribers(self, dev_eui: str) -> list[int]:
        with self._lock:
            return [r[0] for r in self._conn.execute(
                "SELECT user_id FROM device_subs WHERE dev_eui = ?", (dev_eui,))]

    def endpoints_for_users(self, user_ids: list[int]) -> list[dict[str, Any]]:
        if not user_ids:
            return []
        with self._lock:
            marks = ",".join("?" for _ in user_ids)
            return [{"id": r[0], "endpoint": r[1], "p256dh": r[2], "auth": r[3]}
                    for r in self._conn.execute(
                        f"SELECT id, endpoint, p256dh, auth FROM push_endpoints "
                        f"WHERE user_id IN ({marks})", user_ids)]

    def webpush_log(self, event_id: int | None, endpoint_id: int, status: str,
                    http_code: int | None = None) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO webpush_log (event_id, endpoint_id, status, http_code, ts) "
                "VALUES (?, ?, ?, ?, ?)",
                (event_id, endpoint_id, status, http_code, self._utc(utc_now())))
            self._conn.commit()

    def endpoint_ok(self, endpoint_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE push_endpoints SET fail_count = 0, last_success = ? WHERE id = ?",
                (self._utc(utc_now()), endpoint_id))
            self._conn.commit()

    def endpoint_failed(self, endpoint_id: int, http_code: int | None) -> bool:
        """Record a failed delivery; prunes the endpoint on 404/410 (gone for
        good). Returns True when the endpoint was pruned."""
        with self._lock:
            if http_code in (404, 410):
                self._conn.execute("DELETE FROM push_endpoints WHERE id = ?", (endpoint_id,))
                self._conn.commit()
                return True
            self._conn.execute(
                "UPDATE push_endpoints SET fail_count = fail_count + 1 WHERE id = ?",
                (endpoint_id,))
            self._conn.commit()
            return False
