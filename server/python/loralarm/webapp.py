"""FastAPI web app for the PWA / status page, run in a uvicorn thread inside the
service process (PWA design §2/§5). The alarm path is never touched from here:
engine state is read only through the lock-protected `Engine.snapshot()`, and
all persistence goes through `EventLog` (short queries; tokens are hashed before
its lock is taken). API exceptions are caught and logged — they can never reach
the MQTT/watchdog threads.

Auth: invite token (>=128 bit, stored hashed) exchanged once at /api/login for a
server-side session cookie (HttpOnly, SameSite=Lax, Secure when not localhost).
Per-user lockout after repeated bad tokens (in-memory, not per-IP — behind the
tunnel every peer IP is Cloudflare's). CSRF: every state-changing route requires
the `X-Requested-With: loralarm` header and a same-origin Origin when present.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import timedelta, timezone
from pathlib import Path
from typing import Any

from .events import EventLog, hash_token, utc_now
from .service import Engine

try:  # optional dependency: requirements-web.txt; module must import without it
    from fastapi import Depends, FastAPI, HTTPException, Request
    from fastapi.responses import JSONResponse, Response
    from fastapi.staticfiles import StaticFiles
    _HAS_FASTAPI = True
except ImportError:  # pragma: no cover
    _HAS_FASTAPI = False

log = logging.getLogger(__name__)

COOKIE = "loralarm_session"
_EUI_RE = re.compile(r"^[0-9a-f]{16}$")
LOGIN_LOCKOUT_AFTER = 5
LOGIN_LOCKOUT_S = 15 * 60
RATE_LIMIT_PER_MIN = 60
SNOOZE_PRESETS_H = (1.0, 8.0, 24.0)


class _Limiter:
    """Tiny in-memory per-user rate limit and login lockout (not per-IP: behind
    the Cloudflare tunnel the peer IP is always Cloudflare's)."""

    def __init__(self):
        self._hits: dict[str, list[float]] = {}
        self._lockout: dict[str, tuple[int, float]] = {}

    def too_many_requests(self, key: str) -> bool:
        now = time.monotonic()
        hits = [t for t in self._hits.get(key, []) if now - t < 60]
        self._hits[key] = hits
        if len(hits) >= RATE_LIMIT_PER_MIN:
            return True
        hits.append(now)
        return False

    def login_failed(self, name: str) -> bool:
        """Record a bad token; returns True when the name is now locked out."""
        now = time.monotonic()
        fails, locked_until = self._lockout.get(name, (0, 0.0))
        if now < locked_until:
            return True
        fails += 1
        if fails >= LOGIN_LOCKOUT_AFTER:
            self._lockout[name] = (0, now + LOGIN_LOCKOUT_S)
            log.warning("login lockout for %s after %d bad tokens", name, fails)
            return True
        self._lockout[name] = (fails, 0.0)
        return False

    def login_ok(self, name: str) -> None:
        self._lockout.pop(name, None)


def create_app(engine: Engine, event_log: EventLog, webpush: Any = None,
               static_dir: str | Path | None = None,
               cookie_secure: bool = True) -> Any:
    """Build the FastAPI app. All state flows through engine.snapshot() and
    event_log; `webpush` is only used to report whether the channel is on."""
    if not _HAS_FASTAPI:
        raise RuntimeError("web layer needs requirements-web.txt "
                           "(pip install fastapi uvicorn)")

    app = FastAPI(title="loralarm", docs_url=None, redoc_url=None, openapi_url=None)
    limiter = _Limiter()

    def _user(request: Request) -> dict[str, Any]:
        sid = request.cookies.get(COOKIE, "")
        user = event_log.resolve_session(sid) if sid else None
        if user is None:
            raise HTTPException(status_code=401, detail="not logged in")
        return user

    def _csrf(request: Request) -> None:
        """State-changing routes: custom header + same-origin Origin check."""
        if request.headers.get("x-requested-with") != "loralarm":
            raise HTTPException(status_code=403, detail="missing X-Requested-With header")
        origin = request.headers.get("origin")
        if origin:
            from urllib.parse import urlparse
            if urlparse(origin).netloc != request.headers.get("host", ""):
                raise HTTPException(status_code=403, detail="cross-origin request")

    def _limited(request: Request, user: dict[str, Any]) -> None:
        if limiter.too_many_requests(f"u:{user['id']}"):
            raise HTTPException(status_code=429, detail="rate limited")

    async def _guard(request: Request, user: dict[str, Any] = Depends(_user)) -> dict[str, Any]:
        _limited(request, user)
        return user

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        # never let an API error propagate anywhere near the alarm path
        log.exception("web request failed: %s %s", request.method, request.url.path)
        return JSONResponse(status_code=500, content={"detail": "internal error"})

    # ---- auth ----

    @app.post("/api/login")
    async def login(request: Request):
        try:
            body = await request.json()
        except Exception:
            body = {}
        token = str(body.get("token") or "")
        if limiter.too_many_requests(f"login:{request.client.host if request.client else '?'}"):
            raise HTTPException(status_code=429, detail="rate limited")
        # hash outside any EventLog lock (pure sha256), then one short lookup
        user = event_log.user_by_token_hash(hash_token(token)) if token else None
        if user is None:
            name = token[:8]  # lockout keyed on a prefix; never log the token
            if limiter.login_failed(name):
                raise HTTPException(status_code=429, detail="too many attempts; locked out")
            raise HTTPException(status_code=401, detail="bad invite token")
        limiter.login_ok(token[:8])
        sid = event_log.create_session(user["id"])
        resp = Response(content=json.dumps({"ok": True, "name": user["name"], "role": user["role"]},
                                          ensure_ascii=False))
        resp.set_cookie(COOKIE, sid, httponly=True, samesite="lax",
                        secure=cookie_secure, max_age=30 * 24 * 3600)
        return resp

    @app.post("/api/logout")
    async def logout(request: Request, user: dict = Depends(_user)):
        _csrf(request)
        sid = request.cookies.get(COOKIE, "")
        if sid:
            event_log.revoke_session(sid)
        resp = Response(content=json.dumps({"ok": True}))
        resp.delete_cookie(COOKIE)
        return resp

    # ---- fleet / state ----

    def _fleet(user: dict[str, Any]) -> list[dict[str, Any]]:
        snap = engine.snapshot()          # lock-protected, in-memory only
        euis = set(engine.config.devices) | set(snap) | set(event_log.known_devices())
        muted = set(event_log.get_muted(user["id"]))   # opt-out: default = follow all
        out = []
        for eui in sorted(euis):
            entry = snap.get(eui, {})
            state = entry.get("state", {})
            cfg = engine.config.for_event(eui, entry.get("name", ""), entry.get("family", ""))
            armed_row = event_log.get_armed(eui) or {}
            lv = state.get("last_values") or {}
            out.append({
                "dev_eui": eui,
                "name": entry.get("name") or cfg.name,
                "family": entry.get("family") or cfg.family,
                "kind": cfg.kind,
                "active": state.get("active", []),
                "last_seen": state.get("last_seen"),
                "temperature": lv.get("temperature"),
                "battery_pct": lv.get("battery_pct"),
                "battery_voltage": lv.get("battery_voltage"),
                "rssi": state.get("last_rssi"),
                "snr": state.get("last_snr"),
                "armed": bool(armed_row.get("armed", True)),
                "armed_until": armed_row.get("until"),
                "following": eui not in muted,
            })
        return out

    @app.get("/api/devices")
    async def devices(user: dict = Depends(_guard)):
        return {"devices": _fleet(user), "webpush": webpush is not None}

    @app.get("/api/state")
    async def state(user: dict = Depends(_guard)):
        snap = engine.snapshot()
        alarms = [{"dev_eui": eui, "name": e.get("name", ""), "kind": k,
                   "detail": e.get("state", {}).get("last_detail", {}).get(k, "")}
                  for eui, e in snap.items() for k in e.get("state", {}).get("active", [])]
        return {"ok": not alarms, "alarms": alarms}

    @app.get("/api/events")
    async def events(request: Request, user: dict = Depends(_guard)):
        hours = min(max(float(request.query_params.get("hours", 24)), 0.1), 24 * 90)
        limit = min(max(int(request.query_params.get("limit", 200)), 1), 500)
        rows = event_log.recent(hours=hours, limit=limit)
        return {"events": [{"ts": r[0], "device": r[1], "kind": r[2], "active": bool(r[3]),
                            "detail": r[4], "user": r[5]} for r in rows]}

    # ---- subscriptions (per-user follow set; stored as opt-out mutes so the
    # default is "follow all" and a new device never alarms to nobody) ----

    @app.get("/api/subscriptions")
    async def get_subs(user: dict = Depends(_guard)):
        snap = engine.snapshot()
        known = set(engine.config.devices) | set(snap) | set(event_log.known_devices())
        muted = set(event_log.get_muted(user["id"]))
        followed = sorted(known - muted)
        return {"devices": followed, "all": not muted}

    @app.put("/api/subscriptions")
    async def put_subs(request: Request, user: dict = Depends(_guard)):
        _csrf(request)
        body = await request.json()
        devs = body.get("devices")
        if not isinstance(devs, list) or not all(isinstance(d, str) for d in devs):
            raise HTTPException(status_code=422, detail="devices must be a list of devEUIs")
        devs = [d.lower() for d in devs]
        if not all(_EUI_RE.match(d) for d in devs):
            raise HTTPException(status_code=422, detail="devices must be a list of devEUIs")
        snap = engine.snapshot()
        known = set(engine.config.devices) | set(snap) | set(event_log.known_devices())
        event_log.set_muted(user["id"], sorted(known - set(devs)))
        return {"ok": True, "devices": devs}

    # ---- arm/disarm (global per device; audited; snooze presets) ----

    @app.get("/api/devices/{eui}/armed")
    async def get_armed(eui: str, user: dict = Depends(_guard)):
        eui = eui.lower()
        row = event_log.get_armed(eui) or {"armed": True, "until": None}
        return {"dev_eui": eui, "armed": bool(row["armed"]), "until": row["until"],
                "can_disarm_forever": user["role"] == "admin"}

    @app.put("/api/devices/{eui}/armed")
    async def put_armed(eui: str, request: Request, user: dict = Depends(_guard)):
        _csrf(request)
        eui = eui.lower()
        if not _EUI_RE.match(eui):
            raise HTTPException(status_code=422, detail="bad devEUI")
        if eui not in engine.config.devices and eui not in engine.snapshot() \
                and eui not in event_log.known_devices():
            raise HTTPException(status_code=404, detail="unknown device")
        body = await request.json()
        armed = body.get("armed")
        if not isinstance(armed, bool):
            raise HTTPException(status_code=422, detail="armed must be a bool")
        snooze = body.get("snooze_hours")
        until = None
        if not armed:
            if snooze is None:
                if user["role"] != "admin":
                    raise HTTPException(status_code=403,
                                        detail="only admins may disarm 'tills vidare'; "
                                               "pick a snooze (1/8/24 h)")
            else:
                snooze = float(snooze)
                if user["role"] != "admin" and snooze not in SNOOZE_PRESETS_H:
                    raise HTTPException(status_code=403,
                                        detail=f"snooze must be one of {SNOOZE_PRESETS_H}")
                if snooze <= 0 or snooze > 24 * 365:
                    raise HTTPException(status_code=422, detail="snooze out of range")
                until = utc_now() + timedelta(hours=snooze)
        engine.set_armed(eui, armed, until, user=user["name"])
        row = event_log.get_armed(eui) or {}
        return {"dev_eui": eui, "armed": bool(row.get("armed", True)), "until": row.get("until")}

    # ---- Web Push ----

    @app.post("/api/push/subscribe")
    async def push_subscribe(request: Request, user: dict = Depends(_guard)):
        _csrf(request)
        body = await request.json()
        endpoint = str(body.get("endpoint") or "")
        keys = body.get("keys") or {}
        if not endpoint.startswith("https://") or not keys.get("p256dh") or not keys.get("auth"):
            raise HTTPException(status_code=422, detail="bad subscription")
        eid = event_log.add_endpoint(user["id"], endpoint, str(keys["p256dh"]),
                                     str(keys["auth"]), request.headers.get("user-agent", ""))
        return {"ok": True, "endpoint_id": eid}

    @app.delete("/api/push/subscribe")
    async def push_unsubscribe(request: Request, user: dict = Depends(_guard)):
        _csrf(request)
        body = await request.json()
        removed = event_log.remove_endpoint(str(body.get("endpoint") or ""))
        return {"ok": removed}

    @app.post("/api/push/ack")
    async def push_ack(request: Request, user: dict = Depends(_user)):
        # called by the service worker on delivery; cheap and idempotent
        body = await request.json()
        try:
            event_id = int(body.get("event_id"))
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail="bad event_id")
        eid = event_log.endpoint_id(str(body.get("endpoint") or ""))
        ok = eid is not None and event_log.log_push_ack(user["id"], event_id, eid)
        return {"ok": ok}

    @app.get("/api/health")
    async def health():
        return {"ok": True}

    if static_dir and Path(static_dir).is_dir():
        app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")
    else:
        @app.get("/")
        async def index():
            return {"app": "loralarm", "api": "/api/health"}

    return app


class WebServer:
    """Runs the FastAPI app in a uvicorn thread inside the service process.
    The alarm path never waits on this thread; shutdown is best-effort."""

    def __init__(self, app, host: str = "127.0.0.1", port: int = 8080):
        import uvicorn
        self._config = uvicorn.Config(app, host=host, port=port, log_level="warning")
        self._server = uvicorn.Server(self._config)
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._server.run, name="loralarm-web",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
