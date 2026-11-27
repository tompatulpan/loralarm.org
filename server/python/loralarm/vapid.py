"""VAPID keypair for the PWA's Web Push channel (PWA design §9/§10).

The pair is generated once per instance and then left alone: every push
subscription is bound to the public key, so a new pair silently breaks every
installed phone (the PWA re-subscribes on key mismatch, but only when someone
opens it and enables push again). The private key stays on the host, in the
service env file — never in the repo.

Run it with the service venv, where `cryptography` is installed (a pywebpush
dependency, requirements-web.txt):

    python3 -m loralarm.vapid > /tmp/vapid.env          # new keypair
    python3 -m loralarm.vapid --check --env-file /etc/loralarm.env

`cryptography` is imported inside the functions, not at module level, so the
stdlib-only alarm path never depends on it. Wire formats — what pywebpush and
webapp._derive_vapid_public expect — are both base64url without padding:

    private  the raw 32-byte P-256 scalar
    public   the uncompressed point 0x04 || X || Y (65 bytes); this is the
             browser's applicationServerKey
"""
from __future__ import annotations

import argparse
import base64
import os
import sys
from datetime import datetime, timezone
from typing import Mapping

DEFAULT_SUB = "mailto:admin@loralarm.org"
ENV_PRIVATE = "LORALARM_VAPID_PRIVATE_KEY"
ENV_PUBLIC = "LORALARM_VAPID_PUBLIC_KEY"
ENV_SUB = "LORALARM_VAPID_SUB"


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _unb64url(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def generate() -> tuple[str, str]:
    """A fresh keypair: (private, public), both base64url."""
    from cryptography.hazmat.primitives.asymmetric import ec
    priv = ec.generate_private_key(ec.SECP256R1())
    raw = priv.private_numbers().private_value.to_bytes(32, "big")
    return b64url(raw), b64url(_public_bytes(priv))


def _public_bytes(priv) -> bytes:
    from cryptography.hazmat.primitives import serialization
    return priv.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


def public_from_private(key: str) -> str:
    """The applicationServerKey for a private key. ValueError on a bad key."""
    from cryptography.hazmat.primitives.asymmetric import ec
    raw = _unb64url(key)
    if len(raw) != 32:
        raise ValueError(f"private key is {len(raw)} bytes, expected 32")
    try:
        priv = ec.derive_private_key(int.from_bytes(raw, "big"), ec.SECP256R1())
    except ValueError as e:      # out of range for the curve order
        raise ValueError(str(e)) from e
    return b64url(_public_bytes(priv))


def _valid_public(key: str) -> bool:
    try:
        raw = _unb64url(key)
    except Exception:
        return False
    return len(raw) == 65 and raw[0] == 0x04


def env_lines(private: str, public: str, sub: str = DEFAULT_SUB) -> str:
    """An env-file block for /etc/loralarm.env (or a house's env file)."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return "\n".join([
        f"# --- PWA web push (VAPID), generated {today}: keep it stable ------------",
        "# A new keypair invalidates the push registration of every installed PWA.",
        f"{ENV_PRIVATE}={private}",
        f"{ENV_PUBLIC}={public}",
        f"{ENV_SUB}={sub}",
    ]) + "\n"


def parse_env_file(path: str) -> dict[str, str]:
    """KEY=VALUE lines from a systemd EnvironmentFile: comments and blank lines
    ignored, surrounding quotes stripped, an `export ` prefix tolerated."""
    values: dict[str, str] = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[len("export "):]
            name, _, value = line.partition("=")
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            values[name.strip()] = value
    return values


def check(env: Mapping[str, str]) -> tuple[str | None, list[str], list[str]]:
    """Validate the configured keypair: (public key, problems, warnings).

    The public key is derived from the private key, so a missing
    LORALARM_VAPID_PUBLIC_KEY is only a warning (webapp derives it too); a
    *wrong* one is a problem: phones subscribe with it and never verify.
    """
    problems: list[str] = []
    warnings: list[str] = []
    private = (env.get(ENV_PRIVATE) or "").strip()
    public = (env.get(ENV_PUBLIC) or "").strip()
    sub = (env.get(ENV_SUB) or "").strip()

    derived: str | None = None
    if not private:
        problems.append(f"{ENV_PRIVATE} is not set: the worker logs a warning and "
                        "drops every push, so alarms stay visual-only in the PWA")
    else:
        try:
            derived = public_from_private(private)
        except Exception as e:
            problems.append(f"{ENV_PRIVATE} is not a base64url P-256 private key "
                            f"({e})")

    if derived:
        if not public:
            warnings.append(f"{ENV_PUBLIC} is not set: /api/push/key derives it "
                            "from the private key")
        elif public != derived:
            problem = (f"{ENV_PUBLIC} does not belong to {ENV_PRIVATE}: phones "
                       "would subscribe with a key that cannot verify a push")
            if not _valid_public(public):
                problem += " (it is not a P-256 uncompressed point either)"
            problems.append(problem)

    if not sub:
        warnings.append(f"{ENV_SUB} is not set: {DEFAULT_SUB} is used, a contact "
                        "address the push service cannot reach")
    elif not (sub.startswith("mailto:") or sub.startswith("https:")):
        problems.append(f"{ENV_SUB} must start with mailto: or https: (got {sub!r})")

    return derived, problems, warnings


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python3 -m loralarm.vapid",
        description="Generate or verify the VAPID keypair for PWA Web Push.")
    ap.add_argument("--check", action="store_true",
                    help="verify the configured keypair instead of generating one")
    ap.add_argument("--env-file", default="",
                    help="read KEY=VALUE from this env file (systemd "
                         "EnvironmentFile); values override the process env")
    ap.add_argument("--sub", default=DEFAULT_SUB,
                    help=f"VAPID subject for a generated pair (default {DEFAULT_SUB})")
    args = ap.parse_args(argv)

    try:
        import cryptography  # noqa: F401 — availability check
    except ImportError:
        print("cryptography is not installed: use the service venv "
              "(pip install -r requirements-web.txt)", file=sys.stderr)
        return 2

    if not args.check:
        private, public = generate()
        sys.stdout.write(env_lines(private, public, args.sub))
        return 0

    env: dict[str, str] = dict(os.environ)
    if args.env_file:
        if not os.path.isfile(args.env_file):
            print(f"no such env file: {args.env_file}", file=sys.stderr)
            return 2
        env.update(parse_env_file(args.env_file))

    derived, problems, warnings = check(env)
    for w in warnings:
        print(f"WARN: {w}")
    for p in problems:
        print(f"ERROR: {p}")
    if derived and not problems:
        print(f"OK: keypair valid, applicationServerKey {derived}")
        print("    phones subscribed with this key receive pushes from this instance")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
