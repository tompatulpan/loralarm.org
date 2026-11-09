"""Per-device configuration, keyed by devEUI (fallback: device name).

Layering (most specific wins): `defaults` <- `families[<device profile>]` <- `devices[<devEUI>`].
Family defaults encode the two sensor families from the migration spec §4;
per-device values (Tk, battery thresholds, ntfy topic, ...) never live in codecs.
JSON keeps the test app dependency-free; PyYAML can replace the loader later.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class DeviceConfig:
    dev_eui: str
    name: str = ""
    family: str = ""                    # ChirpStack device-profile name, e.g. RAK3172 / RAK4631
    kind: str = "sensor"                # sensor | door | water
    tk_correction_c: float = 0.0        # subtracted from raw temperature
    temp_alarm_limit_c: float = 0.0     # alarm at or below this temperature
    batt_full_v: float = 3.1
    batt_warn_v: float = 2.9
    batt_empty_v: float = 2.8
    tilt_sense_g: float = 0.3
    watchdog_hours: float = 24.0        # stale if no uplink within this
    ntfy_topic: str = "loralarm"
    pushover_user: str = ""
    pushover_token: str = ""


# Migration spec §4: RUI3 = 3 V cell (0 °C Tk, alarm limit 0 °C), RAK4631 Android = LiPo (8 °C Tk, limit 2 °C).
FAMILY_DEFAULTS: dict[str, dict[str, Any]] = {
    "RAK3172": dict(tk_correction_c=0.0, temp_alarm_limit_c=0.0,
                    batt_full_v=3.1, batt_warn_v=2.9, batt_empty_v=2.8),
    "RAK4631": dict(tk_correction_c=8.0, temp_alarm_limit_c=2.0,
                    batt_full_v=4.2, batt_warn_v=3.5, batt_empty_v=3.2),
}


class Config:
    def __init__(self, data: dict[str, Any]):
        self.defaults = data.get("defaults", {})
        self.families = {**FAMILY_DEFAULTS, **data.get("families", {})}
        self.devices = data.get("devices", {})

    @classmethod
    def load(cls, path: str | Path) -> "Config":
        return cls(json.loads(Path(path).read_text()))

    def for_event(self, dev_eui: str, device_name: str = "", family: str = "") -> DeviceConfig:
        entry = self.devices.get(dev_eui) or self.devices.get(device_name) or {}
        merged = {**self.defaults, **self.families.get(family, {}), **entry}
        fields = set(DeviceConfig.__dataclass_fields__) - {"dev_eui", "name", "family"}
        return DeviceConfig(
            dev_eui=dev_eui,
            name=device_name or merged.get("name", ""),
            family=family,
            **{k: v for k, v in merged.items() if k in fields},
        )
