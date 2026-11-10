"""Per-device downlinks (migration spec §7.4, README next-step 2).

Path (confirmed against the ChirpStack v4 MQTT integration docs): publish to
`application/<APPLICATION_ID>/device/<DEV_EUI>/command/down` with

    {"devEui": "...", "confirmed": false, "fPort": 5, "data": "<base64>"}

`data` is the raw frame (base64) and bypasses the device-profile codec;
providing `object` instead would go through the codec's `encodeDownlink`.

Device-side protocol (both families, see server/chirpstack/README.md and the
firmware RX callbacks): 3-digit zero-padded ASCII decimal, value 0-255, on

    port 4  command (RAK4631: e.g. test alarm; RUI3 ignores it)
    port 5  accelerometer interrupt threshold, 1-127
    port 6  accelerometer ODR frequency: RAK4631 LIS3DH register code
            31/47/63/79/95, RUI3 ODR index 1-7
    port 7  interrupt duration, 0-127

Frequency translation (spec §7.4): dashboard/register code 31/47/63/79/95
(= 1/10/25/50/100 Hz) <-> RUI3 index 1-5; RUI3 also has index 6/7 (200/400 Hz)
which have no RAK4631 register code.

Two entry points:

* CLI, one-shot per device:
      python3 -m loralarm.downlink --mqtt <lora-box> --config devices.json \
          --dev sensor3 --set threshold=16 --set frequency=50
* the live service subscribes to `loralarm/down/<devEui-or-name>` (Engine
  .on_dl_command) with a JSON payload like {"threshold": 16, "frequency": 50}
  or {"port": 6, "value": "002"} -- this is what the status page will use.

Every sent downlink is logged to the events table (kind="downlink").
"""
from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# settings/command ports (both families speak 3-digit ASCII decimal here)
PORT_COMMAND = 4      # RAK4631 command byte (high/low, test alarm)
PORT_THRESHOLD = 5    # interrupt threshold 1-127
PORT_FREQUENCY = 6    # ODR: RAK4631 register code / RUI3 index
PORT_DURATION = 7     # interrupt duration 0-127
SETTINGS_PORTS = (PORT_COMMAND, PORT_THRESHOLD, PORT_FREQUENCY, PORT_DURATION)

MAX_VALUE = 255       # firmware: DOWNLINK_MAX_VALUE, 3 ASCII digits
THRESHOLD_RANGE = (1, 127)
DURATION_RANGE = (0, 127)

DEFAULT_DL_TOPIC = "loralarm/down/+"   # <prefix>/<devEui-or-name>

# RUI3 (LIS3DH) ODR index -> Hz; index 6/7 (200/400 Hz) exist only on RUI3
HZ_TO_RUI3_INDEX = {1: 1, 10: 2, 25: 3, 50: 4, 100: 5, 200: 6, 400: 7}
# RAK4631 LIS3DH register code -> Hz (payload_codec_rak4631.js REG)
REG_TO_HZ = {31: 1, 47: 10, 63: 25, 79: 50, 95: 100}
HZ_TO_REG = {hz: code for code, hz in REG_TO_HZ.items()}
# dashboard/register code -> RUI3 ODR index (payload_codec_rui3.js REG_TO_RUI3)
REG_TO_RUI3_INDEX = {0: 0, 31: 1, 47: 2, 63: 3, 79: 4, 95: 5}

RUI3_FAMILIES = {"rak3172", "rui3", "cayenne"}
RAK4631_FAMILIES = {"rak4631", "android", "custom"}

# desired-setting config key -> codec `object` field with the device-reported
# wire value (both codecs emit acc_threshold / acc_duration / acc_frequency)
DL_OBJ_FIELD = {"threshold": "acc_threshold", "frequency": "acc_frequency",
                "duration": "acc_duration"}
DL_PORT_KEY = {PORT_THRESHOLD: "threshold", PORT_FREQUENCY: "frequency",
               PORT_DURATION: "duration"}


def desired_frames(cfg) -> list[DownlinkFrame]:
    """Frames for the per-device desired settings from the config
    (cfg.threshold / cfg.frequency / cfg.duration); empty when none are set.
    Raises ValueError on out-of-range values — callers log and skip."""
    frames: list[DownlinkFrame] = []
    family = getattr(cfg, "family", "")
    for key in ("threshold", "frequency", "duration"):
        value = getattr(cfg, key, None)
        if value is not None:
            frames.append(settings_frame(key, value, family))
    return frames

_EUI_RE = re.compile(r"^[0-9a-f]{16}$")


def ascii3(value: int | str) -> bytes:
    """3-digit zero-padded ASCII decimal frame payload, e.g. 16 -> b'016'.

    Both firmwares parse exactly 3 ASCII digits into a 0-255 value.
    """
    try:
        v = int(str(value).strip(), 10)
    except (TypeError, ValueError):
        raise ValueError(f"not a decimal value: {value!r}") from None
    if isinstance(value, float) and not value.is_integer():
        raise ValueError(f"not an integer value: {value!r}")
    if not 0 <= v <= MAX_VALUE:
        raise ValueError(f"value {v} out of range [0-{MAX_VALUE}]")
    return f"{v:03d}".encode("ascii")


def parse_frequency(value: int | str, family: str = "") -> int:
    """Translate a frequency request to the wire value for `family`.

    Accepted inputs: Hz (1/10/25/50/100/200/400), RAK4631 register code
    (31/47/63/79/95), RUI3 index (1-7), or explicit `hz:`/`code:`/`index:`
    prefixes. With a known family the result is that family's wire value
    (RUI3: index 1-7, RAK4631: register code). With an unknown family the
    value is passed through unchanged (codes/indices only; Hz needs a family).
    """
    fam = (family or "").strip().lower()
    s = str(value).strip().lower()
    hz: int | None = None
    code: int | None = None
    index: int | None = None
    if s.startswith("hz:"):
        hz = _int(s[3:], "hz")
    elif s.startswith("code:"):
        code = _int(s[5:], "code")
    elif s.startswith("index:"):
        index = _int(s[6:], "index")
    else:
        v = _int(s, "frequency")
        if v in REG_TO_HZ:
            code = v
        elif v in HZ_TO_RUI3_INDEX:
            hz = v
        elif 1 <= v <= 7:
            index = v
        else:
            raise ValueError(f"unknown frequency {value!r} (Hz 1/10/25/50/100/200/400, "
                             f"register code 31/47/63/79/95, or RUI3 index 1-7)")
    if hz is not None and hz not in HZ_TO_RUI3_INDEX:
        raise ValueError(f"unknown frequency {hz} Hz")
    if code is not None and code not in REG_TO_HZ:
        raise ValueError(f"unknown register code {code} (31/47/63/79/95)")
    if index is not None and not 1 <= index <= 7:
        raise ValueError(f"RUI3 ODR index out of range [1-7]: {index}")

    if fam in RUI3_FAMILIES:
        if index is not None:
            return index
        if code is not None:
            return REG_TO_RUI3_INDEX[code]
        return HZ_TO_RUI3_INDEX[hz]
    if fam in RAK4631_FAMILIES:
        if code is not None:
            return code
        if index is not None:
            hz = next(h for h, i in HZ_TO_RUI3_INDEX.items() if i == index)
        if hz not in HZ_TO_REG:
            raise ValueError(f"{hz} Hz has no RAK4631 register code (max 100 Hz)")
        return HZ_TO_REG[hz]
    # family unknown: pass the wire value through unchanged
    if index is not None:
        return index
    if code is not None:
        return code
    raise ValueError("frequency in Hz needs a known device family "
                     "(set `family` in the config or pass --family); "
                     "use code:31/47/63/79/95 or index:1-7 for an explicit wire value")


def wire_to_hz(value: int | str, family: str = "") -> float | None:
    """Wire value (RAK4631 register code or RUI3 ODR index) -> Hz, else None.
    With an unknown family both encodings are accepted."""
    fam = (family or "").strip().lower()
    try:
        f = float(value)          # JSON numbers arrive as floats (95.0)
        if not f.is_integer():
            return None
        v = int(f)
    except (TypeError, ValueError):
        return None
    if fam in RAK4631_FAMILIES:
        hz = REG_TO_HZ.get(v)
    elif fam in RUI3_FAMILIES:
        hz = next((h for h, i in HZ_TO_RUI3_INDEX.items() if i == v), None)
    else:
        hz = REG_TO_HZ.get(v)
        if hz is None:
            hz = next((h for h, i in HZ_TO_RUI3_INDEX.items() if i == v), None)
    return float(hz) if hz is not None else None


def _int(s: str, what: str) -> int:
    try:
        return int(s, 10)
    except (TypeError, ValueError):
        raise ValueError(f"invalid {what} value: {s!r}") from None


def _check_range(key: str, v: int, lo: int, hi: int) -> int:
    if not lo <= v <= hi:
        raise ValueError(f"{key} out of range [{lo}-{hi}]: {v}")
    return v


@dataclass
class DownlinkFrame:
    """One frame to enqueue: fPort + raw payload bytes + human-readable note."""
    f_port: int
    data: bytes
    confirmed: bool = False
    note: str = ""


def settings_frame(key: str, value: int | str, family: str = "",
                   confirmed: bool = False) -> DownlinkFrame:
    """Build one settings/command frame.

    key: threshold | frequency | duration | command | port (with value).
    """
    k = str(key).strip().lower()
    if k == "threshold":
        v = _int(str(value), "threshold")
        return DownlinkFrame(PORT_THRESHOLD, ascii3(_check_range("threshold", v, *THRESHOLD_RANGE)),
                             confirmed, f"threshold={v}")
    if k == "duration":
        v = _int(str(value), "duration")
        return DownlinkFrame(PORT_DURATION, ascii3(_check_range("duration", v, *DURATION_RANGE)),
                             confirmed, f"duration={v}")
    if k == "command":
        v = _int(str(value), "command")
        return DownlinkFrame(PORT_COMMAND, ascii3(_check_range("command", v, 0, MAX_VALUE)),
                             confirmed, f"command={v}")
    if k == "frequency":
        v = parse_frequency(value, family)
        hz = wire_to_hz(v, family)
        note = f"frequency wire={v}" + (f" ({hz:g} Hz)" if hz is not None else "")
        return DownlinkFrame(PORT_FREQUENCY, ascii3(v), confirmed, note)
    if k == "port":
        raise ValueError("key 'port' needs the 'value' key too; use parse_command_message")
    raise ValueError(f"unknown downlink key {key!r} "
                     f"(threshold | frequency | duration | command | port+value)")


def parse_command_message(payload: Any, family: str = "",
                          confirmed: bool = False) -> list[DownlinkFrame]:
    """Parse a downlink command payload (JSON object) into frames.

    Accepted shape: {"threshold": 16, "frequency": 50, "duration": 2,
    "command": 1, "confirmed": true} or a raw frame {"port": 6, "value": "002"}.
    Unknown keys are rejected so a typo never silently sends nothing.
    """
    if not isinstance(payload, dict):
        raise ValueError(f"downlink payload must be a JSON object, got {type(payload).__name__}")
    conf = bool(payload.get("confirmed", confirmed))
    frames: list[DownlinkFrame] = []
    if "port" in payload:
        port = _int(str(payload["port"]), "port")
        if not 1 <= port <= 223:
            raise ValueError(f"fPort out of range [1-223]: {port}")
        if "value" not in payload:
            raise ValueError("raw downlink needs 'value' next to 'port'")
        frames.append(DownlinkFrame(port, ascii3(payload["value"]), conf,
                                    f"raw port {port}"))
        rest = {k: v for k, v in payload.items() if k not in ("port", "value", "confirmed")}
        if rest:
            raise ValueError(f"cannot mix raw port/value with settings keys: {sorted(rest)}")
        return frames
    for key in ("threshold", "frequency", "duration", "command"):
        if key in payload:
            frames.append(settings_frame(key, payload[key], family, conf))
    unknown = sorted(set(payload) - {"threshold", "frequency", "duration", "command", "confirmed"})
    if unknown:
        raise ValueError(f"unknown downlink keys: {unknown}")
    if not frames:
        raise ValueError("empty downlink command: set threshold/frequency/duration/command "
                         "or port+value")
    return frames


@dataclass
class Downlink:
    """A frame addressed to one device. devEUI is normalized to lowercase —
    ChirpStack uses lowercase hex in the MQTT topic and requires the payload
    devEui to match it."""
    dev_eui: str
    f_port: int
    data: bytes
    confirmed: bool = False
    note: str = ""

    def __post_init__(self) -> None:
        self.dev_eui = self.dev_eui.lower()

    @classmethod
    def from_frame(cls, dev_eui: str, frame: DownlinkFrame) -> "Downlink":
        return cls(dev_eui=dev_eui, f_port=frame.f_port, data=frame.data,
                   confirmed=frame.confirmed, note=frame.note)

    def topic(self, application_id: str) -> str:
        """ChirpStack v4 MQTT downlink command topic."""
        return f"application/{application_id}/device/{self.dev_eui}/command/down"

    def payload(self) -> str:
        """Command payload; `data` is raw base64 and bypasses the codec."""
        return json.dumps({
            "devEui": self.dev_eui,
            "confirmed": self.confirmed,
            "fPort": self.f_port,
            "data": base64.b64encode(self.data).decode("ascii"),
        })

    def message(self, application_id: str) -> tuple[str, str]:
        return self.topic(application_id), self.payload()


def build_message(application_id: str, dl: Downlink) -> tuple[str, str]:
    return dl.message(application_id)


class DownlinkSender:
    """Publishes Downlink frames through a paho client (QoS 1 by default).

    `wait=True` blocks until the broker acks (CLI one-shots); the live
    service uses wait=False and checks the publish return code.
    """

    def __init__(self, client, qos: int = 1, wait: bool = False):
        self._client = client
        self._qos = qos
        self._wait = wait

    def send(self, application_id: str, dl: Downlink) -> None:
        topic, payload = dl.message(application_id)
        info = self._client.publish(topic, payload, qos=self._qos)
        if self._wait:
            info.wait_for_publish(timeout=15)
        if info.rc != 0:  # MQTT_ERR_SUCCESS == 0; queued-offline also returns 0
            raise ConnectionError(f"publish to {topic} failed (rc={info.rc})")
        log.info("downlink -> %s fPort=%s data=%r", topic, dl.f_port,
                 dl.data.decode("ascii", "replace"))


def resolve_dev_eui(identifier: str, config_devices: dict[str, Any]) -> str:
    """devEUI from a devEUI string or a configured device name (CLI use).

    Config devices are keyed by devEUI with a `name` inside, so a name
    resolves to its key. A config keyed by name cannot yield a devEUI.
    """
    ident = identifier.strip().lower()
    if _EUI_RE.match(ident):
        return ident
    for key, entry in config_devices.items():
        if _EUI_RE.match(key) and isinstance(entry, dict) \
                and str(entry.get("name", "")).lower() == ident:
            return key
    if ident in config_devices:
        raise ValueError(f"{identifier!r} is a config key but not a devEUI; the MQTT downlink "
                         f"topic needs the 16-hex devEUI, and a name-keyed config cannot provide it")
    raise ValueError(f"unknown device {identifier!r}: not a devEUI and no config entry named that")


def main(argv: list[str] | None = None) -> int:
    """One-shot per-device downlink CLI:

        python3 -m loralarm.downlink --mqtt <host> --config devices.json \
            --dev <devEui|name> [--app-id UUID] [--family RAK3172] \
            [--set threshold=16] [--set frequency=50] [--set duration=2] \
            [--set command=1] [--raw --fport 6 --value 002] [--confirmed] [--dry-run]

    With no --set/--raw, sends the device's configured desired settings
    (threshold/frequency/duration from devices.json).
    """
    ap = argparse.ArgumentParser(
        prog="python3 -m loralarm.downlink",
        description="send a downlink to one device via the ChirpStack MQTT command topic")
    ap.add_argument("--mqtt", metavar="HOST", help="Mosquitto host (skip with --dry-run)")
    ap.add_argument("--port", type=int, default=1883, help="MQTT port (default 1883)")
    ap.add_argument("--tls", action="store_true", help="use TLS for the MQTT connection")
    ap.add_argument("--user", default=os.environ.get("LORALARM_MQTT_USER"), help="Mosquitto username")
    ap.add_argument("--pass", dest="password", default=None,
                    help="Mosquitto password (avoid: visible in ps/history)")
    ap.add_argument("--pass-file", default=None, help="file with the Mosquitto password (first line)")
    ap.add_argument("--config", default=str(Path(__file__).resolve().parent.parent / "devices.example.json"),
                    help="per-device config JSON (default: devices.example.json next to the package)")
    ap.add_argument("--dev", required=True, help="devEUI or configured device name")
    ap.add_argument("--app-id", default=None,
                    help="ChirpStack application ID; overrides the device's `application_id` "
                         "in the config, which in turn wins over env LORALARM_CS_APP_ID "
                         "(the live service learns it from uplinks)")
    ap.add_argument("--family", default=None,
                    help="device profile name (RAK3172/RAK4631) for frequency translation; "
                         "defaults to the `family` key of the device config entry")
    ap.add_argument("--set", dest="settings", action="append", default=[], metavar="KEY=VALUE",
                    help="threshold|frequency|duration|command = value; repeatable")
    ap.add_argument("--raw", action="store_true", help="send --fport/--value as a raw frame")
    ap.add_argument("--fport", type=int, default=None, help="raw frame fPort (with --raw --value)")
    ap.add_argument("--value", default=None, help="raw frame value (with --raw --fport)")
    ap.add_argument("--confirmed", action="store_true", help="request a confirmed downlink")
    ap.add_argument("--dry-run", action="store_true", help="print topic+payload without connecting")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    from .config import Config
    config = Config.load(args.config)

    dev_eui = resolve_dev_eui(args.dev, config.devices)
    # family: CLI flag > device config entry > profile passed by the caller
    entry = config.devices.get(dev_eui) or {}
    family = args.family or (str(entry.get("family", "")) if isinstance(entry, dict) else "")
    cfg = config.for_event(dev_eui, str(entry.get("name", "")) if isinstance(entry, dict) else "", family)
    app_id = args.app_id or cfg.application_id or os.environ.get("LORALARM_CS_APP_ID", "")
    if not app_id and not args.dry_run:
        ap.error("no ChirpStack application id: pass --app-id, set LORALARM_CS_APP_ID, "
                 "or add \"application_id\" to the config")

    try:
        frames: list[DownlinkFrame] = []
        for kv in args.settings:
            key, sep, val = kv.partition("=")
            if not sep:
                ap.error(f"--set expects KEY=VALUE, got {kv!r}")
            frames.extend(parse_command_message({key: val}, family, args.confirmed))
        if args.raw:
            if args.fport is None or args.value is None:
                ap.error("--raw needs --fport N and --value V")
            frames.extend(parse_command_message({"port": args.fport, "value": args.value},
                                                family, args.confirmed))
        if not frames:
            # no explicit command: push the desired settings from the config
            frames = desired_frames(cfg)
            if not frames:
                ap.error(f"nothing to send: no --set/--raw given and no "
                         f"threshold/frequency/duration configured for {args.dev}")
    except ValueError as e:
        ap.error(str(e))

    downlinks = [Downlink.from_frame(dev_eui, f) for f in frames]
    for dl in downlinks:
        topic, payload = dl.message(app_id or "<APPLICATION_ID>")
        print(f"{topic}\n  {payload}   # fPort={dl.f_port} {dl.note} confirmed={dl.confirmed}")
    if args.dry_run:
        return 0

    if not args.mqtt:
        ap.error("--mqtt HOST is required unless --dry-run")
    try:
        import paho.mqtt.client as mqtt  # noqa: F401
    except ImportError:
        print("paho-mqtt is not installed: pip install -r requirements.txt", file=sys.stderr)
        return 2

    from . import mqttclient
    password = args.password or (Path(args.pass_file).read_text().strip() if args.pass_file
                                 else os.environ.get("LORALARM_MQTT_PASSWORD"))
    client = mqttclient.make_publisher(args.mqtt, args.port, args.user, password, tls=args.tls)
    mqttclient.connect_loop(client, args.mqtt, args.port)
    client.loop_start()
    try:
        sender = DownlinkSender(client, wait=True)
        for dl in downlinks:
            sender.send(app_id, dl)
            print(f"sent: fPort={dl.f_port} data={dl.data.decode('ascii', 'replace')!r} "
                  f"to {cfg.name} ({dev_eui})", flush=True)
    finally:
        client.loop_stop()
        client.disconnect()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
