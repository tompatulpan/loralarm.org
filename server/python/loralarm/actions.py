"""Config-driven actions: what a device event does besides (or instead of) an alarm.

Declared as an `actions` list on a device (devices.json) or as a family default
(`"defaults": {"actions": [...]}` in a family file); a device's list replaces the
family's. Each entry is a trigger on one decoded field plus an action type:

    {"field": "button", "when": "eq", "value": "double press",
     "do": "downlink", "target": "siren1", "ascii": "001", "port": 10}

Trigger keys (same vocabulary as alarm rules): `field`, `when` (eq ne gt ge lt le
in truthy falsy), `value` or `param` (a key of the device's `params`).

`do`:
  alarm     the event keeps raising its normal alarm (the default for any event
            no action matches, so an unmapped panic button stays a panic button)
  ignore    swallow the event: no alarm, nothing sent
  downlink  send one frame to `target` (devEUI or name): `port` plus the payload as
            `ascii` text or `hex`; optional `confirmed`. Replaces the alarm unless
            `"alarm": true` is added.

Adding an action type = one entry in ACTION_TYPES (validation) and one branch in
Engine._run_actions (execution). Nothing else knows about the types.
"""
from __future__ import annotations

import logging
import operator
from dataclasses import dataclass
from typing import Any, Callable

from .config import DeviceConfig
from .downlink import DownlinkFrame
from .registry import Registry

log = logging.getLogger(__name__)

_COMPARE: dict[str, Callable[[Any, Any], bool]] = {
    "eq": operator.eq, "ne": operator.ne, "gt": operator.gt,
    "ge": operator.ge, "lt": operator.lt, "le": operator.le,
}
_WHEN = set(_COMPARE) | {"truthy", "falsy", "in"}
ACTION_TYPES = ("alarm", "ignore", "downlink")
_KEYS = {"field", "when", "value", "param", "do", "alarm", "target", "port",
         "ascii", "hex", "confirmed", "note"}
MAX_PAYLOAD = 51        # bytes: the smallest LoRaWAN maximum (EU868 DR0)


@dataclass(frozen=True)
class Action:
    field: str
    when: str
    do: str
    value: Any = None
    param: str = ""
    keep_alarm: bool = False
    target: str = ""
    frame: DownlinkFrame | None = None

    def hit(self, cfg: DeviceConfig, values: dict[str, Any]) -> bool:
        v = values.get(self.field)
        if v is None:
            return False
        lim = cfg.params.get(self.param, self.value) if self.param else self.value
        if self.when == "truthy":
            return bool(v)
        if self.when == "falsy":
            return not v
        if self.when == "in":
            return isinstance(lim, (list, tuple, set)) and v in lim
        if self.when in ("eq", "ne"):
            return _COMPARE[self.when](v, lim)
        return (isinstance(v, (int, float)) and isinstance(lim, (int, float))
                and not isinstance(v, bool) and not isinstance(lim, bool)
                and _COMPARE[self.when](v, lim))

    @property
    def suppresses_alarm(self) -> bool:
        return self.do != "alarm" and not self.keep_alarm


def action_from_dict(d: Any) -> Action:
    """Validate one `actions` entry; ValueError names the problem."""
    if not isinstance(d, dict):
        raise ValueError("an action must be an object")
    unknown = sorted(set(d) - _KEYS)
    if unknown:
        raise ValueError(f"unknown key(s) {unknown} (allowed: {sorted(_KEYS)})")
    for key in ("field", "when", "do"):
        if not isinstance(d.get(key), str) or not d[key]:
            raise ValueError(f"{key!r} is required (string)")
    when, do = d["when"], d["do"]
    if when not in _WHEN:
        raise ValueError(f"unknown 'when' {when!r} (use {sorted(_WHEN)})")
    if when in (*_COMPARE, "in") and "value" not in d and "param" not in d:
        raise ValueError(f"'when': {when!r} needs 'value' or 'param'")
    if when == "in" and "value" in d and not isinstance(d["value"], list):
        raise ValueError("'when': 'in' needs a list under 'value'")
    if do not in ACTION_TYPES:
        raise ValueError(f"unknown 'do' {do!r} (use {list(ACTION_TYPES)})")
    if "alarm" in d and not isinstance(d["alarm"], bool):
        raise ValueError("'alarm' must be true or false")
    frame, target = None, ""
    if do == "downlink":
        target = d.get("target")
        if not isinstance(target, str) or not target.strip():
            raise ValueError("'downlink' needs 'target' (devEUI or device name)")
        frame = _frame_from_dict(d)
    return Action(field=d["field"], when=when, do=do, value=d.get("value"),
                  param=str(d.get("param", "")), keep_alarm=bool(d.get("alarm", False)),
                  target=target.strip(), frame=frame)


def _frame_from_dict(d: dict[str, Any]) -> DownlinkFrame:
    port = d.get("port")
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 223:
        raise ValueError("'downlink' needs 'port' (integer 1-223)")
    if ("ascii" in d) == ("hex" in d):
        raise ValueError("'downlink' needs exactly one of 'ascii' or 'hex'")
    if "ascii" in d:
        if not isinstance(d["ascii"], str) or not d["ascii"].isascii():
            raise ValueError("'ascii' must be an ASCII string")
        data = d["ascii"].encode("ascii")
    else:
        try:
            data = bytes.fromhex(d["hex"]) if isinstance(d["hex"], str) else None
        except ValueError:
            data = None
        if data is None:
            raise ValueError("'hex' must be a string of hex digits")
    if not data or len(data) > MAX_PAYLOAD:
        raise ValueError(f"payload must be 1-{MAX_PAYLOAD} bytes")
    confirmed = d.get("confirmed", False)
    if not isinstance(confirmed, bool):
        raise ValueError("'confirmed' must be true or false")
    return DownlinkFrame(port, data, confirmed=confirmed, note=str(d.get("note", "")))


def replaces_pulse(registry: Registry, kind: Any, fields: set[str]) -> bool:
    """True when `kind` is a pulse (fire-only) alarm read from one of `fields`."""
    rule = registry.rule_for(kind)
    return rule is not None and rule.pulse and rule.source_field in fields


class ActionSet:
    """Parses the `actions` of a device config; a bad entry is logged once and
    skipped, never raised (it must not cost an uplink its alarm)."""

    def __init__(self) -> None:
        self._warned: set[tuple[str, int, str]] = set()

    def for_device(self, cfg: DeviceConfig) -> list[Action]:
        out: list[Action] = []
        for i, entry in enumerate(cfg.actions):
            try:
                out.append(action_from_dict(entry))
            except ValueError as e:
                key = (cfg.dev_eui, i, str(e))
                if key not in self._warned:
                    self._warned.add(key)
                    log.error("config %s (%s): actions[%d] ignored: %s", cfg.dev_eui, cfg.name, i, e)
        return out
