"""VAPID keypair helper (loralarm/vapid.py): generate, verify, and the
guarantee that a generated pair is the one /api/push/key serves to the PWA.

No network: the wire format is checked with `cryptography` only.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from loralarm import vapid

needs_crypto = unittest.skipUnless(importlib.util.find_spec("cryptography"),
                                   "VAPID needs cryptography (pywebpush dep)")

needs_web = unittest.skipUnless(
    importlib.util.find_spec("fastapi") and importlib.util.find_spec("httpx")
    and importlib.util.find_spec("cryptography"),
    "web layer needs requirements-web.txt")


def pair() -> tuple[str, str]:
    return vapid.generate()


def write_env(lines: str) -> Path:
    tmp = tempfile.NamedTemporaryFile("w", suffix=".env", delete=False)
    tmp.write(lines)
    tmp.close()
    return Path(tmp.name)


@needs_crypto
class KeypairTest(unittest.TestCase):
    def test_generate_produces_a_matching_pair_in_wire_format(self):
        private, public = pair()
        self.assertEqual(len(vapid._unb64url(private)), 32)
        pub_raw = vapid._unb64url(public)
        self.assertEqual(len(pub_raw), 65)
        self.assertEqual(pub_raw[0], 0x04)               # uncompressed point
        self.assertEqual(vapid.public_from_private(private), public)

    def test_public_from_private_rejects_bad_keys(self):
        for bad in ("", "not-a-key", vapid.b64url(b"short")):
            with self.assertRaises(ValueError, msg=bad):
                vapid.public_from_private(bad)

    def test_env_lines_round_trip_and_pass_the_check(self):
        private, public = pair()
        path = write_env(vapid.env_lines(private, public))
        self.addCleanup(path.unlink)
        env = vapid.parse_env_file(str(path))
        self.assertEqual(env[vapid.ENV_PRIVATE], private)
        self.assertEqual(env[vapid.ENV_PUBLIC], public)
        derived, problems, warnings = vapid.check(env)
        self.assertEqual(derived, public)
        self.assertEqual(problems, [])
        self.assertEqual(warnings, [])

    def test_parse_env_file_ignores_comments_and_strips_quotes(self):
        path = write_env("\n".join([
            "# a comment",
            "export LORALARM_VAPID_PRIVATE_KEY=\"abc\"",
            "",
            "LORALARM_VAPID_SUB='mailto:me@example.org'",
            "LORALARM_VAPID_PUBLIC_KEY=not-a-key",   # last one wins in systemd too
            "LORALARM_VAPID_PUBLIC_KEY=def",
        ]) + "\n")
        self.addCleanup(path.unlink)
        env = vapid.parse_env_file(str(path))
        self.assertEqual(env[vapid.ENV_PRIVATE], "abc")
        self.assertEqual(env[vapid.ENV_SUB], "mailto:me@example.org")
        self.assertEqual(env[vapid.ENV_PUBLIC], "def")

    def test_check_reports_a_missing_private_key(self):
        derived, problems, _ = vapid.check({})
        self.assertIsNone(derived)
        self.assertEqual(len(problems), 1)
        self.assertIn(vapid.ENV_PRIVATE, problems[0])

    def test_check_flags_a_public_key_from_another_pair(self):
        private, _ = pair()
        _, other_public = pair()
        derived, problems, _ = vapid.check({
            vapid.ENV_PRIVATE: private, vapid.ENV_PUBLIC: other_public})
        self.assertNotEqual(derived, other_public)
        self.assertEqual(len(problems), 1)
        self.assertIn("does not belong to", problems[0])

    def test_check_flags_a_garbage_private_key_without_raising(self):
        derived, problems, _ = vapid.check({vapid.ENV_PRIVATE: "xyz"})
        self.assertIsNone(derived)
        self.assertEqual(len(problems), 1)
        self.assertIn("not a base64url P-256 private key", problems[0])

    def test_public_key_alone_is_a_warning_not_a_problem(self):
        private, _ = pair()
        derived, problems, warnings = vapid.check({
            vapid.ENV_PRIVATE: private, vapid.ENV_SUB: "mailto:me@example.org"})
        self.assertEqual(problems, [])
        self.assertEqual(len(warnings), 1)
        self.assertIn(vapid.ENV_PUBLIC, warnings[0])
        self.assertEqual(vapid.public_from_private(private), derived)

    def test_check_flags_a_subject_the_push_service_will_reject(self):
        private, public = pair()
        _, problems, _ = vapid.check({
            vapid.ENV_PRIVATE: private, vapid.ENV_PUBLIC: public,
            vapid.ENV_SUB: "me@example.org"})
        self.assertEqual(len(problems), 1)
        self.assertIn("mailto: or https:", problems[0])


@needs_crypto
class CommandTest(unittest.TestCase):
    def run_main(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = vapid.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_generated_block_checks_clean_on_the_host(self):
        code, out, _ = self.run_main([])
        self.assertEqual(code, 0)
        path = write_env(out)
        self.addCleanup(path.unlink)
        code, out, _ = self.run_main(["--check", "--env-file", str(path)])
        self.assertEqual(code, 0)
        self.assertIn("OK: keypair valid", out)

    def test_check_exits_1_on_a_broken_keypair_and_2_without_the_file(self):
        private, _ = pair()
        _, other_public = pair()
        path = write_env(f"{vapid.ENV_PRIVATE}={private}\n"
                         f"{vapid.ENV_PUBLIC}={other_public}\n")
        self.addCleanup(path.unlink)
        self.assertEqual(self.run_main(["--check", "--env-file", str(path)])[0], 1)
        self.assertEqual(self.run_main(["--check", "--env-file",
                                        str(path) + ".missing"])[0], 2)

    def test_check_reads_the_process_env_when_no_file_is_given(self):
        private, public = pair()
        with mock.patch.dict("os.environ", {vapid.ENV_PRIVATE: private,
                                            vapid.ENV_PUBLIC: public,
                                            vapid.ENV_SUB: "https://loralarm.org"}):
            code, out, _ = self.run_main(["--check"])
        self.assertEqual(code, 0)
        self.assertIn(public, out)


@needs_web
class ServedKeyTest(unittest.TestCase):
    """The key the PWA subscribes with is the one this helper prints."""

    def test_api_push_key_serves_the_generated_public_key(self):
        from fastapi.testclient import TestClient
        from loralarm.events import EventLog
        from loralarm.service import Engine
        from loralarm.webapp import create_app

        from .common import make_config

        private, public = pair()

        class Worker:
            vapid_private_key = private

        log = EventLog(None)
        log.seed_users([{"name": "anna", "token": "token-" + "a" * 32}])
        app = create_app(Engine(make_config(), log, None), log, webpush=Worker(),
                         static_dir=None, cookie_secure=False)
        client = TestClient(app)
        client.post("/api/login", json={"token": "token-" + "a" * 32},
                    headers={"X-Requested-With": "loralarm"})
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(vapid.ENV_PUBLIC, None)
            r = client.get("/api/push/key")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["public_key"], public)


if __name__ == "__main__":
    unittest.main()
