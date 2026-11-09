"""Alarm evaluation — a small, pure port of the Node-RED alarm subflows.

`evaluate()` derives readings and currently-firing alarm kinds from one decoded
uplink. `AlarmState` adds fire-once / clear-once semantics per device, so
notifications are emitted only on transitions, plus a watchdog for stale devices.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum

from .config import DeviceConfig
from .event import Uplink


class AlarmKind(str, Enum):
    TEMP = "temp"
    BATTERY_LOW = "battery_low"
    BATTERY_EMPTY = "battery_empty"
    MOTION = "motion"        # motion_alert (accelerometer interrupt) -> rörelselarm
    CIRCUIT = "circuit"      # motion_switch / door contact -> kretslarm
    TILT = "tilt"
    STALE = "stale"          # watchdog: no uplink within watchdog_hours


@dataclass
class Reading:
    temp_c: float | None
    batt_v: float | None
    batt_percent: float | None
    batt_status: str         # ok | low | empty | unknown
    rssi: int | None
    snr: float | None


@dataclass
class Transition:
    kind: AlarmKind
    active: bool             # True = alarm fired, False = cleared
    detail: str


def evaluate(cfg: DeviceConfig, up: Uplink) -> tuple[Reading, list[Transition]]:
    o = up.obj

    temp = o.get("temperature")
    temp_c = round(temp - cfg.tk_correction_c, 2) if isinstance(temp, (int, float)) else None

    batt_v = o.get("battery_voltage")
    if isinstance(batt_v, (int, float)):
        pct = (batt_v - cfg.batt_empty_v) / (cfg.batt_full_v - cfg.batt_empty_v) * 100
        batt_percent = round(min(max(pct, 0.0), 100.0), 1)
        batt_status = "ok" if batt_v > cfg.batt_warn_v else ("low" if batt_v >= cfg.batt_empty_v else "empty")
    else:
        batt_percent, batt_status = None, "unknown"

    reading = Reading(temp_c=temp_c, batt_v=batt_v if isinstance(batt_v, (int, float)) else None,
                      batt_percent=batt_percent, batt_status=batt_status, rssi=up.rssi, snr=up.snr)

    fired: list[Transition] = []
    if temp_c is not None and temp_c <= cfg.temp_alarm_limit_c:
        fired.append(Transition(AlarmKind.TEMP, True, f"{temp_c} °C ≤ limit {cfg.temp_alarm_limit_c} °C"))
    if batt_status == "low":
        fired.append(Transition(AlarmKind.BATTERY_LOW, True, f"{batt_v} V ≤ warn {cfg.batt_warn_v} V"))
    elif batt_status == "empty":
        fired.append(Transition(AlarmKind.BATTERY_EMPTY, True, f"{batt_v} V < empty {cfg.batt_empty_v} V"))
    if o.get("motion_alert"):
        fired.append(Transition(AlarmKind.MOTION, True, "motion_alert=1"))
    if o.get("motion_switch"):
        fired.append(Transition(AlarmKind.CIRCUIT, True, "circuit open"))
    return reading, fired


def tilt_deviation(acc: dict, baseline: dict[str, float], sense: float) -> str | None:
    """Axis whose current value left [baseline - sense, baseline + sense], if any.

    Mirrors the Node-RED tilt chain: per axis a `smooth` baseline is kept and the
    X/Y/Z alarm switches fire when the live value exceeds baseline ± tilt_sense.
    """
    for axis, v in acc.items():
        if isinstance(v, (int, float)) and axis in baseline:
            if abs(float(v) - baseline[axis]) > sense:
                return axis
    return None


@dataclass
class AlarmState:
    """Per-device fire-once / clear-once state plus last-seen for the watchdog."""

    active: set[AlarmKind] = field(default_factory=set)
    last_seen: datetime | None = None
    accel_baseline: dict[str, float] = field(default_factory=dict)  # EMA per axis, from non-alarming samples

    def update(self, cfg: DeviceConfig, up: Uplink) -> tuple[Reading, list[Transition]]:
        reading, fired = evaluate(cfg, up)
        self.last_seen = up.time

        acc = up.obj.get("accelerometer")
        if isinstance(acc, dict) and all(isinstance(v, (int, float)) for v in acc.values()):
            axis = tilt_deviation(acc, self.accel_baseline, cfg.tilt_sense_g)
            if axis is not None:
                fired.append(Transition(AlarmKind.TILT, True,
                                        f"{axis} moved {abs(float(acc[axis]) - self.accel_baseline[axis]):.3f} g > {cfg.tilt_sense_g}"))
            else:
                # update baseline only from calm samples so a tilted reading never becomes the reference
                for k, v in acc.items():
                    self.accel_baseline[k] = float(v) if k not in self.accel_baseline else 0.7 * self.accel_baseline[k] + 0.3 * float(v)

        now_active = {t.kind for t in fired}
        transitions: list[Transition] = []
        for t in fired:
            if t.kind not in self.active:
                transitions.append(t)
        for kind in self.active - now_active:
            transitions.append(Transition(kind, False, "cleared"))
        self.active = now_active
        return reading, transitions

    def watchdog(self, cfg: DeviceConfig, now: datetime) -> list[Transition]:
        if self.last_seen is None or cfg.watchdog_hours <= 0:
            return []
        stale = now - self.last_seen > timedelta(hours=cfg.watchdog_hours)
        if stale and AlarmKind.STALE not in self.active:
            self.active.add(AlarmKind.STALE)
            return [Transition(AlarmKind.STALE, True, f"no uplink for {now - self.last_seen}")]
        if not stale and AlarmKind.STALE in self.active:
            self.active.discard(AlarmKind.STALE)
            return [Transition(AlarmKind.STALE, False, "uplink received")]
        return []
