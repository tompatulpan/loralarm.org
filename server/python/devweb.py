#!/usr/bin/env python3
"""Local PWA dev runner: the real web layer + PWA, no MQTT and no ntfy.

    .venv/bin/python devweb.py            # http://127.0.0.1:8090

Serves the PWA/static from ../../PWA/web against a throw-away in-memory DB
(sessions and acks vanish on restart; the live loralarm.db is never touched).
One simulated RAK4631 ("testsensor") runs in *level* motion mode and toggles
FIRE/CLEAR every AUTO_S seconds (45) — enough to exercise both the red active
banner and the reappear-after-dismiss behavior. Commands on stdin:

    f    fire the motion alarm now        k    clear it now
    n    normal uplink (no alarm)         q    quit

Login with the invite token(s) printed at startup (admin: can disarm
"tills vidare"; user: only snooze presets).
"""
from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from loralarm.config import Config
from loralarm.event import Uplink
from loralarm.events import EventLog
from loralarm.service import Engine

HOST = os.environ.get("LORALARM_WEB_HOST", "127.0.0.1")
PORT = int(os.environ.get("LORALARM_WEB_PORT", "8090"))
AUTO_S = float(os.environ.get("AUTO_S", "45"))      # 0 = only manual commands
EUI = "0aa0b0c0d0e0f010"                            # fake 16-hex devEUI

ADMIN = {"name": "tomas", "token": "dev", "role": "admin"}
USER2 = {"name": "gäst", "token": "gast", "role": "user"}


def lan_ip() -> str:
    """The LAN address the phone can use (best effort; '' when offline)."""
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.0.2.1", 9))        # no traffic is sent; just picks a route
        ip = s.getsockname()[0]
        s.close()
        return ip if not ip.startswith("127.") else ""
    except OSError:
        return ""


def uplink(motion: int, f_cnt: int) -> Uplink:
    return Uplink(dev_eui=EUI, device_name="testsensor", application_name="Larm",
                  device_profile_name="RAK4631", f_port=2, f_cnt=f_cnt,
                  time=datetime.now(timezone.utc), data_b64="",
                  obj={"temperature": 32.0, "battery_voltage": 3.9,
                       "motion_alert": motion, "motion_switch": 0,
                       "accelerometer": {"x": 0.1, "y": 0.0, "z": 1.0}},
                  rssi=-62, snr=9.1)


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    import sys
    sys.stdout.reconfigure(line_buffering=True)       # tokens in the log right away

    # dev-only: print every login attempt exactly as the browser sent it
    # (session-cookie hashes are 64 hex chars and stay quiet). Patched before
    # loralarm.webapp is imported so its `from .events import hash_token`
    # binding picks the wrapper up.
    import loralarm.events as _events
    _orig_hash = _events.hash_token

    def _loud_hash(token: str) -> str:
        if token and len(token) != 64:
            print(f"[devweb] login attempt {token!r}")
        return _orig_hash(token)

    _events.hash_token = _loud_hash
    from loralarm.webapp import WebServer, create_app

    static_dir = Path(__file__).resolve().parents[2] / "PWA" / "web"
    if not (static_dir / "index.html").is_file():
        raise SystemExit(f"PWA not found under {static_dir}")

    log = EventLog(None)                               # throw-away, in-memory DB
    log.seed_users([ADMIN, USER2])
    cfg = Config({"defaults": {"motion_mode": "level"},
                  "devices": {EUI: {"name": "testsensor"}}}, None)
    engine = Engine(cfg, log, None)                    # no notifier, no MQTT
    engine.seed()

    app = create_app(engine, log, webpush=None, static_dir=static_dir,
                     cookie_secure=False)
    WebServer(app, host=HOST, port=PORT).start()

    motion = 0
    fence = threading.Event()

    def counter() -> int:
        # keep f_cnt rising across alarms/normal uplinks
        counter.n += 1
        return counter.n
    counter.n = 0

    def send(next_motion: int) -> None:
        engine.on_uplink(uplink(next_motion, counter()))

    def toggler() -> None:
        nxt = 1
        while not fence.wait(AUTO_S if AUTO_S > 0 else 1e9):
            print(f"[devweb] auto {'FIRE' if nxt else 'CLEAR'} (motion_alert={nxt})")
            send(nxt)
            nxt = 0 if nxt == 1 else 1                  # fire, then clear, then fire...
    threading.Thread(target=toggler, daemon=True).start()

    def keys() -> None:
        while True:
            try:
                cmd = (input() or "").strip().lower()
            except EOFError:
                return                         # no stdin (nohup/CI): manual keys off
            if cmd in ("f", "fire"):
                print("[devweb] FIRE"); send(1)
            elif cmd in ("k", "clear"):
                print("[devweb] CLEAR"); send(0)
            elif cmd in ("n", "normal"):
                print("[devweb] normal uplink"); send(motion)
            elif cmd in ("q", "quit"):
                print("[devweb] bye"); fence.set(); os._exit(0)
            elif cmd:
                print("[devweb] f=fire k=clear n=normal q=quit")
    threading.Thread(target=keys, daemon=True).start()

    print(f"\n  PWA:      http://{HOST}:{PORT}/")
    if HOST not in ("127.0.0.1", "localhost"):
        ip = lan_ip()
        if ip:
            print(f"  phone:    http://{ip}:{PORT}/   (same wifi only)")
    print(f"  admin:    {ADMIN['token']}  ({ADMIN['name']})")
    print(f"  user:     {' ' * 8}{USER2['token']}  ({USER2['name']})")
    print(f"  device:   testsensor {EUI} · motion toggles every {AUTO_S:g} s")
    print("  commands: f=fire  k=clear  n=normal  q=quit\n")
    try:
        while not fence.wait(3600):
            pass                                       # daemon threads do the work
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
