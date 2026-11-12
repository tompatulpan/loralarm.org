"""Per-device configuration, keyed by devEUI (fallback: device name).

Layering (most specific wins): `defaults` <- `families[<device profile>]` <- `devices[<devEUI>]`.
Family defaults come from the device-profile registry (profiles.py, migration
spec §4); per-device values (Tk, battery thresholds, ntfy topic, ...) never live in codecs.
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

from .registry import Registry

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
    kind: str = "sensor"                # sensor | door | water | any kind an alarm rule names;
                                        # selects which alarm rules and auto-downlink settings apply
    tk_correction_c: float = 0.0        # subtracted from raw temperature
    temp_alarm_min_c: float = 0.0       # temp_low alarm at or below this temperature
    temp_alarm_max_c: float | None = None   # temp_high alarm at or above this; None = off
    # a temp alarm clears only once the temperature is this far back inside the
    # limits (above low / below high); 0 = clear as soon as the limit is passed
    temp_alarm_hysteresis_c: float = 0.0
    batt_full_v: float = 3.1
    batt_warn_v: float = 2.9
    batt_empty_v: float = 2.8
    tilt_sense_g: float = 0.3
    # level: the device reports the contact state in every frame (RUI3) -> fire/clear.
    # event: the device sends one frame when the contact opens and nothing on close
    # (RAK4631) -> every pulse is a fire-only alarm, other frames never clear it.
    circuit_mode: str = "level"
    circuit_hold_s: float = 30.0        # event mode: pulses closer than this count as one
    # motion: the firmware sends motion_alert=1 in one frame per accelerometer
    # interrupt (never in heartbeats). event (default) -> every detection alarms,
    # other frames never clear it. level -> old fire-once/clear-once semantics.
    motion_mode: str = "event"
    motion_window_s: float = 30.0       # event mode: sliding window for counting detections
    motion_min_detections: int = 1      # event mode: alarm when this many detections fall
                                        # within motion_window_s (1 = every detection;
                                        # 3 = "3 detections within 30 s")
    watchdog_hours: float = 24        # stale if no uplink within this
    # Only notify inside this daily window, "HH:MM-HH:MM" in `timezone` (overnight
    # allowed, e.g. "21:00-09:00"); empty = always. Outside it the alarm is still
    # evaluated and logged but not sent. Applies to the "windowed" alarm kinds
    # (activity alarms by default; health kinds temp_low/temp_high/battery/stale always notify)
    # or to exactly `alarm_window_kinds` when that is set. See schedule.py.
    alarm_window: str = ""
    alarm_window_kinds: list[str] | None = None
    timezone: str = "Europe/Stockholm"
    application_id: str = ""            # ChirpStack application UUID; needed for the downlink
                                        # topic (the live service also learns it from uplinks)
    # desired ACC settings, auto-pushed as downlinks (ports 5-7) whenever the
    # device-reported acc_threshold / acc_frequency / acc_duration in the uplink
    # object differ (see service.Engine._enforce_downlinks). `frequency` takes
    # Hz (1/10/25/50/100/200/400), a RAK4631 register code or a RUI3 index —
    # see downlink.parse_frequency. `command` (port 4) is one-shot only: use the
    # CLI or the loralarm/down MQTT topic, not the config.
    threshold: int | None = None
    frequency: int | str | None = None
    duration: int | None = None
    # free-form parameters for alarm rules / profile settings that have no field of
    # their own (e.g. {"humidity_max": 80}); merged key-by-key across the layers
    params: dict[str, Any] = field(default_factory=dict)
    ntfy_topic: str = "loralarm"
    pushover_user: str = ""
    pushover_token: str = ""
    # notification texts: {name} {detail} {label} are substituted; keys are
    # alarm kinds (temp_low, temp_high, battery_low, battery_empty, motion, circuit, tilt, stale)
    notify_title: str = "loralarm {name}"
    notify_fire: dict[str, str] = field(default_factory=dict)
    notify_clear: dict[str, str] = field(default_factory=dict)


# expected type per DeviceConfig field, for config validation
_FIELD_TYPES: dict[str, tuple[type, ...]] = {
    "name": (str,), "family": (str,), "kind": (str,),
    "tk_correction_c": (int, float),
    "temp_alarm_min_c": (int, float), "temp_alarm_max_c": (int, float),
    "temp_alarm_hysteresis_c": (int, float),
    "batt_full_v": (int, float), "batt_warn_v": (int, float),
    "batt_empty_v": (int, float), "tilt_sense_g": (int, float),
    "circuit_mode": (str,), "circuit_hold_s": (int, float),
    "motion_mode": (str,), "motion_window_s": (int, float), "motion_min_detections": (int,),
    "watchdog_hours": (int, float), "application_id": (str,),
    "alarm_window": (str,), "alarm_window_kinds": (list,), "timezone": (str,),
    "threshold": (int,), "frequency": (int, str), "duration": (int,),
    "ntfy_topic": (str,),
    "pushover_user": (str,), "pushover_token": (str,),
    "notify_title": (str,), "notify_fire": (dict,), "notify_clear": (dict,),
    "params": (dict,),
}


def _valid_type(key: str, value: Any) -> bool:
    expected = _FIELD_TYPES[key]
    if isinstance(value, bool) and bool not in expected:
        return False  # True/False must not pass as a number
    if key == "alarm_window_kinds":
        return isinstance(value, list) and all(isinstance(k, str) for k in value)
    return isinstance(value, expected)


def _type_names(types: tuple[type, ...]) -> str:
    return "/".join(t.__name__ for t in types)


class Config:
    def __init__(self, data: dict[str, Any], registry: Registry | None = None):
        data = _expand(data, "config")
        # imported here to avoid an import cycle (definitions imports this module)
        from .definitions import apply_user_definitions, builtin_registry
        # this config's own registry: the built-ins plus the "profiles" section of
        # devices.json. A reload builds a new Config, hence a new fork — nothing to undo.
        self.base_registry = registry if registry is not None else builtin_registry()
        self.registry = self.base_registry.fork()
        apply_user_definitions(self.registry, data.get("profiles"))
        self.defaults = data.get("defaults", {})
        # merge per family so a partial override keeps the built-in keys
        user_families = data.get("families", {})
        family_defaults = {p.name: p.defaults for p in self.registry.profiles() if p.name}
        self.families = {name: {**family_defaults.get(name, {}), **user_families.get(name, {})}
                         for name in {**family_defaults, **user_families}}
        # devEUIs are lowercased on the wire (Uplink lowercases them), so the
        # lookup keys must be lowercase too or an uppercase key never matches
        self.devices = {str(k).lower(): v for k, v in data.get("devices", {}).items()}
        self._warn_unknown("defaults", self.defaults)
        for fam, fvals in data.get("families", {}).items():
            self._warn_unknown(f"families.{fam}", fvals)
        for eui, entry in data.get("devices", {}).items():
            self._warn_unknown(f"devices.{eui}", entry)
        # the ChirpStack application is a property of each device registration,
        # not of its profile — a family/defaults-wide application_id is only
        # correct when every device in that layer really lives in one application
        for where, section in [("defaults", self.defaults)] + [
                (f"families.{fam}", fvals) for fam, fvals in data.get("families", {}).items()]:
            if "application_id" in section:
                log.warning("config %s: application_id applies to every device in that layer; "
                            "with devices spread over several ChirpStack applications, set it "
                            "per device instead (devices.<devEui>.application_id)", where)

    @staticmethod
    def _warn_unknown(where: str, section: dict[str, Any]) -> None:
        for key in section:
            if key not in _FIELD_TYPES:
                log.warning("config %s: unknown key %r ignored (typo?)", where, key)

    @classmethod
    def load(cls, path: str | Path, registry: Registry | None = None) -> "Config":
        return cls(json.loads(Path(path).read_text()), registry)

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
                if k == "params":
                    merged[k] = {**merged.get(k, {}), **v}
                    continue
                merged[k] = v
        fields = set(DeviceConfig.__dataclass_fields__) - {"dev_eui", "name", "family"}
        kwargs = {k: v for k, v in merged.items() if k in fields}
        # the configured name is the most specific source and wins over the
        # ChirpStack deviceName; fall back to deviceName, then the devEUI
        name = merged.get("name") or device_name or dev_eui
        return DeviceConfig(dev_eui=dev_eui, name=name, family=family, **kwargs)
