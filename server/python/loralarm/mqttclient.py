"""Shared paho-mqtt wiring for service.py, replay.py and downlink.py.

One place builds the client, subscribes to the ChirpStack uplink topic and
guards the callbacks: an exception inside on_message is logged, never raised —
with paho 2.x a raised callback exception kills the network thread while the
main thread keeps running, and the service would silently receive nothing.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Callable

log = logging.getLogger(__name__)

TOPIC_UP = "application/+/device/+/event/up"


def topic_matches(subscription: str, topic: str) -> bool:
    """MQTT wildcard match ('+' one level, '#' the rest); avoids depending on
    paho's matcher API, which moved between 1.x and 2.x."""
    sparts = subscription.split("/")
    tparts = topic.split("/")
    for i, sp in enumerate(sparts):
        if sp == "#":
            return True
        if i >= len(tparts):
            return False
        if sp != "+" and sp != tparts[i]:
            return False
    return len(sparts) == len(tparts)


def make_client(host: str, port: int, user: str | None, password: str | None,
                on_uplink: Callable[[object], None], *,
                tls: bool = False,
                client_id: str = "", persistent: bool = False,
                on_connect: Callable[[object], None] | None = None,
                on_disconnect: Callable[[object], None] | None = None,
                extra_subscriptions: tuple[tuple[str, Callable[[object], None]], ...] = ()):
    """Build a paho client subscribed to TOPIC_UP (plus `extra_subscriptions`).

    `on_uplink(msg)` receives every uplink message; each extra subscription is a
    (topic, callback) pair — e.g. the downlink command topic. All callbacks are
    wrapped in try/except so a malformed payload can never kill the network thread.

    `persistent=True` (needs a fixed `client_id`) keeps the broker-side session
    and subscribes at QoS 1, so uplinks published at QoS>=1 while the service
    was down are delivered on reconnect. Two clients with the same id kick each
    other off, so only the service uses it.
    """
    import paho.mqtt.client as mqtt

    if persistent and not client_id:
        raise ValueError("a persistent MQTT session needs a fixed client_id")
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id,
                         clean_session=not persistent)
    if user:
        client.username_pw_set(user, password)
    extra = dict(extra_subscriptions)

    def _on_connect(client, _userdata, _flags, reason_code, _properties):
        if reason_code == 0:
            client.subscribe(TOPIC_UP, qos=1 if persistent else 0)
            for sub in extra:
                client.subscribe(sub, qos=1)
            log.info("subscribed to %s%s on %s:%s", TOPIC_UP,
                     "".join(f", {s}" for s in extra), host, port)
        else:
            log.error("connection refused by %s:%s: %s", host, port, reason_code)
        if on_connect:
            on_connect(reason_code)

    def _on_message(_client, _userdata, msg):
        try:
            for sub, cb in extra.items():
                if topic_matches(sub, msg.topic):
                    cb(msg)
                    return
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


def make_publisher(host: str, port: int, user: str | None, password: str | None, *,
                   tls: bool = False, client_id: str = ""):
    """A short-lived client for one-shot publishes (downlink CLI): clean
    session, unique client id, no subscriptions."""
    import paho.mqtt.client as mqtt

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                         client_id=client_id or f"loralarm-pub-{os.getpid()}",
                         clean_session=True)
    if user:
        client.username_pw_set(user, password)
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
