"""Frame: the evaluated uplink as named measurements.

The codec object is the device boundary; a device type's `fields` say how it
becomes *values*: renamed (`from`), calibrated (`transform`) or derived
(`derive`) from other values. Nothing in here knows what "temperature" or
"battery" mean — they are ordinary declarations in the family JSON, and the
operations (`subtract`, `round`, `linear_pct`, `band`, ...) are plugins in the
`Registry`, so a new calibration is one function.

    "fields": {
      "temperature": {"from": "temperature", "unit": "°C", "type": "number",
                     "transform": [{"op": "subtract", "by": "$tk_correction_c"},
                                   {"op": "round", "n": 2}]},
      "battery_pct": {"derive": "linear_pct", "of": "battery_voltage",
                     "empty": "$batt_empty_v", "full": "$batt_full_v", "round": 1}
    }

`$name` refers to a per-device parameter (a config key or an entry of
`params`), resolved when a frame is built. Codec fields that are not declared
pass through unchanged, so a rule or the status page can read any of them; a
declared field replaces the raw one of the same name, and disappears when its
source is missing or not valid (never a silent 0).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from .registry import Registry

log = logging.getLogger(__name__)

Resolve = Callable[[Any], Any]
Transform = Callable[[Any, Mapping[str, Any], Resolve], Any]
Derivation = Callable[[Mapping[str, Any], Mapping[str, Any], Resolve], Any]


@dataclass
class Frame:
    """One evaluated uplink: measurements plus link quality."""
    values: dict[str, Any] = field(default_factory=dict)
    rssi: int | None = None
    snr: float | None = None


def is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def param_resolver(cfg: Any) -> Resolve:
    """`$name` -> the device's value: a config attribute, else `cfg.params[name]`;
    anything else is returned as is. An unknown name raises ValueError."""
    def resolve(value: Any) -> Any:
        if isinstance(value, str) and value.startswith("$"):
            name = value[1:]
            if hasattr(cfg, name):
                return getattr(cfg, name)
            params = getattr(cfg, "params", None) or {}
            if name in params:
                return params[name]
            raise ValueError(f"unknown parameter {value!r}")
        return value
    return resolve


# -- transforms: (value, spec, resolve) -> new value, or None to drop the field ----

def _arith(op: Callable[[Any, Any], Any]) -> Transform:
    def transform(value: Any, spec: Mapping[str, Any], resolve: Resolve) -> Any:
        by = resolve(spec["by"])
        return op(value, by) if is_number(value) and is_number(by) else None
    return transform


def _t_round(value: Any, spec: Mapping[str, Any], resolve: Resolve) -> Any:
    return round(value, int(spec.get("n", 0))) if is_number(value) else None


def _t_clamp(value: Any, spec: Mapping[str, Any], resolve: Resolve) -> Any:
    if not is_number(value):
        return None
    lo, hi = resolve(spec.get("min")), resolve(spec.get("max"))
    if lo is not None:
        value = max(value, lo)
    if hi is not None:
        value = min(value, hi)
    return value


# -- derivations: (spec, values so far, resolve) -> value, or None to drop ----------

def _d_linear_pct(spec: Mapping[str, Any], values: Mapping[str, Any], resolve: Resolve) -> Any:
    v = values.get(spec["of"])
    empty, full = resolve(spec["empty"]), resolve(spec["full"])
    if not (is_number(v) and is_number(empty) and is_number(full)):
        return None
    span = full - empty
    if span <= 0:      # misconfigured limits (full <= empty): a percentage is meaningless
        return None
    pct = min(max((v - empty) / span * 100, 0.0), 100.0)
    return round(pct, int(spec["round"])) if "round" in spec else pct


_BAND_OPS: dict[str, Callable[[Any, Any], bool]] = {
    "gt": lambda a, b: a > b, "ge": lambda a, b: a >= b,
    "lt": lambda a, b: a < b, "le": lambda a, b: a <= b,
}


def _d_band(spec: Mapping[str, Any], values: Mapping[str, Any], resolve: Resolve) -> Any:
    """First matching band's `as`; `else` if none; `missing` if the source is absent."""
    v = values.get(spec["of"])
    if not is_number(v):
        return spec.get("missing")
    for band in spec.get("bands", ()):
        for op, compare in _BAND_OPS.items():
            if op in band:
                limit = resolve(band[op])
                if is_number(limit) and compare(v, limit):
                    return band["as"]
    return spec.get("else")


def register_builtin_transforms(registry: Registry) -> None:
    registry.register_transform("add", _arith(lambda a, b: a + b))
    registry.register_transform("subtract", _arith(lambda a, b: a - b))
    registry.register_transform("multiply", _arith(lambda a, b: a * b))
    registry.register_transform("round", _t_round)
    registry.register_transform("clamp", _t_clamp)
    registry.register_derivation("linear_pct", _d_linear_pct)
    registry.register_derivation("band", _d_band)


def build_values(fields: Mapping[str, Mapping[str, Any]], raw: Mapping[str, Any],
                 cfg: Any, registry: Registry) -> dict[str, Any]:
    """Codec object -> measurements. `from` fields first (renames/calibration),
    then `derive` fields, which may use them. A field that cannot be produced
    is removed from the result and reported once per registry."""
    values = dict(raw)
    resolve = param_resolver(cfg)
    derived: list[tuple[str, Mapping[str, Any]]] = []
    for name, spec in fields.items():
        if "derive" in spec:
            derived.append((name, spec))
            continue
        source = spec.get("from", name)
        if source not in raw:
            continue
        value = raw[source]
        try:
            if spec.get("type") == "number" and not is_number(value):
                value = None
            else:
                for step in spec.get("transform", ()):
                    if value is None:
                        break
                    value = registry.transform(step["op"])(value, step, resolve)
        except (ValueError, KeyError, TypeError) as e:
            _warn_once(registry, name, e)
            value = None
        _put(values, name, value)
    for name, spec in derived:
        try:
            value = registry.derivation(spec["derive"])(spec, values, resolve)
        except (ValueError, KeyError, TypeError) as e:
            _warn_once(registry, name, e)
            value = None
        _put(values, name, value)
    return values


def _put(values: dict[str, Any], name: str, value: Any) -> None:
    if value is None:
        values.pop(name, None)
    else:
        values[name] = value


def _warn_once(registry: Registry, name: str, error: Exception) -> None:
    key = (name, f"{type(error).__name__}: {error}")
    if key not in registry.warned_fields:
        registry.warned_fields.add(key)
        log.warning("field %r cannot be built (%s: %s); it is left out", name,
                    type(error).__name__, error)
