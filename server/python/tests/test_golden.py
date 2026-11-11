"""Parity guard: the operator-visible output (console + notifications + alarm
state) of the golden scenarios must not change. See tests/golden.py."""
from __future__ import annotations

import json
import unittest

from .golden import GOLDEN, run_all


class GoldenTest(unittest.TestCase):
    def test_golden_file_is_present(self):
        self.assertTrue(GOLDEN.exists(), "run: python3 -m tests.golden --update")

    def test_scenarios_match_golden(self):
        expected = json.loads(GOLDEN.read_text(encoding="utf-8"))
        actual = json.loads(json.dumps(run_all()))
        self.assertEqual(set(actual), set(expected))
        for name in sorted(expected):
            with self.subTest(scenario=name):
                self.assertEqual(actual[name]["notifications"], expected[name]["notifications"])
                self.assertEqual(actual[name]["state"], expected[name]["state"])
                self.assertEqual(actual[name]["console"], expected[name]["console"])


if __name__ == "__main__":
    unittest.main()
