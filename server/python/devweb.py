#!/usr/bin/env python3
"""Local PWA dev runner: the real web layer + PWA, no MQTT and no ntfy.

    .venv/bin/python devweb.py            # http://127.0.0.1:8090

Serves the PWA/static from ../../PWA/web against a throw-away in-memory DB
(sessions and acks vanish on restart; the live loralarm.db is never touched).
A small simulated fleet exercises every state of the fleet view:

    testsensor             RAK4631 *level* motion, toggles FIRE/CLEAR every
                           AUTO_S seconds (45) — red banner + swipe-away
    dorr.baksida           Dragino LDS02 door that is open — latched DÖRRLARM
    dorr.fram              Dragino LDS02 door, DISARMED 8 h (🛡 av + LARM AV)
    vattenmatare.kallare   Browan TBWL100 leak, latched VATTENLARM, weak-ish
                           signal; batt_warn disabled so only water alarms
    temp.vind              RAK4631 at 29.9 °C over a 28 °C limit — HÖG TEMP
    temp.garage            RAK4631 at 7.4 °C, very weak signal (1 bar, red)
    knapp.pannrum          Milesight WS101 button, one 'short press' at boot —
                           recent pulse alarm; heartbeats carry battery only

Commands on stdin:

    f    fire the motion alarm now        k    clear it now
    n    normal uplink (no alarm)         q    quit

Login with the invite token(s) printed at startup (admin: can disarm
"tills vidare"; user: only snooze presets).
"""
from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from loralarm.config import Config
from loralarm.event import Uplink
from loralarm.events import EventLog
from loralarm.service import Engine

HOST = os.environ.get("LORALARM_WEB_HOST", "127.0.0.1")
PORT = int(os.environ.get("LORALARM_WEB_PORT", "8090"))
AUTO_S = float(os.environ.get("AUTO_S", "45"))      # 0 = only manual motion commands
HB_S = float(os.environ.get("HB_S", "30"))          # fleet heartbeat interval

EUI = "0aa0b0c0d0e0f010"                            # testsensor (motion toggle)
EUI_DOOR = "0aa0b0c0d0e0f020"                      # dorr.baksida (open — latched)
EUI_WATER = "0aa0b0c0d0e0f030"                      # vattenmatare.kallare (leak)
EUI_HOT = "0aa0b0c0d0e0f040"                        # temp.vind (temp_high active)
EUI_GARAGE = "0aa0b0c0d0e0f050"                     # temp.garage (weak signal)
EUI_DOOR2 = "0aa0b0c0d0e0f060"                      # dorr.fram (disarmed 8 h)
EUI_BTN = "0aa0b0c0d0e0f070"                        # knapp.pannrum (pulse at boot)

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


def uplink(eui: str, name: str, family: str, f_port: int, obj: dict,
           rssi: int, snr: float, f_cnt: int) -> Uplink:
    return Uplink(dev_eui=eui, device_name=name, application_name="Larm",
                  device_profile_name=family, f_port=f_port, f_cnt=f_cnt,
                  time=datetime.now(timezone.utc), data_b64="", obj=obj,
                  rssi=rssi, snr=snr)


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
                  "devices": {
                      EUI: {"name": "testsensor", "family": "RAK4631"},
                      EUI_DOOR: {"name": "dorr.baksida",
                                 "family": "Dragino Switch LDS02"},
                      EUI_WATER: {"name": "vattenmatare.kallare",
                                  "family": "Browan Water sensor",
                                  "batt_warn_v": 0.0},   # only the water alarm
                      EUI_HOT: {"name": "temp.vind", "family": "RAK4631",
                                "tk_correction_c": 0.0,       # sim: raw value on screen
                                "temp_alarm_max_c": 28.0},
                      EUI_GARAGE: {"name": "temp.garage", "family": "RAK4631-R",
                                   "tk_correction_c": 0.0,
                                   "temp_alarm_min_c": -20.0},   # sim: no false frost alarm
                      EUI_DOOR2: {"name": "dorr.fram",
                                  "family": "Dragino Switch LDS02"},
                      EUI_BTN: {"name": "knapp.pannrum",
                                 "family": "Smart Button [Milesight]"},
                  }}, None)

    engine = Engine(cfg, log, None)                    # no notifier, no MQTT
    engine.seed()

    # dorr.fram is snoozed 8 h from now: the card shows 🛡 larm av till HH:MM
    # and pill LARM AV (the auto re-arm lands inside EventLog's watchdog)
    log.set_armed(EUI_DOOR2, False,
                  until=datetime.now(timezone.utc) + timedelta(hours=8),
                  changed_by="devweb")

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
        engine.on_uplink(uplink(EUI, "testsensor", "RAK4631", 2,
                                {"temperature": 24.0, "battery_voltage": 3.9,
                                 "motion_alert": next_motion, "motion_switch": 0,
                                 "version_byte": 32,
                                 "accelerometer": {"x": 0.1, "y": 0.0, "z": 1.0}},
                                -62, 9.1, counter()))

    # the fixture fleet: state-keeping uplinks, sent at boot and every HB_S.
    # Latched alarms stay latched because the heartbeat repeats the condition
    # (door still open, water still wet, temp still high).
    def heartbeat(dev=None) -> None:
        for eui, name, family, port, obj, rssi, snr in [
            (EUI_DOOR, "dorr.baksida", "Dragino Switch LDS02", 10,
             {"BAT_V": 2.9, "DOOR_OPEN_STATUS": 1}, -71, 5.5),
            (EUI_WATER, "vattenmatare.kallare", "Browan Water sensor", 106,
             {"battery": 3.3, "status": 1}, -89, 0.5),
            (EUI_HOT, "temp.vind", "RAK4631", 2,
             {"temperature": 29.9, "battery_voltage": 4.05, "version_byte": 31}, -55, 10.0),
            (EUI_GARAGE, "temp.garage", "RAK4631-R", 2,
             {"temperature": 7.4, "battery_voltage": 3.55, "version_byte": 33}, -104, -7.0),
            (EUI_DOOR2, "dorr.fram", "Dragino Switch LDS02", 10,
             {"BAT_V": 2.95, "DOOR_OPEN_STATUS": 0}, -63, 6.0),
            (EUI_BTN, "knapp.pannrum", "Smart Button [Milesight]", 85,
             {"battery": 60}, -68, 7.0),
        ]:
            if dev is not None and eui != dev:
                continue
            engine.on_uplink(uplink(eui, name, family, port, obj, rssi, snr,
                                   counter()))

    # one button press: a pulse (recent) alarm; heartbeats never repeat it
    engine.on_uplink(uplink(EUI_BTN, "knapp.pannrum", "Smart Button [Milesight]", 85,
                            {"battery": 60, "button_event": {"status": "short press"}},
                            -68, 7.0, counter()))
    heartbeat()

    def toggler() -> None:
        nxt = 1
        while not fence.wait(AUTO_S if AUTO_S > 0 else 1e9):
            print(f"[devweb] auto {'FIRE' if nxt else 'CLEAR'} (motion_alert={nxt})")
            send(nxt)
            nxt = 0 if nxt == 1 else 1                  # fire, then clear, then fire...
    threading.Thread(target=toggler, daemon=True).start()

    def heartbeater() -> None:
        while not fence.wait(HB_S if HB_S > 0 else 1e9):
            heartbeat()
    threading.Thread(target=heartbeater, daemon=True).start()

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
            elif cmd in ("h", "hb"):
                print("[devweb] heartbeat now"); heartbeat()
            elif cmd in ("q", "quit"):
                print("[devweb] bye"); fence.set(); os._exit(0)
            elif cmd:
                print("[devweb] f=fire k=clear n=normal h=heartbeat q=quit")
    threading.Thread(target=keys, daemon=True).start()

    print(f"\n  PWA:      http://{HOST}:{PORT}/")
    if HOST not in ("127.0.0.1", "localhost"):
        ip = lan_ip()
        if ip:
            print(f"  phone:    http://{ip}:{PORT}/   (same wifi only)")
    print(f"  admin:    {ADMIN['token']}  ({ADMIN['name']})")
    print(f"  user:     {' ' * 8}{USER2['token']}  ({USER2['name']})")
    print("  fleet:    testsensor motion toggles every %g s; door+water+temp"
          " larmar, knappen pulserar, dorr.fram är avslagen 8 h" % AUTO_S)
    print("  commands: f=fire  k=clear  n=normal  h=heartbeat  q=quit\n")
    try:
        while not fence.wait(3600):
            pass                                       # daemon threads do the work
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
