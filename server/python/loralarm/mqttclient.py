"""Shared paho-mqtt wiring for service.py and replay.py.

One place builds the client, subscribes to the ChirpStack uplink topic and
guards the callbacks: an exception inside on_message is logged, never raised —
with paho 2.x a raised callback exception kills the network thread while the
main thread keeps running, and the service would silently receive nothing.
"""
from __future__ import annotations

import logging
import time
from typing import Callable

log = logging.getLogger(__name__)

TOPIC_UP = "application/+/device/+/event/up"


def make_client(host: str, port: int, user: str | None, password: str | None,
                on_uplink: Callable[[object], None], *,
                tls: bool = False,
                on_connect: Callable[[object], None] | None = None,
                on_disconnect: Callable[[object], None] | None = None):
    """Build a paho client subscribed to TOPIC_UP.

    `on_uplink(msg)` receives every uplink message; it is wrapped in
    try/except so a malformed payload can never kill the network thread.
    """
    import paho.mqtt.client as mqtt

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    if user:
        client.username_pw_set(user, password)

    def _on_connect(client, _userdata, _flags, reason_code, _properties):
        if reason_code == 0:
            client.subscribe(TOPIC_UP)
            log.info("subscribed to %s on %s:%s", TOPIC_UP, host, port)
        else:
            log.error("connection refused by %s:%s: %s", host, port, reason_code)
        if on_connect:
            on_connect(reason_code)

    def _on_message(_client, _userdata, msg):
        try:
            on_uplink(msg)
        except Exception:
            log.exception("unhandled error while processing message on %s", msg.topic)

    def _on_disconnect(_client, _userdata, _flags, reason_code, _properties):
        log.warning("disconnected from %s:%s: %s", host, port, reason_code)
        if on_disconnect:
            on_disconnect(reason_code)

    client.on_connect = _on_connect
    client.on_message = _on_message
    client.on_disconnect = _on_disconnect
    if tls:
        client.tls_set()
    return client


def connect_loop(client, host: str, port: int, keepalive: int = 60,
                 retry_delay: float = 10.0) -> None:
    """client.connect() with retry — the broker may be down at startup;
    without this the service would just crash and never come up."""
    while True:
        try:
            client.connect(host, port, keepalive)
            return
        except OSError as e:
            log.warning("broker %s:%s unreachable (%s); retrying in %.0f s",
                        host, port, e, retry_delay)
            time.sleep(retry_delay)
