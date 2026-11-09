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
import os
import sys
from pathlib import Path

from . import mqttclient
from .config import Config
from .event import Uplink
from .service import Engine


def main() -> int:
    ap = argparse.ArgumentParser(description="loralarm test app: replay captured events or live MQTT")
    ap.add_argument("logs", nargs="*", help="captured ChirpStack up-event JSON files")
    ap.add_argument("--config", default=str(Path(__file__).resolve().parent.parent / "devices.example.json"),
                    help="per-device config JSON (default: devices.example.json next to the package)")
    ap.add_argument("--mqtt", metavar="HOST", help="subscribe live to application/+/device/+/event/up (needs paho-mqtt)")
    ap.add_argument("--port", type=int, default=1883, help="MQTT port (default 1883)")
    ap.add_argument("--tls", action="store_true", help="use TLS for the MQTT connection")
    ap.add_argument("--user", default=os.environ.get("LORALARM_MQTT_USER"), help="Mosquitto username")
    ap.add_argument("--pass", dest="password", default=None,
                    help="Mosquitto password (prefer env LORALARM_MQTT_PASSWORD)")
    args = ap.parse_args()

    engine = Engine(Config.load(args.config))

    if args.mqtt:
        try:
            import paho.mqtt.client as mqtt  # noqa: F401
        except ImportError:
            print("paho-mqtt is not installed: pip install paho-mqtt", file=sys.stderr)
            return 2

        password = args.password or os.environ.get("LORALARM_MQTT_PASSWORD")
        client = mqttclient.make_client(
            args.mqtt, args.port, args.user, password,
            on_uplink=lambda msg: engine.on_uplink(Uplink.from_json(msg.payload)),
            tls=args.tls,
        )
        mqttclient.connect_loop(client, args.mqtt, args.port)
        client.loop_forever()
        return 0

    for path in args.logs:
        engine.on_uplink(Uplink.from_file(path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
