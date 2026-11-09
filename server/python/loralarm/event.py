"""Parsing of ChirpStack MQTT uplink events (topic application/+/device/+/event/up).

ChirpStack publishes the raw frame in `data` (base64) plus the codec-decoded
`object`. Sensor data arrives on fPort 2; settings/response frames on fPort 4-7
are 3-digit ASCII decimal and are parsed here, not by the codec.
"""
from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass
class Uplink:
    dev_eui: str
    device_name: str
    application_name: str
    device_profile_name: str
    f_port: int
    f_cnt: int
    time: datetime
    data_b64: str
    obj: dict[str, Any]          # decoded by the ChirpStack device-profile codec
    rssi: int | None
    snr: float | None

    @property
    def is_sensor_data(self) -> bool:
        return self.f_port == 2

    def settings_text(self) -> str | None:
        """fPort 4-7 payload as ASCII (e.g. b'016' -> '016'); None for other ports."""
        if self.f_port in (4, 5, 6, 7):
            return base64.b64decode(self.data_b64).decode("ascii", "replace")
        return None

    @classmethod
    def from_json(cls, raw: str | bytes | dict[str, Any]) -> "Uplink":
        ev = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
        info = ev["deviceInfo"]
        rx = ev.get("rxInfo") or []
        return cls(
            dev_eui=info["devEui"].lower(),
            device_name=info["deviceName"],
            application_name=info.get("applicationName", ""),
            device_profile_name=info.get("deviceProfileName", ""),
            f_port=ev["fPort"],
            f_cnt=ev.get("fCnt", 0),
            time=datetime.fromisoformat(ev["time"]),
            data_b64=ev.get("data", ""),
            obj=ev.get("object") or {},
            rssi=rx[0].get("rssi") if rx else None,
            snr=rx[0].get("snr") if rx else None,
        )

    @classmethod
    def from_file(cls, path: str | Path) -> "Uplink":
        return cls.from_json(Path(path).read_text())
