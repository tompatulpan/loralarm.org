"""Frame/values: declared fields, transforms, derivations and the plugin point
(a new calibration is a registered function, a new sensor type is JSON)."""
from __future__ import annotations

import json
import unittest

from loralarm.config import Config
from loralarm.definitions import builtin_registry
from loralarm.frame import Frame, build_values, param_resolver
from loralarm.service import Engine

from .common import REG, AlarmState, evaluate, make_cfg, make_uplink, sensor_obj
from .test_engine import RecordingNotifier


def values(fields: dict, raw: dict, cfg=None, registry=REG) -> dict:
    return build_values(fields, raw, cfg or make_cfg(), registry)


class ParamResolverTest(unittest.TestCase):
    def test_config_attribute_params_entry_and_literal(self):
        cfg = make_cfg(params={"scale": 2.5})
        resolve = param_resolver(cfg)
        self.assertEqual(resolve("$tk_correction_c"), 8.0)
        self.assertEqual(resolve("$scale"), 2.5)
        self.assertEqual(resolve(7), 7)
        self.assertEqual(resolve("plain"), "plain")

    def test_unknown_param_is_an_error(self):
        with self.assertRaises(ValueError):
            param_resolver(make_cfg())("$nope")


class TransformTest(unittest.TestCase):
    def test_from_rename_and_passthrough_of_undeclared_fields(self):
        v = values({"door_open": {"from": "DOOR_OPEN_STATUS"}}, {"DOOR_OPEN_STATUS": 1, "MOD": 1})
        self.assertEqual(v, {"DOOR_OPEN_STATUS": 1, "MOD": 1, "door_open": 1})

    def test_declared_field_replaces_the_raw_one_of_the_same_name(self):
        f = {"temperature": {"from": "temperature", "transform": [{"op": "subtract", "by": "$tk_correction_c"}]}}
        self.assertEqual(values(f, {"temperature": 32.0})["temperature"], 24.0)

    def test_chain_scale_round_clamp(self):
        f = {"level": {"from": "raw", "transform": [{"op": "multiply", "by": 0.1},
                                                    {"op": "round", "n": 1},
                                                    {"op": "clamp", "min": 0, "max": 100}]}}
        self.assertEqual(values(f, {"raw": 1234})["level"], 100)
        self.assertEqual(values(f, {"raw": 456})["level"], 45.6)
        self.assertEqual(values(f, {"raw": -50})["level"], 0)

    def test_invalid_source_removes_the_field_instead_of_zero(self):
        f = {"t": {"from": "t", "type": "number"}}
        self.assertNotIn("t", values(f, {"t": "n/a"}))
        self.assertNotIn("t", values(f, {"t": None}))
        self.assertNotIn("t", values(f, {"t": True}))          # a bool is not a measurement
        self.assertEqual(values(f, {}), {})

    def test_non_numeric_input_to_arithmetic_drops_the_field(self):
        f = {"x": {"from": "x", "transform": [{"op": "add", "by": 1}]}}
        self.assertNotIn("x", values(f, {"x": "a"}))

    def test_unknown_op_is_reported_once_and_drops_the_field(self):
        reg = builtin_registry().fork()
        f = {"x": {"from": "x", "transform": [{"op": "frobnicate"}]}}
        with self.assertLogs("loralarm.frame", level="WARNING") as cm:
            self.assertNotIn("x", values(f, {"x": 1}, registry=reg))
            values(f, {"x": 2}, registry=reg)
        self.assertEqual(len(cm.records), 1)
        self.assertIn("frobnicate", cm.output[0])

    def test_unknown_param_reference_is_reported_not_raised(self):
        f = {"x": {"from": "x", "transform": [{"op": "add", "by": "$missing"}]}}
        with self.assertLogs("loralarm.frame", level="WARNING"):
            self.assertNotIn("x", values(f, {"x": 1}, registry=builtin_registry().fork()))

    def test_custom_transform_is_a_registered_function(self):
        reg = builtin_registry().fork()
        reg.register_transform("c_to_f", lambda v, spec, resolve: v * 9 / 5 + 32)
        f = {"temp_f": {"from": "temperature", "transform": [{"op": "c_to_f"}]}}
        self.assertEqual(values(f, {"temperature": 100.0}, registry=reg)["temp_f"], 212.0)
        with self.assertLogs("loralarm.frame", level="WARNING"):    # the shared registry has no such op
            self.assertNotIn("temp_f", values(f, {"temperature": 100.0}))


class DerivationTest(unittest.TestCase):
    def test_linear_pct_is_clamped_and_rounded(self):
        f = {"p": {"derive": "linear_pct", "of": "v", "empty": 3.0, "full": 4.0, "round": 1}}
        self.assertEqual(values(f, {"v": 3.5})["p"], 50.0)
        self.assertEqual(values(f, {"v": 5.0})["p"], 100.0)
        self.assertEqual(values(f, {"v": 2.0})["p"], 0.0)
        self.assertNotIn("p", values(f, {}))

    def test_linear_pct_with_bad_span_has_no_value(self):
        f = {"p": {"derive": "linear_pct", "of": "v", "empty": "$batt_full_v", "full": "$batt_full_v"}}
        self.assertNotIn("p", values(f, {"v": 3.0}))

    def test_band_first_match_else_and_missing(self):
        f = {"s": {"derive": "band", "of": "v", "missing": "unknown", "else": "bad",
                   "bands": [{"gt": 3.5, "as": "ok"}, {"ge": 3.0, "as": "low"}]}}
        self.assertEqual(values(f, {"v": 3.9})["s"], "ok")
        self.assertEqual(values(f, {"v": 3.5})["s"], "low")
        self.assertEqual(values(f, {"v": 3.0})["s"], "low")
        self.assertEqual(values(f, {"v": 2.9})["s"], "bad")
        self.assertEqual(values(f, {})["s"], "unknown")

    def test_derived_field_sees_renamed_sources_in_any_order(self):
        f = {"pct": {"derive": "linear_pct", "of": "battery_voltage", "empty": 2.0, "full": 3.0},
             "battery_voltage": {"from": "BAT_V", "type": "number"}}
        self.assertEqual(values(f, {"BAT_V": 2.5})["pct"], 50.0)


class StandardFieldsTest(unittest.TestCase):
    def test_every_frame_has_calibrated_temperature_and_battery_status(self):
        frame, _ = evaluate(make_cfg(), make_uplink(obj=sensor_obj(temperature=32.0, battery_voltage=3.9)))
        self.assertIsInstance(frame, Frame)
        self.assertEqual((frame.values["temperature"], frame.values["battery_status"]), (24.0, "ok"))
        self.assertEqual((frame.rssi, frame.snr), (-35, 13.2))

    def test_battery_status_boundaries_match_the_old_rule(self):
        cfg = make_cfg()                                    # warn 3.5, empty 3.2
        status = lambda v: evaluate(cfg, make_uplink(obj=sensor_obj(battery_voltage=v)))[0].values["battery_status"]
        self.assertEqual([status(v) for v in (3.51, 3.5, 3.2, 3.19)], ["ok", "low", "low", "empty"])


class NewSensorTypeNeedsNoCodeTest(unittest.TestCase):
    """A CO2 sensor: calibration, threshold rule and per-device limit, all in JSON."""
    EUI = "0a0b0c0d0e0f1011"
    DEF = {
        "data_ports": [8],
        "settings": [],
        "fields": {"co2_ppm": {"from": "CO2RAW", "unit": "ppm", "type": "number",
                               "transform": [{"op": "multiply", "by": "$co2_scale"},
                                             {"op": "round", "n": 0}]}},
        "defaults": {"kind": "air", "params": {"co2_scale": 2}},
        "rules": [{"kind": "co2", "label": "CO2-larm", "field": "co2_ppm", "when": "gt",
                   "param": "co2_max", "value": 1500, "fire": "{value} ppm > {limit} ppm",
                   "clear": "under {limit} ppm", "device_kinds": ["air"]}],
    }

    def test_calibrated_value_drives_an_alarm_and_is_kept_for_the_status_page(self):
        config = Config({"profiles": {"CO2-1": self.DEF},
                         "devices": {self.EUI: {"name": "co2-hall", "family": "CO2-1",
                                                "params": {"co2_max": 1000}}}})
        notifier = RecordingNotifier()
        engine = Engine(config, notifier=notifier)
        up = lambda raw: make_uplink(dev_eui=self.EUI, device_name="co2-hall", profile="CO2-1",
                                     f_port=8, obj={"CO2RAW": raw})
        engine.on_uplink(up(400))      # 800 ppm
        engine.on_uplink(up(700))      # 1400 ppm > 1000
        engine.on_uplink(up(300))      # 600 ppm
        self.assertEqual([(c[1], c[2], c[3]) for c in notifier.calls],
                         [("co2", True, "1400 ppm > 1000 ppm"), ("co2", False, "under 1000 ppm")])
        _, state = engine.devices[self.EUI]
        self.assertEqual(state.last_values["co2_ppm"], 600)


class LastValuesTest(unittest.TestCase):
    def test_last_values_are_kept_and_survive_a_snapshot_round_trip(self):
        st = AlarmState()
        st.update(make_cfg(), make_uplink(obj=sensor_obj(temperature=32.0, humidity=44)))
        self.assertEqual((st.last_values["temperature"], st.last_values["humidity"]), (24.0, 44))
        snap = json.loads(json.dumps(st.snapshot()))        # what the event log stores
        restored = AlarmState()
        restored.restore(snap)
        self.assertEqual(restored.last_values, st.last_values)
        self.assertEqual((restored.last_rssi, restored.last_snr), (-35, 13.2))

    def test_old_snapshots_without_last_values_still_restore(self):
        restored = AlarmState()
        restored.restore({"active": [], "last_seen": None})
        self.assertEqual(restored.last_values, {})

    def test_non_sensor_frames_keep_the_previous_values(self):
        st = AlarmState()
        st.update(make_cfg(), make_uplink(obj=sensor_obj(temperature=32.0)))
        st.touch(make_uplink(f_port=5, obj={}))
        self.assertEqual(st.last_values["temperature"], 24.0)


if __name__ == "__main__":
    unittest.main()
