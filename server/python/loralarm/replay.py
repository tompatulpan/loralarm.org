"""Test harness: run captured ChirpStack up events through the alarm
evaluation, or subscribe live to Mosquitto. Uses the same Engine as the
service, so replay output == service output.

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

from .config import Config
from .event import Uplink
from .service import Engine


def main() -> int:
    ap = argparse.ArgumentParser(description="loralarm test app: replay captured events or live MQTT")
    ap.add_argument("logs", nargs="*", help="captured ChirpStack up-event JSON files")
    ap.add_argument("--config", default=str(Path(__file__).resolve().parent.parent / "devices.example.json"),
                    help="per-device config JSON (default: devices.example.json next to the package)")
    ap.add_argument("--mqtt", metavar="HOST", help="subscribe live to application/+/device/+/event/up (needs paho-mqtt)")
    ap.add_argument("--user", default="tomas", help="Mosquitto username")
    ap.add_argument("--pass", dest="password", default=None, help="Mosquitto password")
    args = ap.parse_args()

    engine = Engine(Config.load(args.config))

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
            engine.on_uplink(Uplink.from_json(msg.payload))

        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        client.username_pw_set(args.user, args.password)
        client.on_connect = on_connect
        client.on_message = on_message
        client.connect(args.mqtt, 1883)
        client.loop_forever()
        return 0

    for path in args.logs:
        engine.on_uplink(Uplink.from_file(path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
