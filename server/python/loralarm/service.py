"""The long-running loralarm service (migration spec §7).

MQTT in -> alarm state machine -> ntfy/Pushover notifications + SQLite
events log. `replay.py` shares the same Engine offline; this module adds
what a service needs that replay does not: a watchdog tick that fires
stale alarms without an uplink, settings-frame logging and a persistent
event history.

    python3 -m loralarm --mqtt <lora-box> --dry-run
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .alarms import LABELS, AlarmState
from .config import Config
from .event import Uplink
from .events import EventLog
from .notify import Notifier


class Engine:
    """Transport-free alarm evaluation for the whole fleet.

    Uplinks in, console lines + transitions out, exactly what replay.py
    printed before - one code path for offline replay and live service.
    """

    def __init__(self, config: Config, event_log: EventLog | None = None,
                 notifier: Notifier | None = None):
        self.config = config
        self.event_log = event_log
        self.notifier = notifier
        # dev_eui -> (config, state); config refreshed per uplink so a
        # config file reload picks up on the next message
        self.devices: dict[str, tuple[Any, AlarmState]] = {}

    def on_uplink(self, up: Uplink) -> None:
        cfg = self.config.for_event(up.dev_eui, up.device_name, up.device_profile_name)
        _, state = self.devices.setdefault(up.dev_eui, (cfg, AlarmState()))
        self.devices[up.dev_eui] = (cfg, state)
        reading, transitions = state.update(cfg, up)
        print(f"{up.time:%Y-%m-%d %H:%M:%S} {up.device_name:<10} {up.device_profile_name:<8} "
              f"fPort={up.f_port} fCnt={up.f_cnt} rssi={reading.rssi} snr={reading.snr}",
              flush=True)
        if up.is_sensor_data:
            print(f"    temp={reading.temp_c} °C  batt={reading.batt_v} V "
                  f"({reading.batt_percent} %, {reading.batt_status})", flush=True)
        text = up.settings_text()
        if text is not None:
            print(f"    settings fPort {up.f_port}: {text!r}", flush=True)
            if self.event_log:
                self.event_log.log(up.time, up.dev_eui, up.device_name,
                                   f"settings_f{up.f_port}", True, text)
        for t in transitions:
            print(f"    >>> {LABELS[t.kind.value]}: {'LARM' if t.active else 'kvitterad'} — {t.detail}",
                  flush=True)
            self._emit(cfg, state, up.time, t)

    def watchdog_tick(self) -> list[str]:
        """Check every known device for staleness; returns alarmed dev_euis."""
        alarmed: list[str] = []
        for dev_eui, (cfg, state) in list(self.devices.items()):
            # match the event tz so naive/aware timestamps never collide
            now = datetime.now(state.last_seen.tzinfo) \
                if state.last_seen and state.last_seen.tzinfo else datetime.now()
            for t in state.watchdog(cfg, now):
                print(f"{now:%Y-%m-%d %H:%M:%S} {cfg.name:<10} watchdog "
                      f">>> {LABELS[t.kind.value]}: {'LARM' if t.active else 'kvitterad'} — {t.detail}",
                      flush=True)
                self._emit(cfg, state, now, t)
                if t.active:
                    alarmed.append(dev_eui)
        return alarmed

    def _emit(self, cfg, state: AlarmState, ts: datetime, t) -> None:
        if self.event_log:
            self.event_log.log(ts, cfg.dev_eui, cfg.name, t.kind.value, t.active, t.detail)
        if self.notifier:
            self.notifier.notify(cfg, t.kind.value, t.active, t.detail)


def main() -> int:
    ap = argparse.ArgumentParser(description="loralarm service: live MQTT alarms, watchdog, notifications")
    ap.add_argument("--mqtt", required=True, metavar="HOST",
                    help="Mosquitto host (subscribes application/+/device/+/event/up)")
    ap.add_argument("--user", default="tomas", help="Mosquitto username")
    ap.add_argument("--pass", dest="password", default=None, help="Mosquitto password")
    ap.add_argument("--config", default=str(Path(__file__).resolve().parent.parent / "devices.example.json"),
                    help="per-device config JSON")
    ap.add_argument("--db", default="loralarm.db", help="SQLite events log (default ./loralarm.db)")
    ap.add_argument("--ntfy-url", default=os.environ.get("LORALARM_NTFY_URL", "https://ntfy.sh"),
                    help="self-hosted ntfy base URL (or env LORALARM_NTFY_URL)")
    ap.add_argument("--watchdog-interval", type=float, default=300.0,
                    help="seconds between watchdog checks (default 300)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print notifications instead of sending ntfy/Pushover")
    args = ap.parse_args()

    try:
        import paho.mqtt.client as mqtt
    except ImportError:
        print("paho-mqtt is not installed: pip install paho-mqtt", file=sys.stderr)
        return 2

    config = Config.load(args.config)
    engine = Engine(config, EventLog(args.db),
                    Notifier(ntfy_url=args.ntfy_url, dry_run=args.dry_run))

    def on_connect(client, _userdata, _flags, reason_code, _properties):
        if reason_code == 0:
            client.subscribe("application/+/device/+/event/up")
            print(f"subscribed to application/+/device/+/event/up on {args.mqtt}:1883", flush=True)
        else:
            print(f"connection refused: {reason_code}", file=sys.stderr, flush=True)

    def on_message(_client, _userdata, msg):
        engine.on_uplink(Uplink.from_json(msg.payload))

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.username_pw_set(args.user, args.password)
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(args.mqtt, 1883)
    client.loop_start()

    print(f"service running: watchdog every {args.watchdog_interval:.0f} s, events -> {args.db}",
          flush=True)
    try:
        while True:
            time.sleep(args.watchdog_interval)
            engine.watchdog_tick()
    except KeyboardInterrupt:
        print("shutting down", flush=True)
    finally:
        client.loop_stop()
        client.disconnect()
    return 0
