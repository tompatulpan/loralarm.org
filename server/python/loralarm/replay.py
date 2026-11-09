"""First test application: run captured ChirpStack up events through the alarm
evaluation, or subscribe live to Mosquitto.

    cd server/python
    python3 -m loralarm.replay ../chirpstack/log/*.json --config devices.example.json
    python3 -m loralarm.replay --mqtt <lora-box-host>     # live, needs paho-mqtt

On the lora box the same feed can be inspected with:
    mosquitto_sub -h localhost -t 'application/+/device/+/event/up' -v
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .alarms import AlarmState, Transition
from .config import Config
from .event import Uplink

LABELS = {
    "temp": "Temperaturalarm",
    "battery_low": "Batterinivå låg",
    "battery_empty": "Batteri tomt",
    "motion": "Rörelselarm",
    "circuit": "Kretslarm",
    "tilt": "Tiltlarm",
    "stale": "Ingen kontakt (watchdog)",
}


def handle(config: Config, states: dict[str, AlarmState], up: Uplink) -> None:
    cfg = config.for_event(up.dev_eui, up.device_name, up.device_profile_name)
    state = states.setdefault(up.dev_eui, AlarmState())
    reading, transitions = state.update(cfg, up)
    print(f"{up.time:%Y-%m-%d %H:%M:%S} {up.device_name:<10} {up.device_profile_name:<8} "
          f"fPort={up.f_port} fCnt={up.f_cnt} rssi={reading.rssi} snr={reading.snr}")
    if up.is_sensor_data:
        print(f"    temp={reading.temp_c} °C  batt={reading.batt_v} V "
              f"({reading.batt_percent} %, {reading.batt_status})")
    else:
        print(f"    settings fPort {up.f_port}: {up.settings_text()!r}")
    for t in transitions:
        print(f"    >>> {LABELS[t.kind.value]}: {'LARM' if t.active else 'kvitterad'} — {t.detail}")


def main() -> int:
    ap = argparse.ArgumentParser(description="loralarm test app: replay captured events or live MQTT")
    ap.add_argument("logs", nargs="*", help="captured ChirpStack up-event JSON files")
    ap.add_argument("--config", default=str(Path(__file__).resolve().parent.parent / "devices.example.json"),
                    help="per-device config JSON (default: devices.example.json next to the package)")
    ap.add_argument("--mqtt", metavar="HOST", help="subscribe live to application/+/device/+/event/up (needs paho-mqtt)")
    ap.add_argument("--user", default="tomas", help="Mosquitto username")
    ap.add_argument("--pass", dest="password", default=None, help="Mosquitto password")
    args = ap.parse_args()

    config = Config.load(args.config)
    states: dict[str, AlarmState] = {}

    if args.mqtt:
        try:
            import paho.mqtt.client as mqtt
        except ImportError:
            print("paho-mqtt is not installed: pip install paho-mqtt", file=sys.stderr)
            return 2

        def on_connect(client, _userdata, _flags, reason_code, _properties):
            if reason_code == 0:
                client.subscribe("application/+/device/+/event/up")
                print(f"subscribed to application/+/device/+/event/up on {args.mqtt}:1883")
            else:
                print(f"connection refused: {reason_code}", file=sys.stderr)

        def on_message(_client, _userdata, msg):
            handle(config, states, Uplink.from_json(msg.payload))

        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        client.username_pw_set(args.user, args.password)
        client.on_connect = on_connect
        client.on_message = on_message
        client.connect(args.mqtt, 1883)
        client.loop_forever()
        return 0

    for path in args.logs:
        handle(config, states, Uplink.from_file(path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
