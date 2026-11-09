"""Tests for config layering, env-var secret expansion and validation warnings."""
from __future__ import annotations

import os
import unittest
from unittest import mock

from loralarm.config import Config

from .common import DEV_EUI, OTHER_EUI, make_config


class LayeringTest(unittest.TestCase):
    def test_defaults_family_device_precedence(self):
        cfg = make_config(
            devices={DEV_EUI: {"name": "sensor3", "ntfy_topic": "sensor3"}},
            defaults={"ntfy_topic": "loralarm", "watchdog_hours": 24},
            families={"RAK4631": {"batt_full_v": 4.2, "ntfy_topic": "family-topic"}},
        )
        dc = cfg.for_event(DEV_EUI, "chirp-name", "RAK4631")
        self.assertEqual(dc.ntfy_topic, "sensor3")      # device wins
        self.assertEqual(dc.batt_full_v, 4.2)           # family wins over defaults
        self.assertEqual(dc.watchdog_hours, 24)         # defaults
        self.assertEqual(dc.name, "sensor3")            # config name wins over ChirpStack name

    def test_name_falls_back_to_chirpstack_device_name(self):
        cfg = make_config(devices={DEV_EUI: {}})
        dc = cfg.for_event(DEV_EUI, "sensor3-from-chirpstack", "RAK4631")
        self.assertEqual(dc.name, "sensor3-from-chirpstack")

    def test_device_keys_are_lowercased(self):
        cfg = Config({"devices": {DEV_EUI.upper(): {"name": "sensor3", "ntfy_topic": "abc"}}})
        dc = cfg.for_event(DEV_EUI, "", "")
        self.assertEqual(dc.ntfy_topic, "abc")

    def test_family_defaults_builtin(self):
        cfg = make_config()
        dc = cfg.for_event(OTHER_EUI, "sensor16", "RAK3172")
        self.assertEqual(dc.batt_full_v, 3.1)  # built-in FAMILY_DEFAULTS, not from JSON

    def test_unknown_key_warns(self):
        with self.assertLogs("loralarm.config", level="WARNING") as cm:
            Config({"defaults": {"ntfy_topc": "typo"}})
        self.assertTrue(any("ntfy_topc" in line for line in cm.output))

    def test_wrong_type_warns(self):
        with self.assertLogs("loralarm.config", level="WARNING") as cm:
            cfg = make_config(devices={DEV_EUI: {"watchdog_hours": "a day"}})
            cfg.for_event(DEV_EUI, "", "")
        self.assertTrue(any("watchdog_hours" in line for line in cm.output))

    def test_wrong_type_falls_back_to_lower_layer(self):
        cfg = make_config(devices={DEV_EUI: {"temp_alarm_limit_c": "cold", "watchdog_hours": True}},
                          families={"RAK4631": {"temp_alarm_limit_c": 2.0}})
        with self.assertLogs("loralarm.config", level="WARNING"):
            dc = cfg.for_event(DEV_EUI, "", "RAK4631")
        self.assertEqual(dc.temp_alarm_limit_c, 2.0)   # family value, not the bad string
        self.assertEqual(dc.watchdog_hours, 24.0)      # bool is not a number


class EnvExpansionTest(unittest.TestCase):
    def test_env_var_expanded(self):
        with mock.patch.dict(os.environ, {"LORALARM_TEST_TOKEN": "s3cret"}):
            cfg = Config({"devices": {DEV_EUI: {"pushover_token": "${LORALARM_TEST_TOKEN}"}}})
            dc = cfg.for_event(DEV_EUI, "", "")
        self.assertEqual(dc.pushover_token, "s3cret")

    def test_unset_env_var_warns_and_expands_empty(self):
        os.environ.pop("LORALARM_UNSET_VAR", None)
        with self.assertLogs("loralarm.config", level="WARNING"):
            cfg = Config({"devices": {DEV_EUI: {"pushover_token": "${LORALARM_UNSET_VAR}"}}})
            dc = cfg.for_event(DEV_EUI, "", "")
        self.assertEqual(dc.pushover_token, "")


if __name__ == "__main__":
    unittest.main()
