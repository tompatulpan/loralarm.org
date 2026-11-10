"""Alarm evaluation — a small, pure port of the Node-RED alarm subflows.

`evaluate()` derives readings and currently-firing alarm kinds from one decoded
uplink. `AlarmState` adds fire-once / clear-once semantics per device, so
notifications are emitted only on transitions, plus a watchdog for stale devices.

Only full sensor frames (fPort 2 with a non-empty decoded object) are evaluated;
settings frames and codec failures just refresh `last_seen` via `touch()`, so they
never clear active alarms. The STALE kind is managed exclusively by the watchdog
so its clear reads "uplink mottagen".
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any

from .config import DeviceConfig
from .event import Uplink


class AlarmKind(str, Enum):
    TEMP = "temp"
    BATTERY_LOW = "battery_low"
    BATTERY_EMPTY = "battery_empty"
    MOTION = "motion"        # motion_alert (accelerometer interrupt) -> rörelselarm
    CIRCUIT = "circuit"      # motion_switch / door contact -> kretsalarm
    TILT = "tilt"
    STALE = "stale"          # watchdog: no uplink within watchdog_hours


# user-facing Swedish labels for every alarm kind (notification text + console)
LABELS = {
    "temp": "Temperaturalarm",
    "battery_low": "Batterinivå låg",
    "battery_empty": "Batteri tomt",
    "motion": "Rörelselarm",
    "circuit": "Kretsalarm",
    "tilt": "Tiltlarm",
    "stale": "Ingen kontakt (watchdog)",
}

# alarm kinds that describe the same physical quantity at different severity;
# moving between them is an escalation/de-escalation, not a clear (so
# low -> empty must not send "Batteri OK")
_ESCALATION_GROUP: dict[AlarmKind, str] = {
    AlarmKind.BATTERY_LOW: "battery",
    AlarmKind.BATTERY_EMPTY: "battery",
}

# after this many consecutive tilted samples the baseline is re-accepted, so a
# sensor that is permanently moved does not stay alarmed forever
TILT_REBASELINE_AFTER = 10


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


def format_duration(td: timedelta) -> str:
    """Swedish duration without microseconds: '2:03:04' or '1 dag, 2:03:04'."""
    total = int(td.total_seconds())
    days, rem = divmod(total, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    clock = f"{h:02d}:{m:02d}:{s:02d}"
    if days:
        return f"{days} dag{'ar' if days != 1 else ''}, {clock}"
    return clock


def evaluate(cfg: DeviceConfig, up: Uplink) -> tuple[Reading, list[Transition]]:
    o = up.obj

    temp = o.get("temperature")
    temp_c = round(temp - cfg.tk_correction_c, 2) if isinstance(temp, (int, float)) else None

    batt_v = o.get("battery_voltage")
    if isinstance(batt_v, (int, float)):
        span = cfg.batt_full_v - cfg.batt_empty_v
        if span > 0:
            pct = (batt_v - cfg.batt_empty_v) / span * 100
            batt_percent: float | None = round(min(max(pct, 0.0), 100.0), 1)
        else:  # misconfigured thresholds (full == empty): percent is meaningless
            batt_percent = None
        batt_status = "ok" if batt_v > cfg.batt_warn_v else ("low" if batt_v >= cfg.batt_empty_v else "empty")
    else:
        batt_v = None
        batt_percent, batt_status = None, "unknown"

    reading = Reading(temp_c=temp_c, batt_v=batt_v,
                      batt_percent=batt_percent, batt_status=batt_status, rssi=up.rssi, snr=up.snr)

    fired: list[Transition] = []
    if temp_c is not None and temp_c <= cfg.temp_alarm_limit_c:
        fired.append(Transition(AlarmKind.TEMP, True,
                                f"{temp_c} °C ≤ gräns {cfg.temp_alarm_limit_c} °C"))
    pct_txt = f"{batt_percent} %" if batt_percent is not None else "–"
    if batt_status == "low":
        fired.append(Transition(AlarmKind.BATTERY_LOW, True,
                                f"{batt_v:.2f} V ({pct_txt}) ≤ varningsgräns {cfg.batt_warn_v} V"))
    elif batt_status == "empty":
        fired.append(Transition(AlarmKind.BATTERY_EMPTY, True,
                                f"{batt_v:.2f} V ({pct_txt}) < tomt {cfg.batt_empty_v} V"))
    if o.get("motion_alert"):
        fired.append(Transition(AlarmKind.MOTION, True, "rörelse upptäckt"))
    if o.get("motion_switch"):
        fired.append(Transition(AlarmKind.CIRCUIT, True, "krets öppen"))
    return reading, fired


def clear_detail(kind: AlarmKind, cfg: DeviceConfig, reading: Reading) -> str:
    """Clear text built from the *current* reading, not the stale fire detail."""
    if kind is AlarmKind.TEMP:
        return (f"{reading.temp_c} °C > gräns {cfg.temp_alarm_limit_c} °C"
                if reading.temp_c is not None else "inom gräns")
    if kind is AlarmKind.BATTERY_LOW:
        return (f"{reading.batt_v:.2f} V > varningsgräns {cfg.batt_warn_v} V"
                if reading.batt_v is not None else "över varningsgräns")
    if kind is AlarmKind.BATTERY_EMPTY:
        return (f"{reading.batt_v:.2f} V ≥ tomt {cfg.batt_empty_v} V"
                if reading.batt_v is not None else "över tomt")
    if kind is AlarmKind.MOTION:
        return "ingen rörelse"
    if kind is AlarmKind.CIRCUIT:
        return "krets sluten"
    if kind is AlarmKind.TILT:
        return f"inom baslinje ± {cfg.tilt_sense_g} g"
    return "kvitterad"


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
    last_detail: dict[AlarmKind, str] = field(default_factory=dict)  # fired detail, for the event log
    tilt_streak: int = 0                                            # consecutive tilted samples
    last_circuit_event: datetime | None = None                      # event-mode circuit dedup

    def touch(self, up: Uplink) -> None:
        """Non-sensor frame (settings fPort 4-7, codec failure, ...): only refresh
        last_seen so the watchdog stays quiet; never evaluate or clear alarms."""
        self.last_seen = up.time

    def update(self, cfg: DeviceConfig, up: Uplink) -> tuple[Reading, list[Transition]]:
        reading, fired = evaluate(cfg, up)
        self.last_seen = up.time

        acc = up.obj.get("accelerometer")
        if isinstance(acc, dict) and all(isinstance(v, (int, float)) for v in acc.values()):
            axis = tilt_deviation(acc, self.accel_baseline, cfg.tilt_sense_g)
            if axis is not None:
                self.tilt_streak += 1
                if self.tilt_streak >= TILT_REBASELINE_AFTER:
                    # permanently moved: accept the new orientation as baseline;
                    # the tilt alarm then clears on this/next update
                    for k, v in acc.items():
                        self.accel_baseline[k] = float(v)
                    self.tilt_streak = 0
                else:
                    fired.append(Transition(AlarmKind.TILT, True,
                                            f"{axis}-axeln rörde sig {abs(float(acc[axis]) - self.accel_baseline[axis]):.2f} g (> {cfg.tilt_sense_g} g)"))
            else:
                # update baseline only from calm samples so a tilted reading never becomes the reference
                self.tilt_streak = 0
                for k, v in acc.items():
                    self.accel_baseline[k] = float(v) if k not in self.accel_baseline else 0.7 * self.accel_baseline[k] + 0.3 * float(v)

        if cfg.circuit_mode == "event":
            fired = [t for t in fired if t.kind is not AlarmKind.CIRCUIT]
            circuit = self._circuit_event(cfg, up)
            transitions_pre = [circuit] if circuit else []
        else:
            transitions_pre = []

        now_active = {t.kind for t in fired}
        # STALE is owned by the watchdog: an uplink keeps it active here and the
        # next watchdog tick clears it with "uplink mottagen"
        if AlarmKind.STALE in self.active:
            now_active.add(AlarmKind.STALE)

        transitions: list[Transition] = list(transitions_pre)
        for t in fired:
            if t.kind not in self.active:
                transitions.append(t)
                self.last_detail[t.kind] = t.detail
        for kind in self.active - now_active:
            # escalation within a group (battery low <-> empty) is not a clear:
            # suppress the "OK" notification while the other severity fires
            group = _ESCALATION_GROUP.get(kind)
            if group and any(_ESCALATION_GROUP.get(t.kind) == group for t in fired):
                self.last_detail.pop(kind, None)
                continue
            transitions.append(Transition(kind, False, clear_detail(kind, cfg, reading)))
            self.last_detail.pop(kind, None)
        self.active = now_active
        return reading, transitions

    def _circuit_event(self, cfg: DeviceConfig, up: Uplink) -> Transition | None:
        """Event-mode circuit (RAK4631): the firmware sends bit 0 in one uplink when
        the contact opens and nothing when it closes, so a frame without the bit
        says nothing about the circuit. Each pulse is a fire-only alarm; pulses
        within circuit_hold_s (retransmits, contact bounce) count as one."""
        if not up.obj.get("motion_switch"):
            return None
        last = self.last_circuit_event
        if last is not None and abs((up.time - last).total_seconds()) < cfg.circuit_hold_s:
            return None
        self.last_circuit_event = up.time
        return Transition(AlarmKind.CIRCUIT, True, "krets öppen")

    def watchdog(self, cfg: DeviceConfig, now: datetime) -> list[Transition]:
        if self.last_seen is None or cfg.watchdog_hours <= 0:
            return []
        # normalize tz so a naive last_seen (old DB rows) never collides with aware now
        last_seen = self.last_seen if self.last_seen.tzinfo else self.last_seen.replace(tzinfo=timezone.utc)
        now = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
        stale = now - last_seen > timedelta(hours=cfg.watchdog_hours)
        if stale and AlarmKind.STALE not in self.active:
            self.active.add(AlarmKind.STALE)
            return [Transition(AlarmKind.STALE, True,
                               f"ingen uplink på {format_duration(now - last_seen)}")]
        if not stale and AlarmKind.STALE in self.active:
            self.active.discard(AlarmKind.STALE)
            return [Transition(AlarmKind.STALE, False, "uplink mottagen")]
        return []

    # -- persistence (service seeds/restores state across restarts) ----------

    def snapshot(self) -> dict[str, Any]:
        return {
            "active": sorted(k.value for k in self.active),
            "last_seen": self.last_seen.isoformat() if self.last_seen else None,
            "accel_baseline": self.accel_baseline,
            "last_detail": {k.value: v for k, v in self.last_detail.items()},
            "tilt_streak": self.tilt_streak,
            "last_circuit_event": self.last_circuit_event.isoformat() if self.last_circuit_event else None,
        }

    def restore(self, snap: dict[str, Any]) -> None:
        self.active = {AlarmKind(v) for v in snap.get("active", [])}
        ls = snap.get("last_seen")
        self.last_seen = datetime.fromisoformat(ls) if ls else None
        self.accel_baseline = {k: float(v) for k, v in snap.get("accel_baseline", {}).items()}
        self.last_detail = {AlarmKind(k): v for k, v in snap.get("last_detail", {}).items()}
        self.tilt_streak = int(snap.get("tilt_streak", 0))
        lc = snap.get("last_circuit_event")
        self.last_circuit_event = datetime.fromisoformat(lc) if lc else None
