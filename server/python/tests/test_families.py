"""Contract every built-in family file must meet. Runs over `families/*.json`, so a
new device needs no change here: a broken or colliding definition fails at once."""
from __future__ import annotations

import json
import unittest

from loralarm.definitions import FAMILIES_DIR, builtin_registry, register_definition
from loralarm.profiles import GENERIC, STANDARD_FIELDS

from .common import REG

FILES = sorted(FAMILIES_DIR.glob("*.json"))


def load(path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class FamilyContractTest(unittest.TestCase):
    def test_families_exist(self):
        self.assertTrue(FILES)

    def test_each_family_is_documented_and_resolvable(self):
        for path in FILES:
            d = load(path)
            with self.subTest(family=path.name):
                self.assertTrue(d.get("description"), "a family needs a 'description'")
                profile = REG.get_profile(d["name"])
                self.assertEqual(profile.name, d["name"])
                for alias in d.get("aliases", []):
                    self.assertEqual(REG.get_profile(alias), profile, f"alias {alias!r}")

    def test_rules_can_actually_run_for_the_family(self):
        """A rule only runs for its `device_kinds`: the family's default kind must be
        one of them, and the rule must read a declared (or standard) field."""
        for path in FILES:
            d = load(path)
            kind = d.get("defaults", {}).get("kind", "sensor")
            declared = set(d.get("fields", {})) | set(STANDARD_FIELDS)
            for rule in d.get("rules", []):
                with self.subTest(family=path.name, rule=rule["kind"]):
                    if rule.get("device_kinds") is not None:
                        self.assertIn(kind, rule["device_kinds"],
                                      "default device kind is not in the rule's device_kinds")
                    self.assertIn(rule["field"], declared, "rule reads an undeclared field")

    def test_alarm_kinds_are_not_shared_between_families(self):
        owners: dict[str, str] = {}
        for path in FILES:
            for rule in load(path).get("rules", []):
                other = owners.setdefault(rule["kind"], path.name)
                self.assertEqual(other, path.name,
                                 f"alarm kind {rule['kind']!r} defined by both {other} and {path.name}")


class RuleKindCollisionTest(unittest.TestCase):
    RULE = {"kind": "door", "label": "X", "field": "f", "when": "truthy"}

    def test_a_second_family_cannot_take_over_an_alarm_kind(self):
        reg = builtin_registry().fork()
        with self.assertRaises(ValueError) as cm:
            register_definition(reg, "Other Door", {"rules": [self.RULE]})
        self.assertIn("Dragino Switch LDS02", str(cm.exception))
        self.assertEqual(reg.rule_for("door").family, "Dragino Switch LDS02")   # unchanged
        self.assertIs(reg.get_profile("Other Door"), GENERIC)       # nothing was registered

    def test_a_family_may_redefine_its_own_rules(self):
        reg = builtin_registry().fork()
        register_definition(reg, "dragino switch lds02", {"rules": [{**self.RULE, "label": "Mine"}]})
        self.assertEqual(reg.rule_for("door").label, "Mine")


if __name__ == "__main__":
    unittest.main()
