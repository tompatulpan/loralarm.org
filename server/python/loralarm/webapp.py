"""FastAPI web app for the PWA / status page, run in a uvicorn thread inside the
service process (PWA design §2/§5). The alarm path is never touched from here:
engine state is read only through the lock-protected `Engine.snapshot()`, and
all persistence goes through `EventLog` (short queries; tokens are hashed before
its lock is taken). API exceptions are caught and logged — they can never reach
the MQTT/watchdog threads.

Auth: invite token (>=128 bit, stored hashed) exchanged once at /api/login for a
server-side session cookie (HttpOnly, SameSite=Lax, Secure unless explicitly
disabled for local development). Bad tokens are throttled globally, with bounded
state: the user is unknown at that point and behind the tunnel every peer IP is
Cloudflare's, so neither a per-user nor a per-IP lockout is possible — and a valid
token is always accepted, so the throttle can never lock real users out. Per-user
rate limit once logged in. CSRF: every state-changing route requires the
`X-Requested-With: loralarm` header and a same-origin Origin when present.

Route handlers are plain `def` (FastAPI runs them in its threadpool), so a slow
SQLite commit or the engine lock can only stall a worker thread, never the event
loop. Request bodies are size-capped and must be JSON objects.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import threading
import time
from collections import deque
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .events import EventLog, can_access, hash_token, utc_now
from .service import Engine

try:  # optional dependency: requirements-web.txt; module must import without it
    from fastapi import Depends, FastAPI, HTTPException, Request
    from fastapi.responses import JSONResponse, Response
    from fastapi.staticfiles import StaticFiles
    from starlette.datastructures import MutableHeaders
    _HAS_FASTAPI = True
except ImportError:  # pragma: no cover
    _HAS_FASTAPI = False

log = logging.getLogger(__name__)

COOKIE = "loralarm_session"
_EUI_RE = re.compile(r"^[0-9a-f]{16}$")
LOGIN_LOCKOUT_AFTER = 10       # bad tokens per minute (all clients) before 429
RATE_LIMIT_PER_MIN = 60
SNOOZE_PRESETS_H = (1.0, 8.0, 24.0)
MAX_BODY_BYTES = 16 * 1024
MAX_ENDPOINTS_PER_USER = 10
MAX_ENDPOINT_LEN = 2048
MAX_KEY_LEN = 256
# Web Push services only: the server POSTs to the registered endpoint, so an
# arbitrary https URL would be a server-side request forgery primitive
PUSH_HOST_SUFFIXES = ("fcm.googleapis.com", "push.services.mozilla.com",
                      "push.apple.com", "notify.windows.com")


# The PWA is same-origin only: no inline script/style, no third parties. frame-
# ancestors blocks clickjacking of the arm/disarm screen.
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
       "connect-src 'self'; manifest-src 'self'; worker-src 'self'; "
       "base-uri 'none'; form-action 'self'; frame-ancestors 'none'")


class _SecurityHeaders:
    """Pure ASGI middleware (no body buffering): security headers on every
    response, `no-store` on the API, and `no-cache` (always revalidate, cheap
    with ETags) on static files so a deploy reaches users immediately."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        api = scope.get("path", "").startswith("/api/")

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                h = MutableHeaders(scope=message)
                h["Content-Security-Policy"] = CSP
                h["X-Content-Type-Options"] = "nosniff"
                h["X-Frame-Options"] = "DENY"
                h["Referrer-Policy"] = "no-referrer"
                if api:
                    h["Cache-Control"] = "no-store"
                else:
                    h.setdefault("Cache-Control", "no-cache")
            await send(message)

        await self.app(scope, receive, send_with_headers)


def push_endpoint_allowed(endpoint: str) -> bool:
    if len(endpoint) > MAX_ENDPOINT_LEN:
        return False
    try:
        u = urlparse(endpoint)
        host, port = (u.hostname or "").lower(), u.port
    except ValueError:
        return False
    if u.scheme != "https" or u.username or u.password or port not in (None, 443):
        return False
    return any(host == s or host.endswith("." + s) for s in PUSH_HOST_SUFFIXES)


class _Limiter:
    """Tiny in-memory limiter. Per-user request counts are bounded by the number
    of users; failed logins are one bounded global window (never per attacker-
    chosen key)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._hits: dict[str, deque[float]] = {}
        self._failures: deque[float] = deque(maxlen=LOGIN_LOCKOUT_AFTER)

    def too_many_requests(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            hits = self._hits.setdefault(key, deque(maxlen=RATE_LIMIT_PER_MIN))
            while hits and now - hits[0] >= 60:
                hits.popleft()
            if len(hits) >= RATE_LIMIT_PER_MIN:
                return True
            hits.append(now)
            return False

    def login_failed(self) -> bool:
        """Record a bad token; True when failures are now over the global limit."""
        now = time.monotonic()
        with self._lock:
            self._failures.append(now)
            over = (len(self._failures) >= LOGIN_LOCKOUT_AFTER
                    and now - self._failures[0] < 60)
        if over:
            log.warning("many failed logins (>= %d in a minute)", LOGIN_LOCKOUT_AFTER)
        return over


def _number(value: Any, name: str, lo: float, hi: float) -> float:
    """A finite number within [lo, hi], else 422 (never a 500)."""
    if isinstance(value, bool) or value is None:
        raise HTTPException(status_code=422, detail=f"{name} must be a number")
    try:
        x = float(value)
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail=f"{name} must be a number") from None
    if not math.isfinite(x) or not lo <= x <= hi:
        raise HTTPException(status_code=422, detail=f"{name} out of range")
    return x


async def _body(request: Request) -> dict[str, Any]:
    """Size-capped JSON object body; 413/422 instead of an unhandled error."""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="request too large")
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY_BYTES:
            raise HTTPException(status_code=413, detail="request too large")
        chunks.append(chunk)
    try:
        body = json.loads(b"".join(chunks) or b"{}")
    except ValueError:
        raise HTTPException(status_code=422, detail="body must be JSON") from None
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="body must be a JSON object")
    return body


def create_app(engine: Engine, event_log: EventLog, webpush: Any = None,
               static_dir: str | Path | None = None,
               cookie_secure: bool = True) -> Any:
    """Build the FastAPI app. All state flows through engine.snapshot() and
    event_log; `webpush` is only used to report whether the channel is on."""
    if not _HAS_FASTAPI:
        raise RuntimeError("web layer needs requirements-web.txt "
                           "(pip install fastapi uvicorn)")

    app = FastAPI(title="loralarm", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(_SecurityHeaders)
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
        if origin and urlparse(origin).netloc != request.headers.get("host", ""):
            raise HTTPException(status_code=403, detail="cross-origin request")

    def _guard(user: dict[str, Any] = Depends(_user)) -> dict[str, Any]:
        if limiter.too_many_requests(f"u:{user['id']}"):
            raise HTTPException(status_code=429, detail="rate limited")
        return user

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        # never let an API error propagate anywhere near the alarm path
        log.exception("web request failed: %s %s", request.method, request.url.path)
        return JSONResponse(status_code=500, content={"detail": "internal error"})

    # ---- auth ----

    @app.post("/api/login", dependencies=[Depends(_csrf)])
    def login(body: dict = Depends(_body)):
        token = body.get("token")
        token = token if isinstance(token, str) else ""
        # hash outside any EventLog lock (pure sha256), then one short lookup;
        # a valid token is always accepted, only failures are throttled
        user = event_log.user_by_token_hash(hash_token(token)) if token else None
        if user is None:
            if limiter.login_failed():
                raise HTTPException(status_code=429, detail="too many failed attempts")
            raise HTTPException(status_code=401, detail="bad invite token")
        sid = event_log.create_session(user["id"])
        resp = Response(content=json.dumps({"ok": True, "name": user["name"], "role": user["role"]},
                                          ensure_ascii=False),
                        media_type="application/json")
        resp.set_cookie(COOKIE, sid, httponly=True, samesite="lax",
                        secure=cookie_secure, max_age=30 * 24 * 3600)
        return resp

    @app.get("/api/me")
    def me(user: dict = Depends(_guard)):
        return {"name": user["name"], "role": user["role"]}

    @app.post("/api/logout", dependencies=[Depends(_csrf)])
    def logout(request: Request, user: dict = Depends(_user)):
        sid = request.cookies.get(COOKIE, "")
        if sid:
            event_log.revoke_session(sid)
        resp = Response(content=json.dumps({"ok": True}), media_type="application/json")
        resp.delete_cookie(COOKIE)
        return resp

    # ---- device visibility: the admin assigns devices to groups and users to
    # groups in the config; everything below only ever sees the user's devices ----

    def _group_of(eui: str, snap: dict[str, Any]) -> str:
        entry = snap.get(eui, {})
        return engine.config.for_event(eui, entry.get("name", ""), entry.get("family", "")).group

    def _visible(user: dict[str, Any], snap: dict[str, Any] | None = None) -> set[str]:
        snap = engine.snapshot() if snap is None else snap
        known = set(engine.config.devices) | set(snap) | set(event_log.known_devices())
        return {e for e in known if can_access(user, _group_of(e, snap))}

    # ---- fleet / state ----

    def _fleet(user: dict[str, Any]) -> list[dict[str, Any]]:
        snap = engine.snapshot()          # lock-protected, in-memory only
        euis = _visible(user, snap)
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
                "group": cfg.group,
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
    def devices(user: dict = Depends(_guard)):
        return {"devices": _fleet(user), "webpush": webpush is not None}

    @app.get("/api/state")
    def state(user: dict = Depends(_guard)):
        snap = engine.snapshot()
        visible = _visible(user, snap)
        alarms = [{"dev_eui": eui, "name": e.get("name", ""), "kind": k,
                   "detail": e.get("state", {}).get("last_detail", {}).get(k, "")}
                  for eui, e in snap.items() if eui in visible
                  for k in e.get("state", {}).get("active", [])]
        return {"ok": not alarms, "alarms": alarms}

    @app.get("/api/events")
    def events(request: Request, user: dict = Depends(_guard)):
        # out-of-range values are clamped; unparsable ones are a 422
        hours = min(max(_number(request.query_params.get("hours", 24), "hours", -math.inf, math.inf),
                        0.1), 24 * 90)
        limit = int(min(max(_number(request.query_params.get("limit", 200), "limit",
                                    -math.inf, math.inf), 1), 500))
        rows = event_log.recent(hours=hours, limit=limit, dev_euis=sorted(_visible(user)))
        return {"events": [{"ts": r[0], "device": r[1], "kind": r[2], "active": bool(r[3]),
                            "detail": r[4], "user": r[5], "dev_eui": r[6]} for r in rows]}

    # ---- subscriptions (per-user follow set; stored as opt-out mutes so the
    # default is "follow all" and a new device never alarms to nobody) ----

    @app.get("/api/subscriptions")
    def get_subs(user: dict = Depends(_guard)):
        known = _visible(user)
        muted = set(event_log.get_muted(user["id"]))
        followed = sorted(known - muted)
        return {"devices": followed, "all": not muted}

    @app.put("/api/subscriptions", dependencies=[Depends(_csrf)])
    def put_subs(body: dict = Depends(_body), user: dict = Depends(_guard)):
        devs = body.get("devices")
        if not isinstance(devs, list) or not all(isinstance(d, str) for d in devs):
            raise HTTPException(status_code=422, detail="devices must be a list of devEUIs")
        devs = [d.lower() for d in devs]
        if not all(_EUI_RE.match(d) for d in devs):
            raise HTTPException(status_code=422, detail="devices must be a list of devEUIs")
        known = _visible(user)
        event_log.set_muted(user["id"], sorted(known - set(devs)))
        return {"ok": True, "devices": devs}

    # ---- arm/disarm (global per device; audited; snooze presets) ----

    @app.get("/api/devices/{eui}/armed")
    def get_armed(eui: str, user: dict = Depends(_guard)):
        eui = eui.lower()
        if eui not in _visible(user):      # 404, not 403: do not reveal other users' devices
            raise HTTPException(status_code=404, detail="unknown device")
        row = event_log.get_armed(eui) or {"armed": True, "until": None}
        return {"dev_eui": eui, "armed": bool(row["armed"]), "until": row["until"],
                "can_disarm_forever": user["role"] == "admin"}

    @app.put("/api/devices/{eui}/armed", dependencies=[Depends(_csrf)])
    def put_armed(eui: str, body: dict = Depends(_body), user: dict = Depends(_guard)):
        eui = eui.lower()
        if not _EUI_RE.match(eui):
            raise HTTPException(status_code=422, detail="bad devEUI")
        if eui not in _visible(user):
            raise HTTPException(status_code=404, detail="unknown device")
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
                snooze = _number(snooze, "snooze_hours", 0.0, 24 * 365)
                if snooze <= 0:
                    raise HTTPException(status_code=422, detail="snooze out of range")
                if user["role"] != "admin" and snooze not in SNOOZE_PRESETS_H:
                    raise HTTPException(status_code=403,
                                        detail=f"snooze must be one of {SNOOZE_PRESETS_H}")
                until = utc_now() + timedelta(hours=snooze)
        engine.set_armed(eui, armed, until, user=user["name"])
        row = event_log.get_armed(eui) or {}
        return {"dev_eui": eui, "armed": bool(row.get("armed", True)), "until": row.get("until")}

    # ---- Web Push ----

    @app.post("/api/push/subscribe", dependencies=[Depends(_csrf)])
    def push_subscribe(request: Request, body: dict = Depends(_body),
                       user: dict = Depends(_guard)):
        endpoint = body.get("endpoint")
        keys = body.get("keys")
        keys = keys if isinstance(keys, dict) else {}
        p256dh, auth = keys.get("p256dh"), keys.get("auth")
        if (not isinstance(endpoint, str) or not push_endpoint_allowed(endpoint)
                or not isinstance(p256dh, str) or not isinstance(auth, str)
                or not 0 < len(p256dh) <= MAX_KEY_LEN or not 0 < len(auth) <= MAX_KEY_LEN):
            raise HTTPException(status_code=422, detail="bad subscription")
        if (event_log.endpoint_id(endpoint) is None
                and event_log.count_endpoints(user["id"]) >= MAX_ENDPOINTS_PER_USER):
            raise HTTPException(status_code=409, detail="too many registered devices")
        eid = event_log.add_endpoint(user["id"], endpoint, p256dh, auth,
                                     request.headers.get("user-agent", "")[:200])
        return {"ok": True, "endpoint_id": eid}

    @app.delete("/api/push/subscribe", dependencies=[Depends(_csrf)])
    def push_unsubscribe(body: dict = Depends(_body), user: dict = Depends(_guard)):
        removed = event_log.remove_endpoint(str(body.get("endpoint") or ""), user["id"])
        return {"ok": removed}

    @app.post("/api/push/ack", dependencies=[Depends(_csrf)])
    def push_ack(body: dict = Depends(_body), user: dict = Depends(_guard)):
        # called by the service worker on delivery (with the X-Requested-With
        # header); idempotent, owner-checked, and only for events that exist
        event_id = _number(body.get("event_id"), "event_id", 1, 2**62)
        eid = event_log.endpoint_id(str(body.get("endpoint") or ""))
        ok = (eid is not None and event_log.event_exists(int(event_id))
              and event_log.log_push_ack(user["id"], int(event_id), eid))
        return {"ok": ok}

    vapid_cache: dict[str, str | None] = {}

    def _vapid_public_key() -> str | None:
        """The applicationServerKey the PWA subscribes with: explicit
        LORALARM_VAPID_PUBLIC_KEY, else derived from the private key
        (cryptography, already a pywebpush dependency). Derived once: a key
        that cannot be derived is logged once, not on every request."""
        env = os.environ.get("LORALARM_VAPID_PUBLIC_KEY", "").strip()
        if env:
            return env
        key = getattr(webpush, "vapid_private_key", None) if webpush is not None else None
        if not key:
            return None
        if key not in vapid_cache:
            vapid_cache[key] = _derive_vapid_public(key)
        return vapid_cache[key]

    def _derive_vapid_public(key: str) -> str | None:
        try:
            import base64
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.primitives.asymmetric import ec
            raw = base64.urlsafe_b64decode(key + "=" * (-len(key) % 4))
            priv = ec.derive_private_key(int.from_bytes(raw, "big"), ec.SECP256R1())
            pub = priv.public_key().public_bytes(
                serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
            return base64.urlsafe_b64encode(pub).rstrip(b"=").decode()
        except Exception:
            log.exception("could not derive VAPID public key from the private key")
            return None

    @app.get("/api/push/key")
    def push_key(user: dict = Depends(_guard)):
        key = _vapid_public_key()
        if not key:
            raise HTTPException(status_code=503, detail="web push not configured (no VAPID key)")
        return {"public_key": key}

    @app.post("/api/push/test", dependencies=[Depends(_csrf)])
    def push_test(body: dict = Depends(_body), user: dict = Depends(_guard)):
        if webpush is None:
            raise HTTPException(status_code=503, detail="web push not enabled (--web)")
        title = str(body.get("title") or "loralarm test")[:120]
        message = str(body.get("body") or "Test-push från loralarm PWA")[:500]
        return {"ok": True, "endpoints": webpush.test_push(user["id"], title, message)}

    @app.get("/api/health")
    def health():
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
