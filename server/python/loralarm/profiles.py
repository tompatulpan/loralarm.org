"""Device profiles: everything the Python side knows about one device family.

A profile is looked up by the ChirpStack device-profile name (`family`). It
holds the wire-format details that used to be hardcoded as family-name sets in
downlink.py / service.py:

* `defaults`       — family config defaults (Tk offset, battery voltages, ...)
* `settings`       — downlink settings the device accepts (key, fPort, range,
                     and the codec `object` field the device reports it in)
* `freq_table`     — ODR wire value -> Hz, with `freq_label` for display
* `data_ports`     — fPorts that carry sensor data (default {2}; Dragino: {10})
* `fields`         — measurement declarations: how the codec object becomes named,
                     calibrated values (rename/derive; see frame.py). A third-party
                     codec (Dragino `BAT_V`) is mapped here, never edited
* `settings_kinds` — device kinds (`DeviceConfig.kind`) that take the
                     auto-downlink settings (a door contact does not)

The built-in families are data, not code: one JSON file each in
`loralarm/families/`, and `devices.json` can define or override more under
`"profiles"` (see definitions.py for the schema). `Registry.register_profile()`
(registry.py) remains for code-defined profiles. A family without a profile gets
GENERIC (all standard settings and fields, wire values passed through unchanged).

Codecs stay in ChirpStack; a codec's field names are the profile's business
(`fields`), so any codec works as long as its type maps what the alarm rules read.
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

def _int(s: str, what: str) -> int:
    try:
        return int(s, 10)
    except (TypeError, ValueError):
        raise ValueError(f"invalid {what} value: {s!r}") from None


# Fields every device type gets unless it declares its own of the same name:
# calibrated temperature and battery level/status from `battery_voltage` (the
# mechanics are in frame.py). They only appear when the codec reports the
# source, so a door switch without a temperature simply has no `temperature`.
STANDARD_FIELDS: dict[str, dict[str, Any]] = {
    "temperature": {"from": "temperature", "unit": "°C", "type": "number",
                    "transform": [{"op": "subtract", "by": "$tk_correction_c"},
                                  {"op": "round", "n": 2}]},
    "battery_voltage": {"from": "battery_voltage", "unit": "V", "type": "number"},
    "battery_pct": {"derive": "linear_pct", "of": "battery_voltage", "unit": "%",
                    "empty": "$batt_empty_v", "full": "$batt_full_v", "round": 1},
    "battery_status": {"derive": "band", "of": "battery_voltage",
                       "bands": [{"gt": "$batt_warn_v", "as": "ok"},
                                 {"ge": "$batt_empty_v", "as": "low"}],
                       "else": "empty", "missing": "unknown"},
}


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
    # measurement declarations (see frame.py): codec object -> named values
    fields: dict[str, dict[str, Any]] = field(default_factory=lambda: dict(STANDARD_FIELDS))

    # -- uplinks -------------------------------------------------------------

    def is_data_port(self, f_port: int) -> bool:
        return f_port in self.data_ports

    # -- settings ------------------------------------------------------------

    def setting(self, key: str) -> SettingSpec | None:
        return next((s for s in self.settings if s.key == key), None)

    def setting_for_port(self, port: int) -> SettingSpec | None:
        return next((s for s in self.settings if s.port == port), None)

    def auto_settings(self) -> tuple[SettingSpec, ...]:
        """Settings the service can enforce: the device reports them back."""
        return tuple(s for s in self.settings if s.obj_field is not None)

    # -- frequency -----------------------------------------------------------

    def wire_to_hz(self, wire: int, tables: list[tuple[str, dict[int, int]]] | None = None) -> int | None:
        """Hz for a wire value: this profile's own table, else the first of the
        registry's `tables` (`Registry.freq_tables()`) that knows it."""
        if self.freq_table is not None:
            return self.freq_table.get(wire)
        for _, table in tables or ():
            if wire in table:
                return table[wire]
        return None

    def frequency_to_wire(self, value: int | str,
                          tables: list[tuple[str, dict[int, int]]] | None = None) -> int:
        """Translate a frequency request to this profile's wire value.

        `tables` is `Registry.freq_tables()`: the wire -> Hz tables of every
        registered profile, which is what lets a RAK4631 register code or a RUI3
        index be accepted for the other family.

        Accepted: Hz (`hz:50` or bare 1/10/25/50/100/200/400), a wire value of
        any registered table (`code:47`, `index:2`, or bare), where a bare
        number is tried as this profile's own wire value, then Hz, then the
        other tables. A profile without a table passes wire values through
        unchanged; Hz then needs a known family.
        """
        pairs = list(tables or ())
        by_label = dict(pairs)
        s = str(value).strip().lower()
        hz: int | None = None
        wire: int | None = None          # a wire value in `src_label`'s table
        src_label: str | None = None
        label, sep, rest = s.partition(":")
        if sep and label == "hz":
            hz = _int(rest, "hz")
        elif sep:
            if label not in by_label:
                raise ValueError(f"unknown frequency prefix {label!r} "
                                 f"(hz: or {'/'.join(by_label)}:)")
            wire, src_label = _int(rest, label), label
            if wire not in by_label[label]:
                raise ValueError(f"unknown {label} {wire} (valid: {_fmt_keys(by_label[label])})")
        else:
            v = _int(s, "frequency")
            hz_values = {h for _, t in pairs for h in t.values()}
            if self.freq_table is not None and v in self.freq_table:
                wire, src_label = v, self.freq_label
            elif v in hz_values:
                hz = v
            else:
                found = next(((lb, t) for lb, t in pairs if v in t), None)
                if found is None:
                    raise ValueError(f"unknown frequency {value!r} (Hz "
                                     f"{_fmt_keys({h: 0 for h in sorted(hz_values)})}, or a wire "
                                     f"value: {_wire_help(by_label)})")
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
            hz = by_label[src_label][wire]
        inverse = {h: w for w, h in self.freq_table.items()}
        if hz not in inverse:
            raise ValueError(f"{hz} Hz has no {self.name} {self.freq_label} "
                             f"(supported: {_fmt_keys({h: 0 for h in sorted(inverse)})} Hz)")
        return inverse[hz]


def _fmt_keys(d: dict) -> str:
    return "/".join(str(k) for k in d)


def _wire_help(tables: dict[str, dict[int, int]]) -> str:
    return ", ".join(f"{lb} {_fmt_keys(t)}" for lb, t in tables.items())


# the fallback for a family without a definition: all standard settings, wire
# values passed through unchanged (lookup lives in registry.Registry.get_profile)
GENERIC = DeviceProfile(name="")
