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

    def test_swapping_tokens_between_users_works(self):
        log = EventLog(None)
        log.seed_users([{"name": "a", "token": "x"}, {"name": "b", "token": "y"}])
        log.seed_users([{"name": "a", "token": "y"}, {"name": "b", "token": "x"}])
        self.assertEqual(log.user_by_token_hash(hash_token("y"))["name"], "a")
        self.assertEqual(log.user_by_token_hash(hash_token("x"))["name"], "b")

    def test_duplicate_token_rolls_back_and_raises(self):
        import sqlite3
        log = EventLog(None)
        log.seed_users([{"name": "a", "token": "x"}])
        with self.assertRaises(sqlite3.Error):
            log.seed_users([{"name": "a", "token": "x"}, {"name": "b", "token": "x"}])
        self.assertEqual(log.user_by_token_hash(hash_token("x"))["name"], "a")
        self.assertEqual(log._conn.execute("select count(*) from users").fetchone()[0], 1)

    def test_seed_web_users_never_raises_into_the_service(self):
        from loralarm.service import seed_web_users

        class Cfg:
            web_users = [{"name": "a", "token": "x"}, {"name": "b", "token": "x"}]
        seed_web_users(EventLog(None), Cfg())   # logged, not raised
        seed_web_users(None, Cfg())

    def test_config_rejects_bad_entries_without_logging_tokens(self):
        with self.assertLogs("loralarm.config", level="WARNING") as cm:
            cfg = Config({"web_users": [
                {"token": "SECRET-NO-NAME"},
                {"name": "a", "token": "tok-1", "role": "Admin"},
                {"name": "b", "token": "tok-2"},
                {"name": "b", "token": "tok-3"},          # duplicate name
                {"name": "c", "token": "tok-2"},          # duplicate token
            ]})
        self.assertEqual([u["name"] for u in cfg.web_users], ["b"])
        self.assertNotIn("SECRET-NO-NAME", "\n".join(cm.output))
        self.assertNotIn("tok-", "\n".join(cm.output))

    def test_rotated_token_and_disabled_user_lose_their_sessions(self):
        log = EventLog(None)
        log.seed_users([{"name": "a", "token": "x"}, {"name": "b", "token": "y"}])
        a = log.user_by_token_hash(hash_token("x"))["id"]
        b = log.user_by_token_hash(hash_token("y"))["id"]
        sa, sb = log.create_session(a), log.create_session(b)
        log.seed_users([{"name": "a", "token": "x2"}])    # a rotated, b removed
        log.seed_users([{"name": "a", "token": "x2"}, {"name": "b", "token": "y"}])  # b back
        self.assertIsNone(log.resolve_session(sa))
        self.assertIsNone(log.resolve_session(sb))         # not revived by re-adding

    def test_unchanged_reseed_keeps_sessions_and_session_ids_are_hashed(self):
        log = EventLog(None)
        log.seed_users([{"name": "a", "token": "x"}])
        a = log.user_by_token_hash(hash_token("x"))["id"]
        sid = log.create_session(a)
        log.seed_users([{"name": "a", "token": "x"}])
        self.assertEqual(log.resolve_session(sid)["name"], "a")
        stored = log._conn.execute("select id from sessions").fetchone()[0]
        self.assertNotEqual(stored, sid)
        self.assertEqual(stored, hash_token(sid))

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
    def test_default_is_follow_all_and_mutes_opt_out(self):
        log = EventLog(None)
        log.seed_users([{"name": "anna", "token": "t"}, {"name": "bosse", "token": "u"}])
        anna = log.user_by_token_hash(hash_token("t"))["id"]
        bosse = log.user_by_token_hash(hash_token("u"))["id"]
        # nothing chosen: everyone gets every device, including new ones
        self.assertEqual(sorted(log.subscribers(DEV_EUI)), sorted([anna, bosse]))
        self.assertEqual(log.get_muted(anna), [])
        log.set_muted(anna, [DEV_EUI])
        self.assertEqual(log.get_muted(anna), [DEV_EUI])
        self.assertEqual(log.subscribers(DEV_EUI), [bosse])
        self.assertEqual(sorted(log.subscribers(OTHER_EUI)), sorted([anna, bosse]))
        # clearing the mutes goes back to following everything
        log.set_muted(anna, [])
        self.assertEqual(sorted(log.subscribers(DEV_EUI)), sorted([anna, bosse]))

    def test_disabled_users_are_not_subscribers(self):
        log = EventLog(None)
        log.seed_users([{"name": "anna", "token": "t"}])
        log.seed_users([])
        self.assertEqual(log.subscribers(DEV_EUI), [])

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

    def test_push_ack_idempotent_and_owner_checked(self):
        log = EventLog(None)
        log.seed_users([{"name": "anna", "token": "t"}, {"name": "bosse", "token": "u"}])
        anna = log.user_by_token_hash(hash_token("t"))["id"]
        bosse = log.user_by_token_hash(hash_token("u"))["id"]
        ep = log.add_endpoint(anna, "https://push.example/aaa", "p", "a")
        self.assertTrue(log.log_push_ack(anna, 7, ep))
        self.assertTrue(log.log_push_ack(anna, 7, ep))
        self.assertEqual(log._conn.execute("select count(*) from push_ack").fetchone()[0], 1)
        # bosse cannot ack on anna's endpoint
        self.assertFalse(log.log_push_ack(bosse, 8, ep))
        self.assertEqual(log._conn.execute("select count(*) from push_ack").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
