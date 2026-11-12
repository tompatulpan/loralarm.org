"""Phase 2: the FastAPI web layer — auth (invite token -> session cookie,
lockout), CSRF, rate limit, and the /api/* routes over Engine.snapshot() and
EventLog. Uses fastapi's TestClient (httpx), no network."""
from __future__ import annotations

import unittest
from datetime import timedelta

from loralarm.alarms import AlarmKind
from loralarm.events import EventLog, hash_token
from loralarm.service import Engine
from loralarm.webapp import COOKIE, LOGIN_LOCKOUT_AFTER, create_app

from .common import DEV_EUI, OTHER_EUI, T0, make_config, make_uplink, sensor_obj

ADMIN = {"name": "anna", "token": "admin-token-" + "a" * 32, "role": "admin"}
USER = {"name": "bosse", "token": "user-token-" + "b" * 32, "role": "user"}

HEADERS = {"X-Requested-With": "loralarm"}


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
    return client.post("/api/login", json={"token": token})


class AuthTest(unittest.TestCase):
    def test_bad_token_401_and_lockout_after_n_failures(self):
        client, _, _ = make_client()
        for _ in range(LOGIN_LOCKOUT_AFTER - 1):
            self.assertEqual(login(client, "wrong").status_code, 401)
        self.assertEqual(login(client, "wrong").status_code, 429)

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


class CsrfTest(unittest.TestCase):
    def test_state_change_requires_header(self):
        client, _, _ = make_client()
        login(client, USER["token"])
        self.assertEqual(client.put("/api/subscriptions", json={"devices": []}).status_code, 403)
        r = client.put("/api/subscriptions", json={"devices": [DEV_EUI]}, headers=HEADERS)
        self.assertEqual(r.status_code, 200)


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


class PushApiTest(unittest.TestCase):
    def test_subscribe_ack_remove(self):
        client, _, log = make_client()
        login(client, USER["token"])
        sub = {"endpoint": "https://push.example/xyz",
               "keys": {"p256dh": "p", "auth": "a"}}
        r = client.post("/api/push/subscribe", json=sub, headers=HEADERS)
        self.assertEqual(r.status_code, 200)
        eid = r.json()["endpoint_id"]
        client.post("/api/push/ack", json={"event_id": 5, "endpoint": sub["endpoint"]})
        self.assertEqual(log._conn.execute(
            "select count(*) from push_ack where event_id=5").fetchone()[0], 1)
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


if __name__ == "__main__":
    unittest.main()
