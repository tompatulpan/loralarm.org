"""Device profiles: everything the Python side knows about one device family.

A profile is looked up by the ChirpStack device-profile name (`family`). It
holds the wire-format details that used to be hardcoded as family-name sets in
downlink.py / service.py:

* `defaults`       — family config defaults (Tk offset, battery voltages, ...)
* `settings`       — downlink settings the device accepts (key, fPort, range,
                     and the codec `object` field the device reports it in)
* `freq_table`     — ODR wire value -> Hz, with `freq_label` for display
* `data_ports`     — fPorts that carry sensor data (default {2}; Dragino: {10})
* `field_map`      — codec field -> canonical field, for third-party codecs that
                     cannot be changed (Dragino `BAT_V` -> `battery_voltage`)
* `settings_kinds` — device kinds (`DeviceConfig.kind`) that take the
                     auto-downlink settings (a door contact does not)

Adding a device family = one `register_profile(DeviceProfile(...))` call; a
family without a registered profile gets GENERIC (all standard settings, wire
values passed through unchanged), which is what unknown families always did.

Codecs stay in ChirpStack and must emit the canonical field names documented
in README.md ("Canonical codec fields"); profiles describe only the downlink
side and the per-family defaults.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# standard accelerometer settings/command ports (3-digit ASCII decimal)
PORT_COMMAND = 4      # command byte (RAK4631: test alarm; RUI3 ignores it)
PORT_THRESHOLD = 5    # interrupt threshold 1-127
PORT_FREQUENCY = 6    # ODR: RAK4631 register code / RUI3 index
PORT_DURATION = 7     # interrupt duration 0-127


@dataclass(frozen=True)
class SettingSpec:
    """One downlink setting. `obj_field` is the codec object field in which the
    device reports the current wire value; None = one-shot only (no read-back,
    never auto-pushed from the config)."""
    key: str
    port: int
    lo: int = 0
    hi: int = 255
    obj_field: str | None = None
    kind: str = "int"          # int | frequency (frequency goes through the profile's table)


ACC_SETTINGS: tuple[SettingSpec, ...] = (
    SettingSpec("threshold", PORT_THRESHOLD, 1, 127, "acc_threshold"),
    SettingSpec("frequency", PORT_FREQUENCY, obj_field="acc_frequency", kind="frequency"),
    SettingSpec("duration", PORT_DURATION, 0, 127, "acc_duration"),
    SettingSpec("command", PORT_COMMAND, 0, 255),
)

# LIS3DH ODR: RAK4631 register code -> Hz (payload_codec_rak4631.js)
RAK4631_FREQ = {31: 1, 47: 10, 63: 25, 79: 50, 95: 100}
# LIS3DH ODR: RUI3 index -> Hz; 6/7 (200/400 Hz) exist only on RUI3
RUI3_FREQ = {1: 1, 2: 10, 3: 25, 4: 50, 5: 100, 6: 200, 7: 400}


def _int(s: str, what: str) -> int:
    try:
        return int(s, 10)
    except (TypeError, ValueError):
        raise ValueError(f"invalid {what} value: {s!r}") from None


@dataclass(frozen=True, eq=False)
class DeviceProfile:
    name: str
    aliases: tuple[str, ...] = ()
    defaults: dict[str, Any] = field(default_factory=dict)
    settings: tuple[SettingSpec, ...] = ACC_SETTINGS
    settings_kinds: frozenset[str] = frozenset({"sensor"})
    freq_table: dict[int, int] | None = None     # wire value -> Hz; None = unknown, pass through
    freq_label: str = "index"                    # "code" / "index": how a wire value is shown
    data_ports: frozenset[int] = frozenset({2})
    field_map: dict[str, str] = field(default_factory=dict)

    # -- uplinks -------------------------------------------------------------

    def is_data_port(self, f_port: int) -> bool:
        return f_port in self.data_ports

    def normalize(self, obj: dict[str, Any]) -> dict[str, Any]:
        """Codec object with the canonical field names added (originals kept)."""
        if not self.field_map:
            return obj
        out = dict(obj)
        for src, dst in self.field_map.items():
            if src in obj and dst not in obj:
                out[dst] = obj[src]
        return out

    # -- settings ------------------------------------------------------------

    def setting(self, key: str) -> SettingSpec | None:
        return next((s for s in self.settings if s.key == key), None)

    def setting_for_port(self, port: int) -> SettingSpec | None:
        return next((s for s in self.settings if s.port == port), None)

    def auto_settings(self) -> tuple[SettingSpec, ...]:
        """Settings the service can enforce: the device reports them back."""
        return tuple(s for s in self.settings if s.obj_field is not None)

    # -- frequency -----------------------------------------------------------

    def wire_to_hz(self, wire: int) -> int | None:
        if self.freq_table is not None:
            return self.freq_table.get(wire)
        for _, table in _tables():
            if wire in table:
                return table[wire]
        return None

    def frequency_to_wire(self, value: int | str) -> int:
        """Translate a frequency request to this profile's wire value.

        Accepted: Hz (`hz:50` or bare 1/10/25/50/100/200/400), a wire value of
        any registered table (`code:47`, `index:2`, or bare), where a bare
        number is tried as this profile's own wire value, then Hz, then the
        other tables. A profile without a table passes wire values through
        unchanged; Hz then needs a known family.
        """
        s = str(value).strip().lower()
        tables = dict(_tables())
        hz: int | None = None
        wire: int | None = None          # a wire value in `src_label`'s table
        src_label: str | None = None
        label, sep, rest = s.partition(":")
        if sep and label == "hz":
            hz = _int(rest, "hz")
        elif sep:
            if label not in tables:
                raise ValueError(f"unknown frequency prefix {label!r} "
                                 f"(hz: or {'/'.join(tables)}:)")
            wire, src_label = _int(rest, label), label
            if wire not in tables[label]:
                raise ValueError(f"unknown {label} {wire} (valid: {_fmt_keys(tables[label])})")
        else:
            v = _int(s, "frequency")
            hz_values = {h for _, t in _tables() for h in t.values()}
            if self.freq_table is not None and v in self.freq_table:
                wire, src_label = v, self.freq_label
            elif v in hz_values:
                hz = v
            else:
                found = next(((lb, t) for lb, t in _tables() if v in t), None)
                if found is None:
                    raise ValueError(f"unknown frequency {value!r} (Hz "
                                     f"{_fmt_keys({h: 0 for h in sorted(hz_values)})}, or a wire "
                                     f"value: {_wire_help(tables)})")
                wire, src_label = v, found[0]

        if self.freq_table is None:
            if wire is not None:
                return wire
            raise ValueError("frequency in Hz needs a known device family "
                             "(set `family` in the config or pass --family); "
                             "use code:31/47/63/79/95 or index:1-7 for an explicit wire value")
        if wire is not None:
            if src_label == self.freq_label:
                return wire
            hz = tables[src_label][wire]
        inverse = {h: w for w, h in self.freq_table.items()}
        if hz not in inverse:
            raise ValueError(f"{hz} Hz has no {self.name} {self.freq_label} "
                             f"(supported: {_fmt_keys({h: 0 for h in sorted(inverse)})} Hz)")
        return inverse[hz]


def _fmt_keys(d: dict) -> str:
    return "/".join(str(k) for k in d)


def _wire_help(tables: dict[str, dict[int, int]]) -> str:
    return ", ".join(f"{lb} {_fmt_keys(t)}" for lb, t in tables.items())


# -- registry ----------------------------------------------------------------

_PROFILES: list[DeviceProfile] = []


def _tables() -> list[tuple[str, dict[int, int]]]:
    """(label, wire->Hz) of every registered profile with a frequency table;
    the first profile registered under a label owns it."""
    seen: dict[str, dict[int, int]] = {}
    for p in _PROFILES:
        if p.freq_table is not None:
            seen.setdefault(p.freq_label, p.freq_table)
    return list(seen.items())


def register_profile(profile: DeviceProfile) -> DeviceProfile:
    """Add (or replace, by name) a profile."""
    unregister_profile(profile.name)
    _PROFILES.append(profile)
    return profile


def unregister_profile(name: str) -> None:
    _PROFILES[:] = [p for p in _PROFILES if p.name.lower() != name.lower()]


def profiles() -> list[DeviceProfile]:
    return list(_PROFILES)


GENERIC = DeviceProfile(name="")


def get_profile(family: str | None) -> DeviceProfile:
    """Profile for a ChirpStack device-profile name (case-insensitive, aliases
    included); GENERIC when the family is empty or unknown."""
    fam = (family or "").strip().lower()
    if fam:
        for p in _PROFILES:
            if fam == p.name.lower() or fam in p.aliases:
                return p
    return GENERIC


# Migration spec §4: RAK3172 = 3 V cell (0 °C Tk, alarm limit 0 °C),
# RAK4631 Android = LiPo (8 °C Tk, limit 2 °C).
register_profile(DeviceProfile(
    name="RAK3172",
    aliases=("rak3172", "rui3", "cayenne"),
    defaults=dict(tk_correction_c=0.0, temp_alarm_limit_c=0.0,
                  batt_full_v=3.1, batt_warn_v=2.9, batt_empty_v=2.8,
                  circuit_mode="level"),
    freq_table=RUI3_FREQ, freq_label="index"))
register_profile(DeviceProfile(
    name="RAK4631",
    aliases=("rak4631", "android", "custom"),
    defaults=dict(tk_correction_c=8.0, temp_alarm_limit_c=2.0,
                  batt_full_v=4.2, batt_warn_v=3.5, batt_empty_v=3.2,
                  circuit_mode="event"),
    freq_table=RAK4631_FREQ, freq_label="code"))
# Dragino LDS02 door switch (third-party codec, fPort 10): DOOR_OPEN_STATUS
# 1 = open / 0 = closed, sent on every change plus a periodic heartbeat. No
# temperature, no downlink settings. Battery is 2 x AAA (~3.0 V fresh), so the
# thresholds are looser than the RAK 3 V cell — tune per device in devices.json.
register_profile(DeviceProfile(
    name="Dragino Switch LDS02",
    aliases=("lds02",),
    defaults=dict(kind="door", batt_full_v=3.0, batt_warn_v=2.5, batt_empty_v=2.2),
    settings=(), settings_kinds=frozenset(),
    data_ports=frozenset({10}),
    field_map={"BAT_V": "battery_voltage", "DOOR_OPEN_STATUS": "door_open"}))
