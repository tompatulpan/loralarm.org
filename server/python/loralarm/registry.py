"""The registry: everything pluggable the service knows about, as an *instance*.

Alarm rules, device profiles (and, from phase 2 on, detectors, transforms and
channels) used to be module-level globals that tests and hot reload had to undo
by hand. A `Registry` is now built once from the built-ins
(`definitions.builtin_registry()`), and every `Config` works on its own `fork()`
with the `"profiles"` section of devices.json applied on top — a reload simply
creates a new fork, so there is nothing to undo.

The registry only stores and looks things up; it does not know what a rule or a
profile does (it relies on `.kind`/`.label` and `.name`/`.aliases`).
"""
from __future__ import annotations

from typing import Any, Callable

from .profiles import GENERIC, DeviceProfile


class Registry:
    def __init__(self) -> None:
        self._rules: list[Any] = []
        self._profiles: list[DeviceProfile] = []
        self._labels: dict[str, str] = {}
        self._transforms: dict[str, Callable[..., Any]] = {}
        self._derivations: dict[str, Callable[..., Any]] = {}
        # device kinds / fields already warned about (log once per registry)
        self.warned_kinds: set[str] = set()
        self.warned_fields: set[tuple[str, str]] = set()

    def fork(self) -> "Registry":
        """Independent copy: registering into the fork never touches the original."""
        other = Registry()
        other._rules = list(self._rules)
        other._profiles = list(self._profiles)
        other._labels = dict(self._labels)
        other._transforms = dict(self._transforms)
        other._derivations = dict(self._derivations)
        return other

    # -- field transforms and derivations (see frame.py) ----------------------

    def register_transform(self, name: str, fn: Callable[..., Any]) -> None:
        self._transforms[name] = fn

    def transform(self, name: str) -> Callable[..., Any]:
        try:
            return self._transforms[name]
        except KeyError:
            raise ValueError(f"unknown transform {name!r} (known: {sorted(self._transforms)})") from None

    def register_derivation(self, name: str, fn: Callable[..., Any]) -> None:
        self._derivations[name] = fn

    def derivation(self, name: str) -> Callable[..., Any]:
        try:
            return self._derivations[name]
        except KeyError:
            raise ValueError(f"unknown derivation {name!r} (known: {sorted(self._derivations)})") from None

    # -- alarm rules -----------------------------------------------------------

    def register_rule(self, rule: Any) -> Any:
        """Add (or replace, by kind) an alarm rule."""
        self.unregister_rule(rule.kind)
        self._rules.append(rule)
        self._labels[str(rule.kind)] = rule.label
        return rule

    def unregister_rule(self, kind: str) -> None:
        self._rules[:] = [r for r in self._rules if r.kind != kind]
        self._labels.pop(str(kind), None)

    def rules(self) -> list[Any]:
        return list(self._rules)

    def rule_for(self, kind: str) -> Any | None:
        return next((r for r in self._rules if r.kind == kind), None)

    def label_for(self, kind: str) -> str:
        """User-facing Swedish label of an alarm kind (the key itself if unknown)."""
        return self._labels.get(str(kind), str(kind))

    # -- device profiles -------------------------------------------------------

    def register_profile(self, profile: DeviceProfile) -> DeviceProfile:
        """Add (or replace, by name) a profile."""
        self.unregister_profile(profile.name)
        self._profiles.append(profile)
        return profile

    def unregister_profile(self, name: str) -> None:
        self._profiles[:] = [p for p in self._profiles if p.name.lower() != name.lower()]

    def profiles(self) -> list[DeviceProfile]:
        return list(self._profiles)

    def get_profile(self, family: str | None) -> DeviceProfile:
        """Profile for a ChirpStack device-profile name (case-insensitive, aliases
        included); GENERIC when the family is empty or unknown."""
        fam = (family or "").strip().lower()
        if fam:
            for p in self._profiles:
                if fam == p.name.lower() or fam in p.aliases:
                    return p
        return GENERIC

    def freq_tables(self) -> list[tuple[str, dict[int, int]]]:
        """(label, wire -> Hz) of every profile with a frequency table; the first
        profile registered under a label owns it."""
        seen: dict[str, dict[int, int]] = {}
        for p in self._profiles:
            if p.freq_table is not None:
                seen.setdefault(p.freq_label, p.freq_table)
        return list(seen.items())
