"""The long-running loralarm service (migration spec §7).

MQTT in -> alarm state machine -> ntfy/Pushover notifications + SQLite
events log. `replay.py` shares the same Engine offline; this module adds
what a service needs that replay does not: a watchdog tick that fires
stale alarms without an uplink, settings-frame logging, a persistent
event history and seeded/persisted device state across restarts.

    python3 -m loralarm --mqtt <lora-box> --config devices.json \
        --ntfy-url http://<lora-box>:8075

MQTT credentials: --pass (visible in ps/history, avoid), --pass-file, or the
LORALARM_MQTT_PASSWORD environment variable.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import signal
import sqlite3
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from . import mqttclient
from .alarms import LABELS, AlarmState
from .config import Config, DeviceConfig
from .downlink import (DEFAULT_DL_TOPIC, DL_OBJ_FIELD, DL_PORT_KEY, Downlink,
                       DownlinkSender, desired_frames, parse_command_message)
from .event import Uplink
from .events import EventLog, utc_now
from .notify import Notifier

log = logging.getLogger(__name__)

# exit with this code if the broker connection is lost for longer than this,
# so systemd (Restart=on-failure) brings the service back
MAX_DISCONNECTED_S = 180.0

TOPIC_UP = mqttclient.TOPIC_UP


class Engine:
    """Transport-free alarm evaluation for the whole fleet.

    Uplinks in, console lines + transitions out, exactly what replay.py
    printed before - one code path for offline replay and live service.
    All entry points take one lock: on_uplink runs on the paho network
    thread, watchdog_tick on the main thread, and both mutate AlarmState.
    """

    def __init__(self, config: Config, event_log: EventLog | None = None,
                 notifier: Notifier | None = None, config_path: str | Path | None = None,
                 downlink_sender: DownlinkSender | None = None, dl_topic: str = ""):
        self.config = config
        self.event_log = event_log
        self.notifier = notifier
        self.config_path = Path(config_path) if config_path else None
        self._config_mtime: float | None = None
        if self.config_path and self.config_path.exists():
            self._config_mtime = self.config_path.stat().st_mtime
        # dev_eui -> (config, state)
        self.devices: dict[str, tuple[Any, AlarmState]] = {}
        # dev_eui -> ChirpStack application id, learned from uplink events
        # (deviceInfo.applicationId); the downlink topic needs it
        self.app_ids: dict[str, str] = {}
        self.downlink_sender = downlink_sender
        self.dl_topic = dl_topic
        # devices whose auto-downlink is disabled after a config/send error,
        # so one bad device does not spam the log on every uplink; cleared on
        # each successful config reload
        self._dl_bad: set[str] = set()
        self._lock = threading.Lock()

    def _maybe_reload_config(self) -> None:
        """Reload the config file if it changed on disk (call with the lock held)."""
        if self.config_path is None:
            return
        try:
            mtime = self.config_path.stat().st_mtime
        except OSError:
            return
        if mtime == self._config_mtime:
            return
        # remember the mtime even on failure so a broken file is reported once,
        # not on every uplink; the previous good config stays active
        self._config_mtime = mtime
        try:
            self.config = Config.load(self.config_path)
        except Exception as e:  # bad JSON, wrong shape, unreadable file
            log.error("config reload from %s failed (%s: %s); keeping previous config",
                      self.config_path, type(e).__name__, e)
            return
        log.info("reloaded config from %s", self.config_path)
        self._dl_bad.clear()
        self._push_changed_downlinks()

    def seed(self, now: datetime | None = None) -> None:
        """Create state for every configured device, seeded from the persisted
        snapshot / event history, so a device that was already silent before a
        restart still gets its watchdog alarm."""
        now = now or utc_now()
        with self._lock:
            # configured devices plus every device an earlier run has seen, so an
            # unconfigured sensor that went silent before a restart is still watched
            known = list(self.config.devices)
            if self.event_log:
                try:
                    known += [d for d in self.event_log.known_devices() if d not in self.config.devices]
                except sqlite3.Error:
                    log.exception("could not list saved devices")
            for dev_eui in known:
                if dev_eui in self.devices:
                    continue
                state = AlarmState()
                name = family = ""
                if self.event_log:
                    try:
                        snap = self.event_log.load_state(dev_eui)
                        if snap:
                            state.restore(snap)
                            # identity from the last uplink, so alarms for a device that
                            # stays silent after a restart still read "Door1", not the devEUI
                            name = str(snap.get("name") or "")
                            family = str(snap.get("family") or "")
                    except (sqlite3.Error, ValueError, TypeError, KeyError, AttributeError):
                        log.exception("unreadable state snapshot for %s; starting fresh", dev_eui)
                        state = AlarmState()
                        name = family = ""
                    if not name or name == dev_eui:
                        try:
                            name = self.event_log.last_name(dev_eui)
                        except sqlite3.Error:
                            name = ""
                    if state.last_seen is None:
                        state.last_seen = self.event_log.last_seen(dev_eui)
                if state.last_seen is None:
                    state.last_seen = now
                cfg = self.config.for_event(dev_eui, name, family)
                self.devices[dev_eui] = (cfg, state)
                log.info("seeded %s (%s), last_seen=%s, active=%s",
                         dev_eui, cfg.name, state.last_seen,
                         sorted(k.value for k in state.active))

    def on_uplink(self, up: Uplink) -> None:
        with self._lock:
            self._maybe_reload_config()
            cfg = self.config.for_event(up.dev_eui, up.device_name, up.device_profile_name)
            _, state = self.devices.setdefault(up.dev_eui, (cfg, AlarmState()))
            self.devices[up.dev_eui] = (cfg, state)
            if up.application_id:
                self.app_ids[up.dev_eui] = up.application_id

            if up.is_sensor_data and up.obj:
                reading, transitions = state.update(cfg, up)
                self._enforce_downlinks(up.dev_eui, cfg, up.obj)
            else:
                # settings frame (fPort 4-7) or codec failure: only refresh
                # last_seen, never evaluate and never clear active alarms
                state.touch(up)
                reading, transitions = None, []

            print(f"{up.time:%Y-%m-%d %H:%M:%S} {up.device_name:<10} {up.device_profile_name:<8} "
                  f"fPort={up.f_port} fCnt={up.f_cnt}"
                  + (f" rssi={reading.rssi} snr={reading.snr}" if reading else ""),
                  flush=True)
            if reading is not None:
                print(f"    temp={reading.temp_c} °C  batt={reading.batt_v} V "
                      f"({reading.batt_percent} %, {reading.batt_status})", flush=True)
            text = up.settings_text()
            if text is not None:
                print(f"    settings fPort {up.f_port}: {text!r}", flush=True)
                self._log_event(up.time, up.dev_eui, up.device_name,
                                f"settings_f{up.f_port}", False, text)
            for t in transitions:
                print(f"    >>> {LABELS[t.kind.value]}: {'LARM' if t.active else 'kvitterad'} — {t.detail}",
                      flush=True)
                self._emit(cfg, up.time, t)
            self._persist(up.dev_eui, state)

    def watchdog_tick(self) -> list[str]:
        """Check every known device for staleness; returns alarmed dev_euis."""
        alarmed: list[str] = []
        with self._lock:
            now = utc_now()
            self._maybe_reload_config()
            for dev_eui, (old_cfg, state) in list(self.devices.items()):
                # re-resolve so reloaded thresholds apply and seeded devices
                # pick up their family defaults once the profile is known
                cfg = self.config.for_event(dev_eui, old_cfg.name, old_cfg.family)
                self.devices[dev_eui] = (cfg, state)
                for t in state.watchdog(cfg, now):
                    print(f"{now:%Y-%m-%d %H:%M:%S} {cfg.name:<10} watchdog "
                          f">>> {LABELS[t.kind.value]}: {'LARM' if t.active else 'kvitterad'} — {t.detail}",
                          flush=True)
                    self._emit(cfg, now, t)
                    if t.active:
                        alarmed.append(dev_eui)
                self._persist(dev_eui, state)
        return alarmed

    # ---- downlinks (spec §7.4) ----

    _EUI_RE = re.compile(r"^[0-9a-f]{16}$")

    def resolve_device(self, identifier: str) -> tuple[str, DeviceConfig]:
        """devEUI + config for a devEUI or a device name, from live state first
        (so a name maps to the real devEUI), then from config devEUI keys."""
        ident = identifier.strip().lower()
        with self._lock:
            if ident in self.devices:
                dev_eui, (cfg, _state) = ident, self.devices[ident]
                return dev_eui, cfg
            for eui, (cfg, _state) in self.devices.items():
                if cfg.name.lower() == ident:
                    return eui, cfg
            if self._EUI_RE.match(ident) and ident in self.config.devices:
                cfg = self.config.for_event(ident, "", "")
                return cfg.dev_eui, cfg
        raise KeyError(f"unknown device {identifier!r}: never seen on MQTT and not a "
                       f"devEUI key in the config")

    def send_downlinks(self, identifier: str, frames: list, source: str = "") -> list[Downlink]:
        """Send downlink frames to one device (by devEUI or name) and log each
        to the events table (kind='downlink'). Returns the sent Downlinks."""
        dev_eui, cfg = self.resolve_device(identifier)
        with self._lock:
            return self._send_downlinks_locked(dev_eui, cfg, frames, source)

    def _send_downlinks_locked(self, dev_eui: str, cfg: DeviceConfig, frames: list,
                                source: str = "") -> list[Downlink]:
        """send_downlinks without resolving/locking — call with the lock held."""
        if self.downlink_sender is None:
            raise RuntimeError("downlink sender not configured (start the service with --dl-topic)")
        app_id = self.app_ids.get(dev_eui) or cfg.application_id
        if not app_id:
            raise ValueError(f"no ChirpStack application id known for {cfg.name} ({dev_eui}); "
                             f"set \"application_id\" in the config or wait for an uplink")
        now = utc_now()
        sent: list[Downlink] = []
        for frame in frames:
            dl = Downlink.from_frame(dev_eui, frame)
            self.downlink_sender.send(app_id, dl)
            sent.append(dl)
            text = dl.data.decode("ascii", "replace")
            detail = f"fPort={dl.f_port} data={text!r} {dl.note}".strip()
            if source:
                detail = f"{source} {detail}".strip()
            self._log_event(now, dev_eui, cfg.name, "downlink", True, detail)
            print(f"{now:%Y-%m-%d %H:%M:%S} {cfg.name:<10} downlink >>> "
                  f"fPort={dl.f_port} data={text!r} {dl.note}".rstrip(), flush=True)
        return sent

    def _enforce_downlinks(self, dev_eui: str, cfg: DeviceConfig, obj: dict) -> None:
        """Auto-downlink: compare the ACC settings the device reports in the
        uplink object (acc_threshold / acc_frequency / acc_duration) with the
        desired `threshold` / `frequency` / `duration` from the config and send
        the mismatched ones. Call with the lock held, on sensor-data uplinks."""
        if self.downlink_sender is None or dev_eui in self._dl_bad:
            return
        try:
            frames = desired_frames(cfg)
        except ValueError as e:
            self._dl_bad.add(dev_eui)
            log.error("device %s (%s): invalid downlink settings in config, auto-downlink "
                      "disabled until the next config reload: %s", dev_eui, cfg.name, e)
            return
        pending = []
        for f in frames:
            reported = obj.get(DL_OBJ_FIELD[DL_PORT_KEY[f.f_port]])
            if reported is None:
                continue  # this frame didn't carry the field — don't guess
            try:
                if int(reported) != int(f.data.decode("ascii")):
                    pending.append(f)
            except (TypeError, ValueError):
                continue
        if not pending:
            return
        try:
            self._send_downlinks_locked(dev_eui, cfg, pending, source="auto")
        except (RuntimeError, ValueError) as e:
            # e.g. no application id known yet — log once, not on every uplink
            self._dl_bad.add(dev_eui)
            log.error("auto-downlink for %s (%s) failed, disabled until the next "
                      "config reload: %s", dev_eui, cfg.name, e)

    def _push_changed_downlinks(self) -> None:
        """After a config reload: re-send the desired settings that changed
        (ChirpStack queues a downlink until the device's next uplink, so this
        is safe even for sleeping Class A devices). Call with the lock held."""
        if self.downlink_sender is None:
            return
        for dev_eui, (old_cfg, state) in list(self.devices.items()):
            new_cfg = self.config.for_event(dev_eui, old_cfg.name, old_cfg.family)
            self.devices[dev_eui] = (new_cfg, state)
            try:
                old = {(f.f_port, f.data) for f in desired_frames(old_cfg)}
                new_frames = desired_frames(new_cfg)
            except ValueError:
                continue  # invalid new values: keep the old ones until fixed
            changed = [f for f in new_frames if (f.f_port, f.data) not in old]
            if not changed:
                continue
            try:
                self._send_downlinks_locked(dev_eui, new_cfg, changed, source="config-reload")
            except (RuntimeError, ValueError) as e:
                log.warning("config-reload downlink for %s (%s) not sent: %s",
                            dev_eui, new_cfg.name, e)

    def on_dl_command(self, msg) -> None:
        """MQTT command handler: topic `<dl_topic minus /+>/<devEui-or-name>`,
        payload a JSON object, e.g. {"threshold": 16, "frequency": 50} or
        {"port": 6, "value": "002"}. This is the per-device DL entry point
        the status page will use."""
        prefix = self.dl_topic[:-2] if self.dl_topic.endswith("/+") else self.dl_topic
        identifier = msg.topic[len(prefix):].lstrip("/") if msg.topic.startswith(prefix) else msg.topic
        try:
            payload = json.loads(msg.payload)
            dev_eui, cfg = self.resolve_device(identifier)
            frames = parse_command_message(payload, cfg.family)
            sent = self.send_downlinks(dev_eui, frames, source=f"mqtt {msg.topic}")
            log.info("downlink command on %s: sent %d frame(s) to %s (%s)",
                     msg.topic, len(sent), cfg.name, dev_eui)
        except Exception as e:
            log.error("downlink command on %s failed: %s: %s", msg.topic, type(e).__name__, e)
            print(f"downlink command on {msg.topic} FAILED: {e}", flush=True)
            try:
                self._log_event(utc_now(), identifier.lower(), identifier, "downlink",
                                False, f"error on {msg.topic}: {e}")
            except Exception:
                log.exception("could not log the failed downlink command")

    def _emit(self, cfg, ts: datetime, t) -> None:
        # each step isolated: a DB failure must never suppress the notification
        # (and the notifier only enqueues, so it cannot block either)
        if self.notifier:
            try:
                self.notifier.notify(cfg, t.kind.value, t.active, t.detail)
            except Exception:
                log.exception("enqueue notification failed for %s %s", cfg.dev_eui, t.kind.value)
        self._log_event(ts, cfg.dev_eui, cfg.name, t.kind.value, t.active, t.detail)

    def _log_event(self, ts: datetime, dev_eui: str, name: str,
                   kind: str, active: bool, detail: str) -> None:
        if self.event_log:
            try:
                self.event_log.log(ts, dev_eui, name, kind, active, detail)
            except sqlite3.Error:
                log.exception("event log write failed for %s %s", dev_eui, kind)

    def _persist(self, dev_eui: str, state: AlarmState) -> None:
        if self.event_log:
            try:
                snap = state.snapshot()
                cfg = self.devices[dev_eui][0]
                snap["name"], snap["family"] = cfg.name, cfg.family
                self.event_log.save_state(dev_eui, snap)
            except sqlite3.Error:
                log.exception("state persist failed for %s", dev_eui)

    def persist_all(self) -> None:
        with self._lock:
            for dev_eui, (_cfg, state) in self.devices.items():
                self._persist(dev_eui, state)


def _resolve_password(args) -> str | None:
    """--pass leaks to ps and shell history; prefer --pass-file / env var."""
    if args.password:
        log.warning("--pass on the command line is visible in ps and shell history; "
                    "use --pass-file or LORALARM_MQTT_PASSWORD instead")
        return args.password
    if args.pass_file:
        return Path(args.pass_file).read_text().strip()
    password = os.environ.get("LORALARM_MQTT_PASSWORD")
    if getattr(args, "user", None) and not password:
        log.warning("user %r set but no password: set LORALARM_MQTT_PASSWORD or pass "
                    "--pass-file (the broker will likely refuse the connection)", args.user)
    return password


def main() -> int:
    ap = argparse.ArgumentParser(description="loralarm service: live MQTT alarms, watchdog, notifications")
    ap.add_argument("--mqtt", required=True, metavar="HOST",
                    help="Mosquitto host (subscribes application/+/device/+/event/up)")
    ap.add_argument("--port", type=int, default=1883, help="MQTT port (default 1883)")
    ap.add_argument("--tls", action="store_true", help="use TLS for the MQTT connection")
    ap.add_argument("--client-id", default="loralarm-service",
                    help="fixed MQTT client id; with a persistent session the broker queues "
                         "QoS>=1 uplinks while the service is down (must be unique per broker)")
    ap.add_argument("--user", default=os.environ.get("LORALARM_MQTT_USER"),
                    help="Mosquitto username (or env LORALARM_MQTT_USER)")
    ap.add_argument("--pass", dest="password", default=None,
                    help="Mosquitto password (avoid: visible in ps/history)")
    ap.add_argument("--pass-file", default=None,
                    help="file with the Mosquitto password (first line)")
    ap.add_argument("--config", required=True,
                    help="per-device config JSON (e.g. devices.json; see devices.example.json)")
    ap.add_argument("--db", default="loralarm.db", help="SQLite events log (default ./loralarm.db)")
    ap.add_argument("--ntfy-url", default=os.environ.get("LORALARM_NTFY_URL"),
                    help="ntfy base URL, self-hosted recommended (or env LORALARM_NTFY_URL); "
                         "no default — the public ntfy.sh with a guessable topic is readable by anyone")
    ap.add_argument("--ntfy-token", default=os.environ.get("LORALARM_NTFY_TOKEN"),
                    help="Bearer token for a self-hosted ntfy that requires auth")
    ap.add_argument("--watchdog-interval", type=float, default=300.0,
                    help="seconds between watchdog checks (default 300)")
    ap.add_argument("--dl-topic", default=os.environ.get("LORALARM_DL_TOPIC", DEFAULT_DL_TOPIC),
                    help="MQTT topic for per-device downlink commands, subscribed as "
                         "'<topic>/<devEui-or-name>' with a JSON payload "
                         '({"threshold":16,"frequency":50} or {"port":6,"value":"002"}); '
                         "default loralarm/down/+; pass '' to disable downlinks")
    ap.add_argument("--dry-run", action="store_true",
                    help="log notifications instead of sending ntfy/Pushover")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if not args.ntfy_url:
        ap.error("--ntfy-url (or LORALARM_NTFY_URL) is required — "
                 "do not use the public ntfy.sh with guessable topics")
    password = _resolve_password(args)

    try:
        import paho.mqtt.client as mqtt  # noqa: F401 — availability check
    except ImportError:
        print("paho-mqtt is not installed: pip install -r requirements.txt", file=sys.stderr)
        return 2

    config = Config.load(args.config)
    event_log = EventLog(args.db)
    notifier = Notifier(ntfy_url=args.ntfy_url, dry_run=args.dry_run,
                        ntfy_token=args.ntfy_token)
    dl_topic = args.dl_topic or ""
    engine = Engine(config, event_log, notifier, config_path=args.config, dl_topic=dl_topic)
    engine.seed()
    notifier.start()

    client = mqttclient.make_client(
        args.mqtt, args.port, args.user, password,
        on_uplink=lambda msg: engine.on_uplink(Uplink.from_json(msg.payload)),
        tls=args.tls,
        client_id=args.client_id, persistent=True,
        extra_subscriptions=[(dl_topic, engine.on_dl_command)] if dl_topic else (),
    )
    if dl_topic:
        # the engine publishes downlinks through the same broker connection
        engine.downlink_sender = DownlinkSender(client)
    mqttclient.connect_loop(client, args.mqtt, args.port)
    client.loop_start()

    stop = threading.Event()

    def _stop_handler(_signum, _frame):
        log.info("signal received, shutting down")
        stop.set()

    signal.signal(signal.SIGTERM, _stop_handler)
    signal.signal(signal.SIGINT, _stop_handler)

    log.info("service running: watchdog every %.0f s, events -> %s",
             args.watchdog_interval, args.db)
    disconnected_since: float | None = None
    try:
        while not stop.wait(args.watchdog_interval):
            engine.watchdog_tick()
            if client.is_connected():
                disconnected_since = None
            else:
                # paho auto-reconnects, but if the broker stays down the
                # service is deaf: exit non-zero so systemd restarts it
                if disconnected_since is None:
                    disconnected_since = time.monotonic()
                    log.warning("broker connection lost; waiting up to %.0f s for reconnect",
                                MAX_DISCONNECTED_S)
                elif time.monotonic() - disconnected_since > MAX_DISCONNECTED_S:
                    log.critical("broker %s:%s unreachable for %.0f s — exiting for restart",
                                 args.mqtt, args.port, MAX_DISCONNECTED_S)
                    return 1
    finally:
        engine.persist_all()
        notifier.close()
        client.loop_stop()
        client.disconnect()
    return 0
