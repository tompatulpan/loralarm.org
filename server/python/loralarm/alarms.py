"""Alarm evaluation — a small, pure port of the Node-RED alarm subflows.

Level alarms are rule-driven: each `AlarmRule` (temp, battery, motion, circuit
level, ...) lives in a registry and names the device kinds it applies to, so a
new sensor type is one `register_rule()` call (see the example in README.md).
Stateful detectors (tilt baseline, pulse-style motion/circuit, watchdog) stay
in `AlarmState`; they are registered label-only (`check=None`) so labels,
clear texts, notification fallbacks and escalation groups still come from one
place.

`evaluate()` derives readings and currently-firing alarm kinds from one decoded
uplink. `AlarmState` adds fire-once / clear-once semantics per device, so
notifications are emitted only on transitions, plus a watchdog for stale devices.
Pulse-style inputs (motion, event-mode circuit) are the exception: every pulse is
reported fire-only, and motion detections are counted in a sliding window so a
"N detections within M seconds" rule is a config change, not a code change.

Only full sensor frames (fPort 2 with a non-empty decoded object) are evaluated;
settings frames and codec failures just refresh `last_seen` via `touch()`, so they
never clear active alarms. The STALE kind is managed exclusively by the watchdog
so its clear reads "uplink mottagen".
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, ClassVar

from .config import DeviceConfig
from .event import Uplink

log = logging.getLogger(__name__)


class AlarmKind(str):
    """An alarm kind is just its string key: the built-ins below, or any new
    one created by `register_rule` (`AlarmKind("leak")`). Compare with `==`."""

    __slots__ = ()

    TEMP: ClassVar["AlarmKind"]
    BATTERY_LOW: ClassVar["AlarmKind"]
    BATTERY_EMPTY: ClassVar["AlarmKind"]
    MOTION: ClassVar["AlarmKind"]       # motion_alert (accelerometer interrupt) -> rörelselarm
    CIRCUIT: ClassVar["AlarmKind"]      # motion_switch / door contact -> kretsalarm
    DOOR: ClassVar["AlarmKind"]         # door_open (door contact) -> dörrlarm
    TILT: ClassVar["AlarmKind"]
    STALE: ClassVar["AlarmKind"]        # watchdog: no uplink within watchdog_hours

    @property
    def value(self) -> str:
        return str(self)


AlarmKind.TEMP = AlarmKind("temp")
AlarmKind.BATTERY_LOW = AlarmKind("battery_low")
AlarmKind.BATTERY_EMPTY = AlarmKind("battery_empty")
AlarmKind.MOTION = AlarmKind("motion")
AlarmKind.CIRCUIT = AlarmKind("circuit")
AlarmKind.DOOR = AlarmKind("door")
AlarmKind.TILT = AlarmKind("tilt")
AlarmKind.STALE = AlarmKind("stale")

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


# -- rule registry -------------------------------------------------------------

# device kinds (DeviceConfig.kind) that carry the full sensor set
SENSOR = frozenset({"sensor"})


@dataclass(frozen=True)
class AlarmRule:
    """One alarm kind.

    check(cfg, reading, obj) returns the fire detail while the condition holds,
    else None; None as `check` itself marks a stateful detector that AlarmState
    runs (tilt, watchdog) and that only contributes label/texts/group.
    clear(cfg, reading) builds the clear text from the *current* reading.
    `device_kinds` limits the rule to those `DeviceConfig.kind` values
    (None = every kind). `group` ties severities of one quantity together:
    moving within a group is an escalation, not a clear. Rule-specific
    thresholds belong in `cfg.params` ("params" in devices.json).
    """
    kind: AlarmKind
    label: str
    check: Callable[[DeviceConfig, "Reading", dict], str | None] | None = None
    clear: Callable[[DeviceConfig, "Reading"], str] | None = None
    device_kinds: frozenset[str] | None = None
    group: str | None = None
    notify_fire: str = ""       # fallback texts for kinds notify.DEFAULT_TEXT lacks;
    notify_clear: str = ""      # {name} {detail} {label}; empty -> generic text


_RULES: list[AlarmRule] = []
# user-facing Swedish labels for every registered alarm kind (notification text + console)
LABELS: dict[str, str] = {}
_warned_kinds: set[str] = set()


def register_rule(rule: AlarmRule) -> AlarmRule:
    """Add (or replace, by kind) an alarm rule."""
    unregister_rule(rule.kind)
    _RULES.append(rule)
    LABELS[str(rule.kind)] = rule.label
    return rule


def unregister_rule(kind: str) -> None:
    _RULES[:] = [r for r in _RULES if r.kind != kind]
    LABELS.pop(str(kind), None)


def rule_for(kind: str) -> AlarmRule | None:
    return next((r for r in _RULES if r.kind == kind), None)


def label_for(kind: str) -> str:
    return LABELS.get(str(kind), str(kind))


def known_device_kinds() -> set[str]:
    kinds = set(SENSOR)
    for r in _RULES:
        kinds |= r.device_kinds or set()
    return kinds


def rule_applies(kind: str, cfg: DeviceConfig) -> bool:
    """True if the alarm kind is evaluated for this device's `kind`."""
    r = rule_for(kind)
    return r is None or r.device_kinds is None or cfg.kind in r.device_kinds


def _warn_unknown_kind(cfg: DeviceConfig) -> None:
    if cfg.kind not in known_device_kinds() and cfg.kind not in _warned_kinds:
        _warned_kinds.add(cfg.kind)
        log.warning("device %s: unknown kind %r (known: %s); only rules that apply to "
                    "every kind will run", cfg.dev_eui, cfg.kind, sorted(known_device_kinds()))


def _known_kind(value: str) -> AlarmKind:
    """AlarmKind for a persisted key; ValueError if no rule is registered for it
    (corrupt snapshot, or a rule that has since been removed)."""
    if rule_for(value) is None:
        raise ValueError(f"unknown alarm kind {value!r}")
    return AlarmKind(value)


def _group(kind: AlarmKind) -> str | None:
    r = rule_for(kind)
    return r.group if r else None


def _temp_check(cfg: DeviceConfig, r: "Reading", obj: dict) -> str | None:
    if r.temp_c is not None and r.temp_c <= cfg.temp_alarm_limit_c:
        return f"{r.temp_c} °C ≤ gräns {cfg.temp_alarm_limit_c} °C"
    return None


def _pct_txt(r: "Reading") -> str:
    return f"{r.batt_percent} %" if r.batt_percent is not None else "–"


def _batt_low_check(cfg: DeviceConfig, r: "Reading", obj: dict) -> str | None:
    if r.batt_status == "low":
        return f"{r.batt_v:.2f} V ({_pct_txt(r)}) ≤ varningsgräns {cfg.batt_warn_v} V"
    return None


def _batt_empty_check(cfg: DeviceConfig, r: "Reading", obj: dict) -> str | None:
    if r.batt_status == "empty":
        return f"{r.batt_v:.2f} V ({_pct_txt(r)}) < tomt {cfg.batt_empty_v} V"
    return None


register_rule(AlarmRule(
    AlarmKind.TEMP, "Temperaturalarm", _temp_check,
    lambda cfg, r: (f"{r.temp_c} °C > gräns {cfg.temp_alarm_limit_c} °C"
                    if r.temp_c is not None else "inom gräns")))
register_rule(AlarmRule(
    AlarmKind.BATTERY_LOW, "Batterinivå låg", _batt_low_check,
    lambda cfg, r: (f"{r.batt_v:.2f} V > varningsgräns {cfg.batt_warn_v} V"
                    if r.batt_v is not None else "över varningsgräns"),
    group="battery"))
register_rule(AlarmRule(
    AlarmKind.BATTERY_EMPTY, "Batteri tomt", _batt_empty_check,
    lambda cfg, r: (f"{r.batt_v:.2f} V ≥ tomt {cfg.batt_empty_v} V"
                    if r.batt_v is not None else "över tomt"),
    group="battery"))
# motion/circuit: the level check below is the `level` mode; in `event` mode
# AlarmState replaces it with the pulse detectors (still gated by device_kinds)
register_rule(AlarmRule(
    AlarmKind.MOTION, "Rörelselarm",
    lambda cfg, r, obj: "rörelse upptäckt" if obj.get("motion_alert") else None,
    lambda cfg, r: "ingen rörelse", device_kinds=SENSOR))
register_rule(AlarmRule(
    AlarmKind.CIRCUIT, "Kretsalarm",
    lambda cfg, r, obj: "krets öppen" if obj.get("motion_switch") else None,
    lambda cfg, r: "krets sluten", device_kinds=SENSOR | {"door"}))
# door contact (canonical `door_open`, level: open -> alarm, closed -> clear)
register_rule(AlarmRule(
    AlarmKind.DOOR, "Dörrlarm",
    lambda cfg, r, obj: "dörren är öppen" if obj.get("door_open") else None,
    lambda cfg, r: "dörren är stängd", device_kinds=frozenset({"door"}),
    notify_fire="🚪 DÖRRLARM: {name} — {detail}",
    notify_clear="✔️ Dörr stängd: {name} — {detail}"))
register_rule(AlarmRule(
    AlarmKind.TILT, "Tiltlarm", None,
    lambda cfg, r: f"inom baslinje ± {cfg.tilt_sense_g} g", device_kinds=SENSOR))
register_rule(AlarmRule(AlarmKind.STALE, "Ingen kontakt (watchdog)"))


def evaluate(cfg: DeviceConfig, up: Uplink) -> tuple[Reading, list[Transition]]:
    o = up.obj
    _warn_unknown_kind(cfg)

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
    for rule in _RULES:
        if rule.check is None or not rule_applies(rule.kind, cfg):
            continue
        detail = rule.check(cfg, reading, o)
        if detail is not None:
            fired.append(Transition(rule.kind, True, detail))
    return reading, fired


def clear_detail(kind: AlarmKind, cfg: DeviceConfig, reading: Reading) -> str:
    """Clear text built from the *current* reading, not the stale fire detail."""
    rule = rule_for(kind)
    if rule is not None and rule.clear is not None:
        return rule.clear(cfg, reading)
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
    motion_events: list[datetime] = field(default_factory=list)     # event-mode motion: detections within the window
    motion_burst_active: bool = False                               # event mode, min>1: already alarmed for this burst

    def touch(self, up: Uplink) -> None:
        """Non-sensor frame (settings fPort 4-7, codec failure, ...): only refresh
        last_seen so the watchdog stays quiet; never evaluate or clear alarms."""
        self.last_seen = up.time

    def update(self, cfg: DeviceConfig, up: Uplink) -> tuple[Reading, list[Transition]]:
        reading, fired = evaluate(cfg, up)
        self.last_seen = up.time

        acc = up.obj.get("accelerometer")
        if rule_applies(AlarmKind.TILT, cfg) and isinstance(acc, dict) and all(isinstance(v, (int, float)) for v in acc.values()):
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

        transitions_pre: list[Transition] = []
        if cfg.circuit_mode == "event" and rule_applies(AlarmKind.CIRCUIT, cfg):
            fired = [t for t in fired if t.kind != AlarmKind.CIRCUIT]
            circuit = self._circuit_event(cfg, up)
            if circuit:
                transitions_pre.append(circuit)
        if cfg.motion_mode == "event":
            # motion_alert is a one-shot pulse (one frame per interrupt): report
            # every detection, never clear on other frames
            motion_fired = [t for t in fired if t.kind == AlarmKind.MOTION]
            fired = [t for t in fired if t.kind != AlarmKind.MOTION]
            if motion_fired:  # heartbeats without the bit are not detections
                motion = self._motion_event(cfg, up)
                if motion:
                    transitions_pre.append(motion)

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
            group = _group(kind)
            if group and any(_group(t.kind) == group for t in fired):
                self.last_detail.pop(kind, None)
                continue
            transitions.append(Transition(kind, False, clear_detail(kind, cfg, reading)))
            self.last_detail.pop(kind, None)
        self.active = now_active
        return reading, transitions

    def _motion_event(self, cfg: DeviceConfig, up: Uplink) -> Transition | None:
        """Event-mode motion: the firmware sends motion_alert=1 in one frame per
        accelerometer interrupt, so every frame with the bit is one detection.
        Detections are counted in a sliding motion_window_s window; with
        motion_min_detections=1 (default) each detection alarms immediately
        (transparent). Set e.g. motion_min_detections=3, motion_window_s=30 to
        alarm only when 3 detections fall within 30 s. Fire-only: no clear."""
        now = up.time if up.time.tzinfo else up.time.replace(tzinfo=timezone.utc)
        cutoff = now - timedelta(seconds=cfg.motion_window_s)
        self.motion_events = [t for t in self.motion_events
                              if (t if t.tzinfo else t.replace(tzinfo=timezone.utc)) > cutoff]
        self.motion_events.append(now)
        n = len(self.motion_events)
        window = f"{cfg.motion_window_s:.0f} s"
        if cfg.motion_min_detections <= 1:
            return Transition(AlarmKind.MOTION, True,
                              f"rörelse upptäckt — {n} detektering{'ar' if n != 1 else ''} inom {window}")
        if n < cfg.motion_min_detections:
            self.motion_burst_active = False  # burst left the window: the next one can alarm again
            return None
        if self.motion_burst_active:
            return None
        self.motion_burst_active = True
        return Transition(AlarmKind.MOTION, True,
                          f"rörelse upptäckt — {n} detekteringar inom {window} "
                          f"(gräns {cfg.motion_min_detections})")

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
            "motion_events": [t.isoformat() for t in self.motion_events],
            "motion_burst_active": self.motion_burst_active,
        }

    def restore(self, snap: dict[str, Any]) -> None:
        self.active = {_known_kind(v) for v in snap.get("active", [])}
        ls = snap.get("last_seen")
        self.last_seen = datetime.fromisoformat(ls) if ls else None
        self.accel_baseline = {k: float(v) for k, v in snap.get("accel_baseline", {}).items()}
        self.last_detail = {_known_kind(k): v for k, v in snap.get("last_detail", {}).items()}
        self.tilt_streak = int(snap.get("tilt_streak", 0))
        lc = snap.get("last_circuit_event")
        self.last_circuit_event = datetime.fromisoformat(lc) if lc else None
        self.motion_events = [datetime.fromisoformat(s) for s in snap.get("motion_events", [])]
        self.motion_burst_active = bool(snap.get("motion_burst_active", False))
