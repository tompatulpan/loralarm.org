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
-- PWA accounts: invite tokens are stored hashed only (raw token never at rest);
-- sessions are server-side and revocable; push_ack is the service-worker
-- delivery receipt, keyed by endpoint_id like webpush_log
CREATE TABLE IF NOT EXISTS users (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL UNIQUE,
    token_hash TEXT NOT NULL UNIQUE,     -- sha256 hex of the invite token
    role       TEXT NOT NULL DEFAULT 'user',   -- user | admin (admin may disarm "tills vidare")
    created_at TEXT NOT NULL,
    disabled   INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS sessions (
    id         TEXT PRIMARY KEY,          -- random; this is the cookie value
    user_id    INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE TABLE IF NOT EXISTS push_ack (
    event_id    INTEGER NOT NULL,
    endpoint_id INTEGER NOT NULL,
    ts          TEXT NOT NULL,
    PRIMARY KEY (event_id, endpoint_id)
);
"""


def hash_token(token: str) -> str:
    """sha256 hex of an invite token. Pure function on purpose: callers hash
    *outside* EventLog._lock, so a slow hash never contends with the alarm path."""
    import hashlib
    return hashlib.sha256(token.encode("utf-8")).hexdigest()

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

    # ---- PWA accounts: users, sessions, subscriptions, endpoints, acks ----

    def seed_users(self, users: list[dict]) -> None:
        """Config is the single source of truth for who may exist: upsert every
        listed user (enabled, current token/role), disable the rest. Tokens are
        hashed before the lock is taken. Each entry: {name, token, role}."""
        rows = [(u["name"], hash_token(u["token"]), u.get("role", "user")) for u in users]
        now = self._utc(utc_now())
        with self._lock:
            for name, th, role in rows:
                self._conn.execute(
                    "INSERT INTO users (name, token_hash, role, created_at, disabled) "
                    "VALUES (?, ?, ?, ?, 0) ON CONFLICT(name) DO UPDATE SET "
                    "token_hash=excluded.token_hash, role=excluded.role, disabled=0",
                    (name, th, role, now))
            if rows:
                marks = ",".join("?" for _ in rows)
                self._conn.execute(
                    f"UPDATE users SET disabled = 1 WHERE name NOT IN ({marks})",
                    [r[0] for r in rows])
            else:
                self._conn.execute("UPDATE users SET disabled = 1")
            self._conn.commit()

    def user_by_token_hash(self, token_hash: str) -> dict[str, Any] | None:
        """The user for a valid (enabled) invite token; disabled users are not
        returned — a disabled user is logged out immediately."""
        with self._lock:
            row = self._conn.execute(
                "SELECT id, name, role FROM users WHERE token_hash = ? AND disabled = 0",
                (token_hash,)).fetchone()
        return {"id": row[0], "name": row[1], "role": row[2]} if row else None

    def create_session(self, user_id: int, ttl_hours: float = 24 * 30) -> str:
        import secrets
        sid = secrets.token_hex(32)
        now = utc_now()
        with self._lock:
            self._conn.execute(
                "INSERT INTO sessions (id, user_id, created_at, expires_at, revoked) "
                "VALUES (?, ?, ?, ?, 0)",
                (sid, user_id, self._utc(now),
                 self._utc(now + timedelta(hours=ttl_hours))))
            self._conn.commit()
        return sid

    def resolve_session(self, sid: str) -> dict[str, Any] | None:
        """Session -> user, or None when unknown/revoked/expired/disabled."""
        with self._lock:
            row = self._conn.execute(
                "SELECT u.id, u.name, u.role, s.expires_at, s.revoked, u.disabled "
                "FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.id = ?",
                (sid,)).fetchone()
        if not row:
            return None
        if row[4] or row[5]:
            return None
        if row[3] <= self._utc(utc_now()):
            return None
        return {"id": row[0], "name": row[1], "role": row[2]}

    def revoke_session(self, sid: str) -> None:
        with self._lock:
            self._conn.execute("UPDATE sessions SET revoked = 1 WHERE id = ?", (sid,))
            self._conn.commit()

    def revoke_user_sessions(self, user_id: int) -> None:
        with self._lock:
            self._conn.execute("UPDATE sessions SET revoked = 1 WHERE user_id = ?", (user_id,))
            self._conn.commit()

    def get_subs(self, user_id: int) -> list[str] | None:
        """The user's device set; None = not chosen yet = follow all (default)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT dev_eui FROM device_subs WHERE user_id = ?", (user_id,)).fetchall()
        return [r[0] for r in rows] if rows else None

    def set_subs(self, user_id: int, dev_euis: list[str]) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM device_subs WHERE user_id = ?", (user_id,))
            self._conn.executemany(
                "INSERT INTO device_subs (user_id, dev_eui) VALUES (?, ?)",
                [(user_id, e) for e in dev_euis])
            self._conn.commit()

    def add_endpoint(self, user_id: int, endpoint: str, p256dh: str, auth: str,
                     ua: str = "") -> int:
        """Register a Web Push subscription; re-registering the same endpoint
        refreshes its keys (a second browser on the same account)."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO push_endpoints (user_id, endpoint, p256dh, auth, ua, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(endpoint) DO UPDATE SET "
                "user_id=excluded.user_id, p256dh=excluded.p256dh, auth=excluded.auth, "
                "ua=excluded.ua, fail_count=0",
                (user_id, endpoint, p256dh, auth, ua, self._utc(utc_now())))
            self._conn.commit()
            return self._conn.execute(
                "SELECT id FROM push_endpoints WHERE endpoint = ?", (endpoint,)).fetchone()[0]

    def remove_endpoint(self, endpoint: str) -> bool:
        """Logout: delete this browser's subscription so a second user on a
        shared phone does not inherit the first user's alarms."""
        with self._lock:
            cur = self._conn.execute("DELETE FROM push_endpoints WHERE endpoint = ?",
                                     (endpoint,))
            self._conn.commit()
            return cur.rowcount > 0

    def log_push_ack(self, event_id: int, endpoint_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO push_ack (event_id, endpoint_id, ts) VALUES (?, ?, ?)",
                (event_id, endpoint_id, self._utc(utc_now())))
            self._conn.commit()
