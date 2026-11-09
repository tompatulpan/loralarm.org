"""Per-device configuration, keyed by devEUI (fallback: device name).

Layering (most specific wins): `defaults` <- `families[<device profile>]` <- `devices[<devEUI>]`.
Family defaults encode the two sensor families from the migration spec §4;
per-device values (Tk, battery thresholds, ntfy topic, ...) never live in codecs.
JSON keeps the test app dependency-free; PyYAML can replace the loader later.

Secrets never live in the tracked files: any string of the form "${VAR}" is
expanded from the environment at load time (unset variables expand to "" and
are reported), so devices.example.json can stay in git while the real
devices.json / .env stay untracked.
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_ENV_RE = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")


def _expand(value: Any, where: str) -> Any:
    """Recursively expand "${VAR}" strings from the environment."""
    if isinstance(value, str):
        m = _ENV_RE.match(value)
        if m:
            env = os.environ.get(m.group(1))
            if env is None:
                log.warning("config %s: environment variable %s is not set; using empty value",
                            where, m.group(1))
                return ""
            return env
        return value
    if isinstance(value, dict):
        return {k: _expand(v, where) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v, where) for v in value]
    return value


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
    # notification texts: {name} {detail} {label} are substituted; keys are
    # alarm kinds (temp, battery_low, battery_empty, motion, circuit, tilt, stale)
    notify_title: str = "loralarm {name}"
    notify_fire: dict[str, str] = field(default_factory=dict)
    notify_clear: dict[str, str] = field(default_factory=dict)


# Migration spec §4: RAK3172 = 3 V cell (0 °C Tk, alarm limit 0 °C), RAK4631 Android = LiPo (8 °C Tk, limit 2 °C).
FAMILY_DEFAULTS: dict[str, dict[str, Any]] = {
    "RAK3172": dict(tk_correction_c=0.0, temp_alarm_limit_c=0.0,
                    batt_full_v=3.1, batt_warn_v=2.9, batt_empty_v=2.8),
    "RAK4631": dict(tk_correction_c=8.0, temp_alarm_limit_c=2.0,
                    batt_full_v=4.2, batt_warn_v=3.5, batt_empty_v=3.2),
}

# expected type per DeviceConfig field, for config validation
_FIELD_TYPES: dict[str, tuple[type, ...]] = {
    "name": (str,), "family": (str,), "kind": (str,),
    "tk_correction_c": (int, float), "temp_alarm_limit_c": (int, float),
    "batt_full_v": (int, float), "batt_warn_v": (int, float),
    "batt_empty_v": (int, float), "tilt_sense_g": (int, float),
    "watchdog_hours": (int, float), "ntfy_topic": (str,),
    "pushover_user": (str,), "pushover_token": (str,),
    "notify_title": (str,), "notify_fire": (dict,), "notify_clear": (dict,),
}


def _valid_type(key: str, value: Any) -> bool:
    expected = _FIELD_TYPES[key]
    if isinstance(value, bool) and bool not in expected:
        return False  # True/False must not pass as a number
    return isinstance(value, expected)


def _type_names(types: tuple[type, ...]) -> str:
    return "/".join(t.__name__ for t in types)


class Config:
    def __init__(self, data: dict[str, Any]):
        data = _expand(data, "config")
        self.defaults = data.get("defaults", {})
        self.families = {**FAMILY_DEFAULTS, **data.get("families", {})}
        # devEUIs are lowercased on the wire (Uplink lowercases them), so the
        # lookup keys must be lowercase too or an uppercase key never matches
        self.devices = {str(k).lower(): v for k, v in data.get("devices", {}).items()}
        self._warn_unknown("defaults", self.defaults)
        for fam, fvals in data.get("families", {}).items():
            self._warn_unknown(f"families.{fam}", fvals)
        for eui, entry in data.get("devices", {}).items():
            self._warn_unknown(f"devices.{eui}", entry)

    @staticmethod
    def _warn_unknown(where: str, section: dict[str, Any]) -> None:
        for key in section:
            if key not in _FIELD_TYPES:
                log.warning("config %s: unknown key %r ignored (typo?)", where, key)

    @classmethod
    def load(cls, path: str | Path) -> "Config":
        return cls(json.loads(Path(path).read_text()))

    def for_event(self, dev_eui: str, device_name: str = "", family: str = "") -> DeviceConfig:
        dev_eui = dev_eui.lower()
        entry = self.devices.get(dev_eui) or self.devices.get(device_name.lower()) or {}
        merged: dict[str, Any] = {}
        for layer in (self.defaults, self.families.get(family, {}), entry):
            for k, v in layer.items():
                if k in _FIELD_TYPES and not _valid_type(k, v):
                    # an invalid value must not reach the alarm code (TypeError on
                    # every uplink); keep the value from the lower layer instead
                    log.warning("config for %s: key %r should be %s, got %r; ignoring it",
                                dev_eui, k, _type_names(_FIELD_TYPES[k]), v)
                    continue
                merged[k] = v
        fields = set(DeviceConfig.__dataclass_fields__) - {"dev_eui", "name", "family"}
        kwargs = {k: v for k, v in merged.items() if k in fields}
        # the configured name is the most specific source and wins over the
        # ChirpStack deviceName; fall back to deviceName, then the devEUI
        name = merged.get("name") or device_name or dev_eui
        return DeviceConfig(dev_eui=dev_eui, name=name, family=family, **kwargs)
