"""Phase 2: the FastAPI web layer — auth (invite token -> session cookie,
lockout), CSRF, rate limit, and the /api/* routes over Engine.snapshot() and
EventLog. Uses fastapi's TestClient (httpx), no network."""
from __future__ import annotations

import importlib.util
import unittest
from datetime import timedelta
from pathlib import Path

from loralarm.alarms import AlarmKind
from loralarm.events import EventLog, hash_token
from loralarm.service import Engine
from loralarm.webapp import COOKIE, LOGIN_LOCKOUT_AFTER, create_app

from .common import DEV_EUI, OTHER_EUI, T0, make_config, make_uplink, sensor_obj

ADMIN = {"name": "anna", "token": "admin-token-" + "a" * 32, "role": "admin"}
USER = {"name": "bosse", "token": "user-token-" + "b" * 32, "role": "user"}

HEADERS = {"X-Requested-With": "loralarm"}
PUSH = "https://fcm.googleapis.com/fcm/send/xyz"

needs_web = unittest.skipUnless(importlib.util.find_spec("fastapi")
                                and importlib.util.find_spec("httpx"),
                                "web layer needs requirements-web.txt")


def make_client(users=(ADMIN, USER), **cfg_kw):
    from fastapi.testclient import TestClient
    log = EventLog(None)
    log.seed_users(list(users))
    cfg = make_config(devices={DEV_EUI: {"name": "sensor3"}, OTHER_EUI: {"name": "sensor16"}},
                      defaults={"motion_mode": "level"}, **cfg_kw)
    engine = Engine(cfg, log, None)
    app = create_app(engine, log, webpush=None, static_dir=None, cookie_secure=False)
    return TestClient(app), engine, log


def login(client, token):
    return client.post("/api/login", json={"token": token}, headers=HEADERS)


@needs_web
class AuthTest(unittest.TestCase):
    def test_bad_tokens_are_throttled_globally_but_valid_token_still_works(self):
        client, _, _ = make_client()
        for i in range(LOGIN_LOCKOUT_AFTER - 1):
            self.assertEqual(login(client, f"{i}-wrong").status_code, 401)
        # the throttle does not depend on the guessed token's prefix ...
        self.assertEqual(login(client, "zzzz-other").status_code, 429)
        # ... and never locks a real user out
        self.assertEqual(login(client, USER["token"]).status_code, 200)

    def test_limiter_state_stays_bounded(self):
        from loralarm.webapp import _Limiter
        lim = _Limiter()
        for _ in range(10_000):
            lim.login_failed()
        self.assertLessEqual(len(lim._failures), LOGIN_LOCKOUT_AFTER)

    def test_login_needs_csrf_header(self):
        client, _, _ = make_client()
        r = client.post("/api/login", json={"token": USER["token"]})
        self.assertEqual(r.status_code, 403)

    def test_cookie_is_secure_by_default(self):
        from fastapi.testclient import TestClient
        client, engine, log = make_client()
        app = create_app(engine, log)            # default: cookie_secure=True
        r = TestClient(app).post("/api/login", json={"token": USER["token"]}, headers=HEADERS)
        self.assertIn("secure", r.headers["set-cookie"].lower())
        self.assertIn("httponly", r.headers["set-cookie"].lower())

    def test_me_returns_identity_and_needs_session(self):
        client, _, _ = make_client()
        self.assertEqual(client.get("/api/me").status_code, 401)
        login(client, USER["token"])
        r = client.get("/api/me")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"name": "bosse", "role": "user"})

    def test_good_token_sets_session_cookie_and_api_needs_it(self):
        client, _, _ = make_client()
        r = login(client, USER["token"])
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["name"], "bosse")
        self.assertIn(COOKIE, r.cookies)
        self.assertEqual(client.get("/api/devices").status_code, 200)
        anon = make_client()[0]
        self.assertEqual(anon.get("/api/devices").status_code, 401)

    def test_logout_revokes_session(self):
        client, _, log = make_client()
        login(client, USER["token"])
        r = client.post("/api/logout", headers=HEADERS)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(client.get("/api/devices").status_code, 401)

    def test_disabled_user_cannot_login(self):
        client, _, log = make_client()
        log.seed_users([ADMIN])   # bosse removed -> disabled
        self.assertEqual(login(client, USER["token"]).status_code, 401)


@needs_web
class CsrfTest(unittest.TestCase):
    def test_state_change_requires_header(self):
        client, _, _ = make_client()
        login(client, USER["token"])
        self.assertEqual(client.put("/api/subscriptions", json={"devices": []}).status_code, 403)
        r = client.put("/api/subscriptions", json={"devices": [DEV_EUI]}, headers=HEADERS)
        self.assertEqual(r.status_code, 200)


@needs_web
class FleetApiTest(unittest.TestCase):
    def test_devices_and_state_reflect_engine(self):
        client, engine, log = make_client()
        engine.on_uplink(make_uplink(obj=sensor_obj(motion_alert=1)))
        login(client, USER["token"])

        devs = client.get("/api/devices").json()["devices"]
        by_eui = {d["dev_eui"]: d for d in devs}
        self.assertIn(DEV_EUI, by_eui)
        d = by_eui[DEV_EUI]
        self.assertEqual(d["name"], "sensor3")
        self.assertEqual(d["active"], ["motion"])
        self.assertEqual(d["temperature"], 24.0)
        self.assertTrue(d["armed"])
        self.assertTrue(d["following"])          # default: follow all

        state = client.get("/api/state").json()
        self.assertFalse(state["ok"])
        self.assertEqual(state["alarms"][0]["kind"], "motion")

    def test_subscriptions_roundtrip(self):
        client, _, _ = make_client()
        login(client, USER["token"])
        r = client.get("/api/subscriptions").json()
        # default: follow all (opt-out mutes are empty)
        self.assertEqual(r, {"devices": [DEV_EUI, OTHER_EUI], "all": True})
        client.put("/api/subscriptions", json={"devices": [DEV_EUI.upper()]}, headers=HEADERS)
        r = client.get("/api/subscriptions").json()
        self.assertEqual(r, {"devices": [DEV_EUI], "all": False})
        # bad eui rejected
        r = client.put("/api/subscriptions", json={"devices": ["nope"]}, headers=HEADERS)
        self.assertEqual(r.status_code, 422)

    def test_events_bounded(self):
        client, engine, _ = make_client()
        engine.on_uplink(make_uplink(obj=sensor_obj(motion_alert=1)))
        login(client, USER["token"])
        r = client.get("/api/events?hours=87600&limit=10").json()   # T0 is days before now
        self.assertEqual(len(r["events"]), 1)
        self.assertEqual(r["events"][0]["kind"], "motion")
        r = client.get("/api/events?hours=99999&limit=99999").json()  # clamped, not unbounded
        self.assertEqual(r["events"][0]["kind"], "motion")


@needs_web
class ArmedApiTest(unittest.TestCase):
    def test_get_armed_default_true(self):
        client, _, _ = make_client()
        login(client, USER["token"])
        r = client.get(f"/api/devices/{DEV_EUI}/armed").json()
        self.assertTrue(r["armed"])
        self.assertFalse(r["can_disarm_forever"])   # bosse is not admin

    def test_disarm_with_snooze_suppresses_and_audits(self):
        client, engine, log = make_client()
        login(client, USER["token"])
        r = client.put(f"/api/devices/{DEV_EUI}/armed",
                       json={"armed": False, "snooze_hours": 1}, headers=HEADERS)
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["armed"])
        self.assertIsNotNone(r.json()["until"])
        self.assertFalse(engine.is_armed(DEV_EUI))
        row = log._conn.execute(
            "select active, user from events where kind='armed' order by id desc limit 1"
        ).fetchone()
        self.assertEqual(row, (0, "bosse"))

    def test_disarm_forever_requires_admin(self):
        client, _, _ = make_client()
        login(client, USER["token"])
        r = client.put(f"/api/devices/{DEV_EUI}/armed", json={"armed": False}, headers=HEADERS)
        self.assertEqual(r.status_code, 403)
        # non-admin snooze must be a preset
        r = client.put(f"/api/devices/{DEV_EUI}/armed",
                       json={"armed": False, "snooze_hours": 3}, headers=HEADERS)
        self.assertEqual(r.status_code, 403)
        # admin may go "tills vidare"
        login(client, ADMIN["token"])
        r = client.put(f"/api/devices/{DEV_EUI}/armed", json={"armed": False}, headers=HEADERS)
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.json()["until"])

    def test_unknown_device_404(self):
        client, _, _ = make_client()
        login(client, USER["token"])
        r = client.put("/api/devices/0102030405060708/armed",
                       json={"armed": False, "snooze_hours": 1}, headers=HEADERS)
        self.assertEqual(r.status_code, 404)


@needs_web
class PushApiTest(unittest.TestCase):
    def test_subscribe_ack_remove(self):
        client, _, log = make_client()
        login(client, USER["token"])
        sub = {"endpoint": PUSH,
               "keys": {"p256dh": "p", "auth": "a"}}
        r = client.post("/api/push/subscribe", json=sub, headers=HEADERS)
        self.assertEqual(r.status_code, 200)
        eid = r.json()["endpoint_id"]
        ev = log.log(T0, DEV_EUI, "sensor3", "motion", True, "x")
        r = client.post("/api/push/ack", json={"event_id": ev, "endpoint": sub["endpoint"]},
                        headers=HEADERS)
        self.assertTrue(r.json()["ok"])
        self.assertEqual(log._conn.execute(
            "select count(*) from push_ack where event_id=?", (ev,)).fetchone()[0], 1)
        # unknown event ids are not recorded
        r = client.post("/api/push/ack", json={"event_id": 99999, "endpoint": sub["endpoint"]},
                        headers=HEADERS)
        self.assertFalse(r.json()["ok"])
        # raw endpoint never appears in the logs
        logs = log._conn.execute("select endpoint_id from webpush_log").fetchall()
        self.assertNotIn(sub["endpoint"], str(logs))
        r = client.request("DELETE", "/api/push/subscribe", json=sub, headers=HEADERS)
        self.assertTrue(r.json()["ok"])
        self.assertIsNone(log.endpoint_id(sub["endpoint"]))

    def test_subscribe_validates(self):
        client, _, _ = make_client()
        login(client, USER["token"])
        r = client.post("/api/push/subscribe", json={"endpoint": "http://x", "keys": {}},
                        headers=HEADERS)
        self.assertEqual(r.status_code, 422)

    def test_endpoint_must_be_a_known_push_service(self):
        client, _, _ = make_client()
        login(client, USER["token"])
        keys = {"p256dh": "p", "auth": "a"}
        for bad in ("https://169.254.169.254/x", "https://localhost/x",
                    "https://evil.example/push", "https://fcm.googleapis.com.evil.example/x",
                    "https://user:pw@fcm.googleapis.com/x", "https://fcm.googleapis.com:8443/x",
                    "https://fcm.googleapis.com/" + "a" * 3000):
            r = client.post("/api/push/subscribe", json={"endpoint": bad, "keys": keys},
                            headers=HEADERS)
            self.assertEqual(r.status_code, 422, bad[:50])
        for good in ("https://fcm.googleapis.com/fcm/send/abc",
                     "https://updates.push.services.mozilla.com/wpush/v2/abc",
                     "https://web.push.apple.com/abc", "https://wns2.notify.windows.com/x"):
            r = client.post("/api/push/subscribe", json={"endpoint": good, "keys": keys},
                            headers=HEADERS)
            self.assertEqual(r.status_code, 200, good)

    def test_endpoints_per_user_are_capped(self):
        from loralarm.webapp import MAX_ENDPOINTS_PER_USER
        client, _, _ = make_client()
        login(client, USER["token"])
        keys = {"p256dh": "p", "auth": "a"}
        for i in range(MAX_ENDPOINTS_PER_USER):
            r = client.post("/api/push/subscribe", headers=HEADERS,
                            json={"endpoint": f"https://fcm.googleapis.com/{i}", "keys": keys})
            self.assertEqual(r.status_code, 200)
        r = client.post("/api/push/subscribe", headers=HEADERS,
                        json={"endpoint": "https://fcm.googleapis.com/extra", "keys": keys})
        self.assertEqual(r.status_code, 409)
        # re-registering an existing one is still fine
        r = client.post("/api/push/subscribe", headers=HEADERS,
                        json={"endpoint": "https://fcm.googleapis.com/0", "keys": keys})
        self.assertEqual(r.status_code, 200)

    def test_unsubscribe_only_removes_own_endpoint(self):
        c2, _, log2 = make_client()
        sub = {"endpoint": PUSH, "keys": {"p256dh": "p", "auth": "a"}}
        login(c2, ADMIN["token"])
        c2.post("/api/push/subscribe", json=sub, headers=HEADERS)
        login(c2, USER["token"])     # same client now carries bosse's session
        r = c2.request("DELETE", "/api/push/subscribe", json=sub, headers=HEADERS)
        self.assertFalse(r.json()["ok"])
        self.assertIsNotNone(log2.endpoint_id(PUSH))

    def _client_with_webpush(self, webpush):
        from fastapi.testclient import TestClient
        from loralarm.webapp import create_app
        log = EventLog(None)
        log.seed_users([ADMIN, USER])
        engine = Engine(make_config(), log, None)
        app = create_app(engine, log, webpush=webpush, static_dir=None, cookie_secure=False)
        return TestClient(app)

    def test_push_key_from_env(self):
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {"LORALARM_VAPID_PUBLIC_KEY": "BPubKey123"}):
            client, _, _ = make_client()
            login(client, USER["token"])
            r = client.get("/api/push/key")
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json(), {"public_key": "BPubKey123"})

    @unittest.skipUnless(importlib.util.find_spec("cryptography"),
                         "VAPID derivation needs cryptography (pywebpush dep)")
    def test_push_key_derived_from_private_key(self):
        import base64
        import os
        from unittest import mock
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        priv = ec.generate_private_key(ec.SECP256R1())
        raw = priv.private_numbers().private_value.to_bytes(32, "big")
        key = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
        expected = base64.urlsafe_b64encode(priv.public_key().public_bytes(
            serialization.Encoding.X962,
            serialization.PublicFormat.UncompressedPoint)).rstrip(b"=").decode()

        class FakePush:
            vapid_private_key = key
        client = self._client_with_webpush(FakePush())
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LORALARM_VAPID_PUBLIC_KEY", None)
            login(client, USER["token"])
            r = client.get("/api/push/key")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["public_key"], expected)

    def test_push_key_503_without_any_key(self):
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LORALARM_VAPID_PUBLIC_KEY", None)
            client, _, _ = make_client()      # webpush=None
            login(client, USER["token"])
            r = client.get("/api/push/key")
            self.assertEqual(r.status_code, 503)

    def test_push_test_503_without_webpush(self):
        client, _, _ = make_client()
        login(client, USER["token"])
        r = client.post("/api/push/test", json={}, headers=HEADERS)
        self.assertEqual(r.status_code, 503)

    def test_push_test_enqueues_for_calling_user_and_needs_csrf_and_auth(self):
        from fastapi.testclient import TestClient
        calls = []

        class FakePush:
            vapid_private_key = None

            def test_push(self, user_id, title, body):
                calls.append((user_id, title, body))
                return 1
        client = self._client_with_webpush(FakePush())
        login(client, USER["token"])
        r = client.post("/api/push/test", json={"title": "t", "body": "b"}, headers=HEADERS)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"ok": True, "endpoints": 1})
        self.assertEqual(calls[0][1:], ("t", "b"))
        self.assertEqual(client.post("/api/push/test", json={}).status_code, 403)  # no header
        self.assertEqual(TestClient(client.app).post("/api/push/test", json={},
                                                     headers=HEADERS).status_code, 401)


PWA_DIR = Path(__file__).resolve().parents[3] / "PWA" / "web"


@needs_web
class HeadersAndStaticTest(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        _, engine, log = make_client()
        self.client = TestClient(create_app(engine, log, static_dir=PWA_DIR, cookie_secure=False))

    def test_security_headers_everywhere(self):
        for path in ("/", "/app.js", "/sw.js", "/api/health", "/api/me"):   # /api/me is a 401
            h = self.client.get(path).headers
            self.assertIn("frame-ancestors 'none'", h["content-security-policy"], path)
            self.assertEqual(h["x-content-type-options"], "nosniff", path)
            self.assertEqual(h["x-frame-options"], "DENY", path)
            self.assertEqual(h["referrer-policy"], "no-referrer", path)

    def test_api_is_never_cached_and_static_always_revalidates(self):
        self.assertEqual(self.client.get("/api/health").headers["cache-control"], "no-store")
        for path in ("/", "/app.js", "/styles.css", "/sw.js", "/manifest.webmanifest"):
            r = self.client.get(path)
            self.assertEqual(r.status_code, 200, path)
            self.assertEqual(r.headers["cache-control"], "no-cache", path)

    def test_pwa_files_satisfy_the_csp(self):
        # style-src/script-src 'self' only: no inline handlers, styles or scripts
        import re
        for name in ("index.html", "app.js"):
            text = (PWA_DIR / name).read_text(encoding="utf-8")
            self.assertNotRegex(text, r"\sstyle=", name)
            self.assertNotRegex(text, r"\son[a-z]+=", name)
        html = (PWA_DIR / "index.html").read_text(encoding="utf-8")
        self.assertEqual(re.findall(r"<script(?![^>]*\ssrc=)[^>]*>", html), [])
        self.assertNotIn("<style", html)

    def test_bad_vapid_key_is_logged_once_not_per_request(self):
        from fastapi.testclient import TestClient

        class Worker:
            vapid_private_key = "not-a-key"
        _, engine, log = make_client()
        client = TestClient(create_app(engine, log, webpush=Worker(), cookie_secure=False))
        login(client, USER["token"])
        with self.assertLogs("loralarm.webapp", level="ERROR") as cm:
            self.assertEqual(client.get("/api/push/key").status_code, 503)
            self.assertEqual(client.get("/api/push/key").status_code, 503)
        self.assertEqual(len(cm.records), 1)


@needs_web
class InputHardeningTest(unittest.TestCase):
    def setUp(self):
        self.client, self.engine, self.log = make_client()
        login(self.client, ADMIN["token"])

    def test_bad_bodies_are_422_not_500(self):
        for content in (b"{", b"[1]", b"\"str\"", b"null"):
            r = self.client.put("/api/subscriptions", content=content, headers=HEADERS)
            self.assertEqual(r.status_code, 422, content)

    def test_oversized_body_is_413(self):
        r = self.client.put("/api/subscriptions", headers=HEADERS,
                            content=b'{"devices": [' + b'"a",' * 10_000 + b'"b"]}')
        self.assertEqual(r.status_code, 413)

    def test_bad_query_numbers_are_422(self):
        for q in ("hours=abc", "hours=nan", "hours=inf", "limit=x"):
            self.assertEqual(self.client.get(f"/api/events?{q}").status_code, 422, q)

    def test_bad_snooze_is_422(self):
        for v in ("x", "nan", True, -1, 0, 10**9, [1]):
            r = self.client.put(f"/api/devices/{DEV_EUI}/armed", headers=HEADERS,
                                json={"armed": False, "snooze_hours": v})
            self.assertEqual(r.status_code, 422, repr(v))

    def test_routes_run_in_the_threadpool(self):
        import inspect
        app = create_app(self.engine, self.log)
        for route in app.routes:
            ep = getattr(route, "endpoint", None)
            if ep is not None and route.path.startswith("/api/"):
                self.assertFalse(inspect.iscoroutinefunction(ep), route.path)


if __name__ == "__main__":
    unittest.main()
