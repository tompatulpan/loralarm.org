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
from .frame import Frame, build_values
from .registry import Registry

log = logging.getLogger(__name__)


class AlarmKind(str):
    """An alarm kind is just its string key: the built-ins below, or any new
    one created by `register_rule` (`AlarmKind("leak")`). Compare with `==`."""

    __slots__ = ()

    TEMP_LOW: ClassVar["AlarmKind"]     # temperature at or below temp_alarm_min_c
    TEMP_HIGH: ClassVar["AlarmKind"]    # temperature at or above temp_alarm_max_c
    BATTERY_LOW: ClassVar["AlarmKind"]
    BATTERY_EMPTY: ClassVar["AlarmKind"]
    MOTION: ClassVar["AlarmKind"]       # motion_alert (accelerometer interrupt) -> rörelselarm
    CIRCUIT: ClassVar["AlarmKind"]      # motion_switch / door contact -> kretsalarm
    TILT: ClassVar["AlarmKind"]
    STALE: ClassVar["AlarmKind"]        # watchdog: no uplink within watchdog_hours

    @property
    def value(self) -> str:
        return str(self)


AlarmKind.TEMP_LOW = AlarmKind("temp_low")
AlarmKind.TEMP_HIGH = AlarmKind("temp_high")
AlarmKind.BATTERY_LOW = AlarmKind("battery_low")
AlarmKind.BATTERY_EMPTY = AlarmKind("battery_empty")
AlarmKind.MOTION = AlarmKind("motion")
AlarmKind.CIRCUIT = AlarmKind("circuit")
AlarmKind.TILT = AlarmKind("tilt")
AlarmKind.STALE = AlarmKind("stale")

# alarm kinds renamed over time; old persisted snapshots / configs may still use them
LEGACY_KINDS = {"temp": "temp_low"}

# after this many consecutive tilted samples the baseline is re-accepted, so a
# sensor that is permanently moved does not stay alarmed forever
TILT_REBASELINE_AFTER = 10


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

    check(cfg, frame, values) returns the fire detail while the condition holds,
    else None (`values` is `frame.values`); None as `check` itself marks a
    stateful detector that AlarmState runs (tilt, watchdog) and that only
    contributes label/texts/group.
    clear(cfg, frame) builds the clear text from the *current* frame.
    `device_kinds` limits the rule to those `DeviceConfig.kind` values
    (None = every kind). `group` ties severities of one quantity together:
    moving within a group is an escalation, not a clear. Rule-specific
    thresholds belong in `cfg.params` ("params" in devices.json).
    """
    kind: AlarmKind
    label: str
    check: Callable[[DeviceConfig, Frame, dict], str | None] | None = None
    clear: Callable[[DeviceConfig, Frame], str] | None = None
    device_kinds: frozenset[str] | None = None
    group: str | None = None
    notify_fire: str = ""       # fallback texts for kinds notify.DEFAULT_TEXT lacks;
    notify_clear: str = ""      # {name} {detail} {label}; empty -> generic text
    # False = a "health" alarm (temperature, battery, no contact): it always
    # notifies, whatever the device's alarm_window says
    windowed: bool = True
    # latching rule (button): once fired it stays active across frames that say
    # nothing about it, and clears only when release(cfg, frame, values) is True
    release: Callable[[DeviceConfig, Frame, dict], bool] | None = None
    # name of the device family whose definition declared the rule ("" = built-in
    # core rule); register_definition refuses two families sharing an alarm kind
    family: str = ""


def known_device_kinds(registry: Registry) -> set[str]:
    kinds = set(SENSOR)
    for r in registry.rules():
        kinds |= r.device_kinds or set()
    return kinds


def rule_applies(registry: Registry, kind: str, cfg: DeviceConfig) -> bool:
    """True if the alarm kind is evaluated for this device's `kind`."""
    r = registry.rule_for(kind)
    return r is None or r.device_kinds is None or cfg.kind in r.device_kinds


def _warn_unknown_kind(registry: Registry, cfg: DeviceConfig) -> None:
    known = known_device_kinds(registry)
    if cfg.kind not in known and cfg.kind not in registry.warned_kinds:
        registry.warned_kinds.add(cfg.kind)
        log.warning("device %s: unknown kind %r (known: %s); only rules that apply to "
                    "every kind will run", cfg.dev_eui, cfg.kind, sorted(known))


def _known_kind(registry: Registry, value: str) -> AlarmKind:
    """AlarmKind for a persisted key; ValueError if no rule is registered for it
    (corrupt snapshot, or a rule that has since been removed)."""
    value = LEGACY_KINDS.get(value, value)
    if registry.rule_for(value) is None:
        raise ValueError(f"unknown alarm kind {value!r}")
    return AlarmKind(value)


def _group(registry: Registry, kind: AlarmKind) -> str | None:
    r = registry.rule_for(kind)
    return r.group if r else None


def _temp_low_check(cfg: DeviceConfig, frame: Frame, obj: dict) -> str | None:
    temp = frame.values.get("temperature")
    if temp is not None and temp <= cfg.temp_alarm_min_c:
        return f"{temp} °C ≤ nedre gräns {cfg.temp_alarm_min_c} °C"
    return None


def _temp_low_release(cfg: DeviceConfig, frame: Frame, obj: dict) -> bool:
    """Hysteresis: once low, the alarm holds until the temperature is above
    the limit by temp_alarm_hysteresis_c. A frame without temperature clears it."""
    temp = frame.values.get("temperature")
    return temp is None or temp > cfg.temp_alarm_min_c + cfg.temp_alarm_hysteresis_c


def _temp_high_check(cfg: DeviceConfig, frame: Frame, obj: dict) -> str | None:
    temp = frame.values.get("temperature")
    if temp is not None and cfg.temp_alarm_max_c is not None and temp >= cfg.temp_alarm_max_c:
        return f"{temp} °C ≥ övre gräns {cfg.temp_alarm_max_c} °C"
    return None


def _temp_high_release(cfg: DeviceConfig, frame: Frame, obj: dict) -> bool:
    temp = frame.values.get("temperature")
    if temp is None or cfg.temp_alarm_max_c is None:
        return True
    return temp < cfg.temp_alarm_max_c - cfg.temp_alarm_hysteresis_c


def _pct_txt(frame: Frame) -> str:
    pct = frame.values.get("battery_pct")
    return f"{pct} %" if pct is not None else "–"


def _batt_low_check(cfg: DeviceConfig, frame: Frame, obj: dict) -> str | None:
    if frame.values.get("battery_status") == "low":
        return (f"{frame.values['battery_voltage']:.2f} V ({_pct_txt(frame)}) "
                f"≤ varningsgräns {cfg.batt_warn_v} V")
    return None


def _batt_empty_check(cfg: DeviceConfig, frame: Frame, obj: dict) -> str | None:
    if frame.values.get("battery_status") == "empty":
        return (f"{frame.values['battery_voltage']:.2f} V ({_pct_txt(frame)}) "
                f"< tomt {cfg.batt_empty_v} V")
    return None


def _clear_temp_low(cfg: DeviceConfig, frame: Frame) -> str:
    temp = frame.values.get("temperature")
    limit = cfg.temp_alarm_min_c + cfg.temp_alarm_hysteresis_c
    return f"{temp} °C > nedre gräns {round(limit, 2)} °C" if temp is not None else "inom gräns"


def _clear_temp_high(cfg: DeviceConfig, frame: Frame) -> str:
    temp = frame.values.get("temperature")
    if temp is None or cfg.temp_alarm_max_c is None:
        return "inom gräns"
    limit = cfg.temp_alarm_max_c - cfg.temp_alarm_hysteresis_c
    return f"{temp} °C < övre gräns {round(limit, 2)} °C"


def _clear_batt_low(cfg: DeviceConfig, frame: Frame) -> str:
    v = frame.values.get("battery_voltage")
    return f"{v:.2f} V > varningsgräns {cfg.batt_warn_v} V" if v is not None else "över varningsgräns"


def _clear_batt_empty(cfg: DeviceConfig, frame: Frame) -> str:
    v = frame.values.get("battery_voltage")
    return f"{v:.2f} V ≥ tomt {cfg.batt_empty_v} V" if v is not None else "över tomt"


BUILTIN_RULES: list[AlarmRule] = [
    AlarmRule(AlarmKind.TEMP_LOW, "Lågtemperaturalarm", _temp_low_check, _clear_temp_low,
              windowed=False, release=_temp_low_release),
    AlarmRule(AlarmKind.TEMP_HIGH, "Högtemperaturalarm", _temp_high_check, _clear_temp_high,
              windowed=False, release=_temp_high_release),
    AlarmRule(AlarmKind.BATTERY_LOW, "Batterinivå låg", _batt_low_check, _clear_batt_low,
              group="battery", windowed=False),
    AlarmRule(AlarmKind.BATTERY_EMPTY, "Batteri tomt", _batt_empty_check, _clear_batt_empty,
              group="battery", windowed=False),
    # motion/circuit: the level check below is the `level` mode; in `event` mode
    # AlarmState replaces it with the pulse detectors (still gated by device_kinds)
    AlarmRule(
        AlarmKind.MOTION, "Rörelselarm",
        lambda cfg, f, values: "rörelse upptäckt" if values.get("motion_alert") else None,
        lambda cfg, f: "ingen rörelse", device_kinds=SENSOR),
    AlarmRule(
        AlarmKind.CIRCUIT, "Kretsalarm",
        lambda cfg, f, values: "krets öppen" if values.get("motion_switch") else None,
        lambda cfg, f: "krets sluten", device_kinds=SENSOR | {"door"}),
    AlarmRule(
        AlarmKind.TILT, "Tiltlarm", None,
        lambda cfg, f: f"inom baslinje ± {cfg.tilt_sense_g} g", device_kinds=SENSOR),
    AlarmRule(AlarmKind.STALE, "Ingen kontakt (watchdog)", windowed=False),
]


def register_builtin_rules(registry: Registry) -> None:
    for rule in BUILTIN_RULES:
        registry.register_rule(rule)


def evaluate(cfg: DeviceConfig, up: Uplink, registry: Registry) -> tuple[Frame, list[Transition]]:
    _warn_unknown_kind(registry, cfg)
    profile = registry.get_profile(cfg.family)
    frame = Frame(values=build_values(profile.fields, up.obj, cfg, registry),
                  rssi=up.rssi, snr=up.snr)

    fired: list[Transition] = []
    for rule in registry.rules():
        if rule.check is None or not rule_applies(registry, rule.kind, cfg):
            continue
        detail = rule.check(cfg, frame, frame.values)
        if detail is not None:
            fired.append(Transition(rule.kind, True, detail))
    return frame, fired


def clear_detail(kind: AlarmKind, cfg: DeviceConfig, frame: Frame, registry: Registry) -> str:
    """Clear text built from the *current* frame, not the stale fire detail."""
    rule = registry.rule_for(kind)
    if rule is not None and rule.clear is not None:
        return rule.clear(cfg, frame)
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
    last_values: dict[str, Any] = field(default_factory=dict)       # measurements of the last evaluated frame
    # active alarms whose fire was NOT notified (outside the alarm window), so their
    # clear is not notified either; unknown = notify (fail safe, also for old snapshots)
    suppressed: set[AlarmKind] = field(default_factory=set)
    last_rssi: int | None = None
    last_snr: float | None = None

    def touch(self, up: Uplink) -> None:
        """Non-sensor frame (settings fPort 4-7, codec failure, ...): only refresh
        last_seen so the watchdog stays quiet; never evaluate or clear alarms."""
        self.last_seen = up.time

    def update(self, cfg: DeviceConfig, up: Uplink, registry: Registry) -> tuple[Frame, list[Transition]]:
        frame, fired = evaluate(cfg, up, registry)
        self.last_seen = up.time
        self.last_values = dict(frame.values)
        self.last_rssi, self.last_snr = frame.rssi, frame.snr

        acc = frame.values.get("accelerometer")
        if rule_applies(registry, AlarmKind.TILT, cfg) and isinstance(acc, dict) and all(isinstance(v, (int, float)) for v in acc.values()):
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
        if cfg.circuit_mode == "event" and rule_applies(registry, AlarmKind.CIRCUIT, cfg):
            fired = [t for t in fired if t.kind != AlarmKind.CIRCUIT]
            circuit = self._circuit_event(cfg, up, frame)
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
        for kind in self.active - now_active:
            rule = registry.rule_for(kind)
            if (rule is not None and rule.release is not None
                    and rule_applies(registry, kind, cfg)
                    and not rule.release(cfg, frame, frame.values)):
                now_active.add(kind)
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
            group = _group(registry, kind)
            if group and any(_group(registry, t.kind) == group for t in fired):
                self.last_detail.pop(kind, None)
                continue
            transitions.append(Transition(kind, False, clear_detail(kind, cfg, frame, registry)))
            self.last_detail.pop(kind, None)
        self.active = now_active
        return frame, transitions

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

    def _circuit_event(self, cfg: DeviceConfig, up: Uplink, frame: Frame) -> Transition | None:
        """Event-mode circuit (RAK4631): the firmware sends bit 0 in one uplink when
        the contact opens and nothing when it closes, so a frame without the bit
        says nothing about the circuit. Each pulse is a fire-only alarm; pulses
        within circuit_hold_s (retransmits, contact bounce) count as one."""
        if not frame.values.get("motion_switch"):
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
            "last_values": self.last_values,
            "suppressed": sorted(k.value for k in self.suppressed),
            "last_rssi": self.last_rssi,
            "last_snr": self.last_snr,
        }

    def restore(self, snap: dict[str, Any], registry: Registry) -> None:
        self.active = {_known_kind(registry, v) for v in snap.get("active", [])}
        ls = snap.get("last_seen")
        self.last_seen = datetime.fromisoformat(ls) if ls else None
        self.accel_baseline = {k: float(v) for k, v in snap.get("accel_baseline", {}).items()}
        self.last_detail = {_known_kind(registry, k): v for k, v in snap.get("last_detail", {}).items()}
        self.tilt_streak = int(snap.get("tilt_streak", 0))
        lc = snap.get("last_circuit_event")
        self.last_circuit_event = datetime.fromisoformat(lc) if lc else None
        self.motion_events = [datetime.fromisoformat(s) for s in snap.get("motion_events", [])]
        self.motion_burst_active = bool(snap.get("motion_burst_active", False))
        values = snap.get("last_values", {})
        self.last_values = dict(values) if isinstance(values, dict) else {}
        self.suppressed = {_known_kind(registry, v) for v in snap.get("suppressed", [])}
        self.last_rssi = snap.get("last_rssi")
        self.last_snr = snap.get("last_snr")
