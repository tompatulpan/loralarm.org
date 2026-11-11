"""Data-driven device definitions: JSON -> DeviceProfile + alarm rules.

One JSON file per device family lives in `loralarm/families/` (built-in), and
`devices.json` may carry more under a top-level `"profiles"` object
(`{"<ChirpStack device-profile name>": {...}}`); a user definition replaces a
built-in one of the same name and disappears again when removed from the file
(devices.json is hot-reloaded). Schema (all keys optional except `name` in
the family files):

    {
      "name": "Dragino Switch LDS02",       # ChirpStack device-profile name
      "aliases": ["lds02"],
      "data_ports": [10],                   # fPorts carrying sensor data (default [2])
      "field_map": {"BAT_V": "battery_voltage"},   # codec field -> canonical field
      "settings": [],                       # omit = standard ACC settings; [] = none;
                                            # else [{key, port, lo, hi, obj_field, kind}]
      "settings_kinds": [],                 # device kinds that take the settings (default ["sensor"])
      "frequency": {"label": "code", "table": {"31": 1}},   # wire value -> Hz
      "defaults": {"kind": "door", "batt_warn_v": 2.5},     # family config defaults
      "rules": [{                           # declarative alarm rules
        "kind": "door", "label": "Dörrlarm",
        "field": "door_open", "when": "truthy",     # truthy falsy eq ne gt ge lt le
        "value": 1, "param": "limit_name",          # eq/gt/...: compare to value, or cfg.params[param]
        "fire": "dörren är öppen", "clear": "dörren är stängd",   # fire: {value} {limit} {field}; clear: {limit} {field}
        "device_kinds": ["door"], "group": null,
        "notify_fire": "...", "notify_clear": "..."
      }]
    }

A bad user definition is logged and skipped; it never stops the service.
"""
from __future__ import annotations

import json
import logging
import operator
from pathlib import Path
from typing import Any, Callable

from .alarms import AlarmKind, AlarmRule, register_rule, rule_for, unregister_rule
from .config import _FIELD_TYPES, DeviceConfig
from .profiles import (ACC_SETTINGS, DeviceProfile, SettingSpec, profiles, register_profile,
                       unregister_profile)

log = logging.getLogger(__name__)

FAMILIES_DIR = Path(__file__).resolve().parent / "families"

_PROFILE_KEYS = {"name", "description", "aliases", "data_ports", "field_map", "settings",
                 "settings_kinds", "frequency", "defaults", "rules"}
_RULE_KEYS = {"kind", "label", "field", "when", "value", "param", "fire", "clear",
              "device_kinds", "group", "notify_fire", "notify_clear"}
_SETTING_KEYS = {"key", "port", "lo", "hi", "obj_field", "kind"}

_COMPARE: dict[str, Callable[[Any, Any], bool]] = {
    "eq": operator.eq, "ne": operator.ne, "gt": operator.gt,
    "ge": operator.ge, "lt": operator.lt, "le": operator.le,
}
_WHEN = set(_COMPARE) | {"truthy", "falsy"}


def _check_keys(what: str, d: dict, allowed: set[str]) -> None:
    unknown = sorted(set(d) - allowed)
    if unknown:
        raise ValueError(f"{what}: unknown key(s) {unknown} (allowed: {sorted(allowed)})")


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _fmt(template: str, **fields: Any) -> str:
    try:
        return template.format(**fields)
    except (KeyError, IndexError, ValueError):
        return template


def rule_from_dict(d: dict[str, Any]) -> AlarmRule:
    _check_keys(f"rule {d.get('kind')!r}", d, _RULE_KEYS)
    for key in ("kind", "label", "field", "when"):
        if not isinstance(d.get(key), str) or not d[key]:
            raise ValueError(f"rule {d.get('kind')!r}: {key!r} is required (string)")
    kind, field_name, when = d["kind"], d["field"], d["when"]
    if when not in _WHEN:
        raise ValueError(f"rule {kind!r}: unknown 'when' {when!r} (use {sorted(_WHEN)})")
    if when in _COMPARE and "value" not in d and "param" not in d:
        raise ValueError(f"rule {kind!r}: 'when': {when!r} needs 'value' or 'param'")
    fire = str(d.get("fire", "{field} = {value}"))
    clear = str(d.get("clear", "inom gräns"))
    device_kinds = d.get("device_kinds")
    if device_kinds is not None and not (isinstance(device_kinds, list)
                                         and all(isinstance(k, str) for k in device_kinds)):
        raise ValueError(f"rule {kind!r}: 'device_kinds' must be a list of strings or null")

    def limit(cfg: DeviceConfig) -> Any:
        param = d.get("param")
        if param is not None and param in cfg.params:
            return cfg.params[param]
        return d.get("value")

    def check(cfg: DeviceConfig, reading: Any, obj: dict) -> str | None:
        v = obj.get(field_name)
        if v is None:
            return None
        lim = limit(cfg)
        if when == "truthy":
            hit = bool(v)
        elif when == "falsy":
            hit = not v
        elif when in ("eq", "ne"):
            hit = _COMPARE[when](v, lim)
        else:   # ordering needs two numbers; anything else never fires
            hit = _is_number(v) and _is_number(lim) and _COMPARE[when](v, lim)
        return _fmt(fire, value=v, limit=lim, field=field_name) if hit else None

    return AlarmRule(
        AlarmKind(kind), d["label"], check,
        lambda cfg, r: _fmt(clear, value="", limit=limit(cfg), field=field_name),
        device_kinds=frozenset(device_kinds) if device_kinds is not None else None,
        group=d.get("group"),
        notify_fire=str(d.get("notify_fire", "")), notify_clear=str(d.get("notify_clear", "")))


def profile_from_dict(name: str, d: dict[str, Any]) -> DeviceProfile:
    _check_keys(f"profile {name!r}", d, _PROFILE_KEYS)
    freq = d.get("frequency")
    freq_table = freq_label = None
    if freq is not None:
        _check_keys(f"profile {name!r} frequency", freq, {"label", "table"})
        freq_table = {int(w): int(hz) for w, hz in freq["table"].items()}
        freq_label = str(freq.get("label", "index"))
    settings = ACC_SETTINGS
    if "settings" in d:
        settings = tuple(_setting_from_dict(name, s) for s in d["settings"])
    defaults = dict(d.get("defaults", {}))
    for key in defaults:
        if key not in _FIELD_TYPES:
            log.warning("profile %s: unknown default %r ignored (typo?)", name, key)
    extra: dict[str, Any] = {}
    if freq_table is not None:
        extra.update(freq_table=freq_table, freq_label=freq_label)
    if "settings_kinds" in d:
        extra["settings_kinds"] = frozenset(d["settings_kinds"])
    if "data_ports" in d:
        extra["data_ports"] = frozenset(int(p) for p in d["data_ports"])
    return DeviceProfile(
        name=name,
        aliases=tuple(str(a).lower() for a in d.get("aliases", ())),
        defaults=defaults, settings=settings,
        field_map={str(k): str(v) for k, v in d.get("field_map", {}).items()},
        **extra)


def _setting_from_dict(profile: str, s: dict[str, Any]) -> SettingSpec:
    _check_keys(f"profile {profile!r} setting {s.get('key')!r}", s, _SETTING_KEYS)
    if "key" not in s or "port" not in s:
        raise ValueError(f"profile {profile!r}: a setting needs 'key' and 'port'")
    return SettingSpec(**s)


def register_definition(name: str, d: dict[str, Any]) -> tuple[DeviceProfile, list[AlarmRule]]:
    """Validate first, register after: a bad definition changes nothing."""
    profile = profile_from_dict(name, d)
    rules = [rule_from_dict(r) for r in d.get("rules", [])]
    register_profile(profile)
    for r in rules:
        register_rule(r)
    return profile, rules


def load_builtin() -> None:
    """Register every `families/*.json` (alphabetical). A broken shipped file is a
    bug, so errors propagate here."""
    for path in sorted(FAMILIES_DIR.glob("*.json")):
        d = json.loads(path.read_text(encoding="utf-8"))
        if "name" not in d:
            raise ValueError(f"{path.name}: missing 'name'")
        register_definition(str(d["name"]), {k: v for k, v in d.items() if k != "name"})


# -- user definitions from devices.json ------------------------------------------

# what the previous user section replaced, so it can be put back on reload
_user_profiles: dict[str, DeviceProfile | None] = {}
_user_rules: dict[str, AlarmRule | None] = {}


def _undo_user_definitions() -> None:
    for kind, original in _user_rules.items():
        unregister_rule(kind)
        if original is not None:
            register_rule(original)
    for name, original in _user_profiles.items():
        unregister_profile(name)
        if original is not None:
            register_profile(original)
    _user_profiles.clear()
    _user_rules.clear()


def apply_user_definitions(section: Any) -> None:
    """Replace the previously applied `"profiles"` section of devices.json with
    this one (None/{} just removes the old one)."""
    _undo_user_definitions()
    if not section:
        return
    if not isinstance(section, dict):
        log.error("config 'profiles' must be an object of {name: definition}; ignored")
        return
    for name, d in section.items():
        if not isinstance(d, dict):
            log.error("config profiles.%s must be an object; ignored", name)
            continue
        try:
            profile, rules = profile_from_dict(name, d), [rule_from_dict(r) for r in d.get("rules", [])]
        except (ValueError, TypeError, KeyError, AttributeError) as e:
            log.error("config profiles.%s ignored: %s: %s", name, type(e).__name__, e)
            continue
        original = next((p for p in profiles() if p.name.lower() == name.lower()), None)
        _user_profiles.setdefault(original.name if original else name, original)
        register_profile(profile)
        for r in rules:
            _user_rules.setdefault(str(r.kind), rule_for(r.kind))
            register_rule(r)
        log.info("config: profile %s defined in devices.json (%d rule(s))", name, len(rules))


__all__ = ["apply_user_definitions", "load_builtin", "register_definition",
           "profile_from_dict", "rule_from_dict"]
