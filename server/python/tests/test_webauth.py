"""Phase 1 data layer: PWA users (config-seeded, tokens hashed), sessions,
subscriptions, push endpoints and acks — all through EventLog."""
from __future__ import annotations

import unittest
from datetime import timedelta

from loralarm.config import Config
from loralarm.events import EventLog, hash_token, utc_now

from .common import DEV_EUI, OTHER_EUI


class UserSeedTest(unittest.TestCase):
    def test_seed_creates_users_with_hashed_tokens_and_config_wins(self):
        log = EventLog(None)
        log.seed_users([{"name": "anna", "token": "secret-1", "role": "admin"},
                        {"name": "bosse", "token": "secret-2"}])
        u = log.user_by_token_hash(hash_token("secret-1"))
        self.assertEqual((u["name"], u["role"]), ("anna", "admin"))
        # raw token is nowhere in the DB
        raw = log._conn.execute("select count(*) from users where token_hash = 'secret-1'"
                                ).fetchone()[0]
        self.assertEqual(raw, 0)
        # re-seed without bosse: he is disabled, anna's token still works
        log.seed_users([{"name": "anna", "token": "secret-1", "role": "admin"}])
        self.assertIsNone(log.user_by_token_hash(hash_token("secret-2")))
        bosse = log._conn.execute("select disabled from users where name='bosse'").fetchone()
        self.assertEqual(bosse[0], 1)
        # rotating anna's token invalidates the old one
        log.seed_users([{"name": "anna", "token": "secret-3", "role": "user"}])
        self.assertIsNone(log.user_by_token_hash(hash_token("secret-1")))
        self.assertEqual(log.user_by_token_hash(hash_token("secret-3"))["role"], "user")

    def test_seed_empty_disables_everyone(self):
        log = EventLog(None)
        log.seed_users([{"name": "anna", "token": "x"}])
        log.seed_users([])
        self.assertIsNone(log.user_by_token_hash(hash_token("x")))

    def test_config_parses_web_users(self):
        cfg = Config({"web_users": [{"name": "anna", "token": "t", "role": "admin"},
                                    {"name": "broken"}]})
        self.assertEqual(cfg.web_users, [{"name": "anna", "token": "t", "role": "admin"}])


class SessionTest(unittest.TestCase):
    def test_session_lifecycle(self):
        log = EventLog(None)
        log.seed_users([{"name": "anna", "token": "t"}])
        uid = log.user_by_token_hash(hash_token("t"))["id"]
        sid = log.create_session(uid, ttl_hours=1)
        self.assertEqual(log.resolve_session(sid)["name"], "anna")
        log.revoke_session(sid)
        self.assertIsNone(log.resolve_session(sid))
        # revoking all of a user's sessions logs them out everywhere
        s2 = log.create_session(uid)
        log.revoke_user_sessions(uid)
        self.assertIsNone(log.resolve_session(s2))

    def test_expired_session_is_invalid(self):
        log = EventLog(None)
        log.seed_users([{"name": "anna", "token": "t"}])
        uid = log.user_by_token_hash(hash_token("t"))["id"]
        sid = log.create_session(uid, ttl_hours=1)
        # age the session beyond expiry directly in the DB
        log._conn.execute("update sessions set expires_at = ?",
                          ((utc_now() - timedelta(hours=1)).isoformat(),))
        log._conn.commit()
        self.assertIsNone(log.resolve_session(sid))

    def test_disabled_user_is_logged_out_immediately(self):
        log = EventLog(None)
        log.seed_users([{"name": "anna", "token": "t"}])
        uid = log.user_by_token_hash(hash_token("t"))["id"]
        sid = log.create_session(uid)
        log.seed_users([])   # anna removed from config -> disabled
        self.assertIsNone(log.resolve_session(sid))


class SubsEndpointAckTest(unittest.TestCase):
    def test_subs_default_is_none_follow_all(self):
        log = EventLog(None)
        log.seed_users([{"name": "anna", "token": "t"}])
        uid = log.user_by_token_hash(hash_token("t"))["id"]
        self.assertIsNone(log.get_subs(uid))
        log.set_subs(uid, [DEV_EUI])
        self.assertEqual(log.get_subs(uid), [DEV_EUI])
        self.assertEqual(log.subscribers(DEV_EUI), [uid])
        self.assertEqual(log.subscribers(OTHER_EUI), [])

    def test_endpoint_upsert_and_remove(self):
        log = EventLog(None)
        log.seed_users([{"name": "anna", "token": "t"}])
        uid = log.user_by_token_hash(hash_token("t"))["id"]
        e1 = log.add_endpoint(uid, "https://push.example/aaa", "p", "a", "ua")
        # same endpoint again: refresh keys, same row
        e2 = log.add_endpoint(uid, "https://push.example/aaa", "p2", "a2")
        self.assertEqual(e1, e2)
        self.assertEqual(log.endpoints_for_users([uid])[0]["p256dh"], "p2")
        self.assertTrue(log.remove_endpoint("https://push.example/aaa"))
        self.assertEqual(log.endpoints_for_users([uid]), [])
        self.assertFalse(log.remove_endpoint("https://push.example/aaa"))

    def test_push_ack_idempotent(self):
        log = EventLog(None)
        log.log_push_ack(7, 3)
        log.log_push_ack(7, 3)
        self.assertEqual(log._conn.execute("select count(*) from push_ack").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
