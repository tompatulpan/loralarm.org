"""Golden-master scenarios: drive the real Engine with captured and synthetic
uplinks and record everything an operator would see — the console lines and
every ntfy/Pushover message — so a refactor can prove it changed nothing.

    python3 -m tests.golden --update     # rewrite tests/golden/engine.json

Only regenerate when a behaviour change is intended and reviewed in the diff.
"""
from __future__ import annotations

import base64
import contextlib
import io
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest import mock

from loralarm.config import Config
from loralarm.event import Uplink
from loralarm.notify import Notifier
from loralarm.service import Engine

from .common import DEV_EUI, OTHER_EUI, T0, make_uplink, sensor_obj

GOLDEN = Path(__file__).resolve().parent / "golden" / "engine.json"
FIXTURES = Path(__file__).resolve().parent.parent.parent / "chirpstack" / "fixtures"

DOOR_EUI = "a84041a5d7598dd0"
BARE_EUI = "0102030405060708"
LDS02 = "Dragino Switch LDS02"
PUSH = {"pushover_user": "u", "pushover_token": "t"}


def base_config(**defaults: Any) -> dict:
    return {
        "defaults": {"watchdog_hours": 24, **defaults},
        "devices": {
            DEV_EUI: {"name": "sensor3", "family": "RAK4631", "ntfy_topic": "sensor3", **PUSH},
            OTHER_EUI: {"name": "sensor16", "family": "RAK3172", "ntfy_topic": "sensor16"},
            DOOR_EUI: {"name": "Door1", "family": LDS02, "ntfy_topic": "door1", **PUSH},
        },
    }


class CapturingNotifier(Notifier):
    """Real message rendering, no network: records what would have been sent."""

    def __init__(self) -> None:
        super().__init__("http://ntfy.invalid", retries=1)
        self.sent: list[dict] = []

    def _send(self, fn, *args, label: str, target: str) -> bool:
        title, message = (args[1], args[2]) if label == "ntfy" else (args[2], args[3])
        self.sent.append({"channel": label, "target": target, "title": title,
                          "message": message, "active": bool(args[-1])})
        return True


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def sensor(seconds: float, **obj: Any) -> Uplink:
    return make_uplink(obj=sensor_obj(**obj), time=at(seconds), f_cnt=int(seconds) + 1)


def rui(seconds: float, **obj: Any) -> Uplink:
    return make_uplink(dev_eui=OTHER_EUI, device_name="sensor16", profile="RAK3172",
                       obj=sensor_obj(**obj), time=at(seconds), f_cnt=int(seconds) + 1)


def door(seconds: float, open_: int, batt: float = 2.95, f_port: int = 10) -> Uplink:
    obj = {"DOOR_OPEN_STATUS": open_, "Node_type": "LDS02", "BAT_V": batt, "ALARM": 0, "MOD": 1}
    return Uplink(dev_eui=DOOR_EUI, device_name="Door1", application_name="Larm",
                  device_profile_name=LDS02, f_port=f_port, f_cnt=int(seconds) + 1,
                  time=at(seconds), data_b64="", obj=obj, rssi=-68, snr=9.0)


def fixture_uplinks() -> list[Uplink]:
    return [Uplink.from_file(p) for p in sorted(FIXTURES.glob("*.json"))]


def scenarios() -> dict[str, tuple[dict, list[Any]]]:
    """name -> (config data, steps); a step is an Uplink or a datetime (watchdog tick)."""
    s: dict[str, tuple[dict, list[Any]]] = {}

    s["real_fixtures"] = (base_config(), fixture_uplinks())

    s["rak4631_temperature_and_battery"] = (base_config(), [
        sensor(0), sensor(60, temperature=9.0), sensor(120, temperature=9.0),
        sensor(180, temperature=20.0),
        sensor(240, battery_voltage=3.4), sensor(300, battery_voltage=3.1),
        sensor(360, battery_voltage=3.4), sensor(420, battery_voltage=3.9),
        sensor(480, temperature=None), sensor(540, battery_voltage=None),
        sensor(600, temperature=2.0, battery_voltage=3.5),
    ])

    s["rak4631_motion_event_single"] = (base_config(), [
        sensor(0), sensor(10, motion_alert=1), sensor(11, motion_alert=1), sensor(100),
    ])
    s["rak4631_motion_event_min3"] = (base_config(motion_min_detections=3, motion_window_s=30), [
        sensor(0, motion_alert=1), sensor(5, motion_alert=1), sensor(10, motion_alert=1),
        sensor(12, motion_alert=1), sensor(200, motion_alert=1), sensor(205, motion_alert=1),
        sensor(210, motion_alert=1),
    ])
    s["rak4631_motion_level"] = (base_config(motion_mode="level"), [
        sensor(0, motion_alert=1), sensor(10, motion_alert=1), sensor(20), sensor(30),
    ])

    s["rak4631_circuit_event"] = (base_config(), [
        sensor(0), sensor(10, motion_switch=1), sensor(20, motion_switch=1),
        sensor(60, motion_switch=1), sensor(70),
    ])
    s["rak3172_circuit_level"] = (base_config(), [
        rui(0), rui(10, motion_switch=1), rui(20, motion_switch=1), rui(30),
    ])

    def acc(x: float, y: float, z: float) -> dict:
        return {"x": x, "y": y, "z": z}

    s["rak4631_tilt"] = (base_config(), [
        sensor(0, accelerometer=acc(0.0, 0.0, 1.0)), sensor(10, accelerometer=acc(0.0, 0.05, 1.0)),
        sensor(20, accelerometer=acc(0.0, 0.7, 0.7)),
        *[sensor(30 + i, accelerometer=acc(0.0, 0.7, 0.7)) for i in range(12)],
        sensor(100, accelerometer=acc(0.0, 0.7, 0.7)),
    ])

    s["watchdog"] = (base_config(), [
        sensor(0), at(3600), at(25 * 3600), sensor(25 * 3600 + 30), at(25 * 3600 + 60),
        at(60 * 3600),
    ])

    s["lds02_door"] = (base_config(), [
        door(0, 0), door(10, 1), door(20, 1), door(30, 0),
        door(40, 0, batt=2.4), door(50, 0, batt=2.1), door(60, 0, batt=2.95),
        door(70, 1, f_port=2),
    ])

    s["non_sensor_frames"] = (base_config(), [
        sensor(0, motion_alert=1),
        make_uplink(f_port=5, obj={}, time=at(10), data_b64=base64.b64encode(b"016").decode()),
        make_uplink(f_port=2, obj={}, time=at(20)),
        sensor(30),
    ])

    s["unconfigured_device"] = (base_config(), [
        make_uplink(dev_eui=BARE_EUI, device_name="newbie", profile="RAK3172",
                    obj=sensor_obj(temperature=-3.0, battery_voltage=2.85), time=at(0)),
        make_uplink(dev_eui=BARE_EUI, device_name="newbie", profile="",
                    obj=sensor_obj(battery_voltage=2.7), time=at(10)),
    ])
    return s


def run_scenario(config: dict, steps: list[Any]) -> dict:
    notifier = CapturingNotifier()
    engine = Engine(Config(json.loads(json.dumps(config))), notifier=notifier)
    out = io.StringIO()
    clock = {"now": T0}
    with contextlib.redirect_stdout(out), mock.patch("loralarm.service.utc_now",
                                                     lambda: clock["now"]):
        for step in steps:
            if isinstance(step, datetime):
                clock["now"] = step
                engine.watchdog_tick()
            else:
                clock["now"] = step.time
                engine.on_uplink(step)
    snap = {eui: {"active": sorted(str(k) for k in state.active),
                  "last_detail": {str(k): v for k, v in sorted(state.last_detail.items())}}
            for eui, (_cfg, state) in sorted(engine.devices.items())}
    return {"console": out.getvalue().splitlines(), "notifications": notifier.sent, "state": snap}


def run_all() -> dict[str, dict]:
    return {name: run_scenario(cfg, steps) for name, (cfg, steps) in scenarios().items()}


def main(argv: list[str]) -> int:
    result = run_all()
    if "--update" in argv:
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(json.dumps(result, indent=1, ensure_ascii=False, sort_keys=True) + "\n",
                          encoding="utf-8")
        print(f"wrote {GOLDEN} ({len(result)} scenarios)")
        return 0
    print(json.dumps(result, indent=1, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
