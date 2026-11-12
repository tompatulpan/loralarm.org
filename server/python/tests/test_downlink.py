"""Tests for loralarm.downlink: ASCII-3 frames, frequency translation, the
ChirpStack command/down message, the command-topic parser, config-driven
desired settings and Engine wiring."""
from __future__ import annotations

import base64
import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest import mock
from datetime import timedelta
from pathlib import Path

from loralarm import mqttclient
from loralarm.config import Config
from loralarm.downlink import (
    Downlink, DownlinkSender, ascii3, build_message, desired_frames,
    parse_command_message, parse_frequency, resolve_dev_eui, settings_frame,
    wire_to_hz, main as dl_main,
    PORT_COMMAND, PORT_DURATION, PORT_FREQUENCY, PORT_THRESHOLD,
)
from loralarm.events import EventLog, utc_now
from loralarm import service
from loralarm.service import Engine

from .common import DEV_EUI, OTHER_EUI, make_config, make_cfg, make_uplink, sensor_obj

APP_ID = "1d7d3417-e6bf-4e85-ab60-fb422fecdf90"


class Ascii3Test(unittest.TestCase):
    def test_pads_to_three_digits(self):
        self.assertEqual(ascii3(0), b"000")
        self.assertEqual(ascii3(7), b"007")
        self.assertEqual(ascii3(16), b"016")
        self.assertEqual(ascii3(255), b"255")
        self.assertEqual(ascii3("047"), b"047")

    def test_rejects_out_of_range_and_non_numeric(self):
        for bad in (256, -1, "abc", "", 1.5, None):
            with self.assertRaises(ValueError, msg=repr(bad)):
                ascii3(bad)


class FrequencyTranslationTest(unittest.TestCase):
    def test_hz_to_family_wire_value(self):
        self.assertEqual(parse_frequency(50, "RAK4631"), 79)      # register code
        self.assertEqual(parse_frequency(50, "RAK3172"), 4)       # RUI3 ODR index
        self.assertEqual(parse_frequency("hz:100", "RAK4631"), 95)
        self.assertEqual(parse_frequency(200, "RAK3172"), 6)      # RUI3-only rate
        with self.assertRaises(ValueError):                        # no RAK4631 code for 200 Hz
            parse_frequency(200, "RAK4631")

    def test_register_code_translation(self):
        self.assertEqual(parse_frequency(31, "RAK3172"), 1)
        self.assertEqual(parse_frequency(95, "RAK3172"), 5)
        self.assertEqual(parse_frequency("code:63", "RAK3172"), 3)
        self.assertEqual(parse_frequency("code:63", "RAK4631"), 63)  # passthrough

    def test_index_translation(self):
        self.assertEqual(parse_frequency("index:3", "RAK4631"), 63)
        self.assertEqual(parse_frequency(2, "RAK3172"), 2)
        with self.assertRaises(ValueError):                        # index 7 = 400 Hz, no code
            parse_frequency("index:7", "RAK4631")

    def test_unknown_family_passes_wire_values_through(self):
        self.assertEqual(parse_frequency(95, ""), 95)
        self.assertEqual(parse_frequency("index:2", ""), 2)
        with self.assertRaises(ValueError):                        # Hz needs a family
            parse_frequency(50, "")

    def test_garbage_rejected(self):
        for bad in (999, "abc", "code:12", "index:0", "index:8", "hz:3"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                parse_frequency(bad, "RAK3172")


class SettingsFrameTest(unittest.TestCase):
    def test_ports_and_payloads(self):
        f = settings_frame("threshold", 16)
        self.assertEqual((f.f_port, f.data), (PORT_THRESHOLD, b"016"))
        f = settings_frame("duration", 0)
        self.assertEqual((f.f_port, f.data), (PORT_DURATION, b"000"))
        f = settings_frame("command", 255)
        self.assertEqual((f.f_port, f.data), (PORT_COMMAND, b"255"))
        f = settings_frame("frequency", 10, family="RAK3172")
        self.assertEqual((f.f_port, f.data), (PORT_FREQUENCY, b"002"))

    def test_ranges_enforced(self):
        with self.assertRaises(ValueError):
            settings_frame("threshold", 0)      # firmware: 1-127
        with self.assertRaises(ValueError):
            settings_frame("threshold", 128)
        with self.assertRaises(ValueError):
            settings_frame("duration", 128)
        with self.assertRaises(ValueError):
            settings_frame("command", 256)
        with self.assertRaises(ValueError):
            settings_frame("bogus", 1)


class ParseCommandMessageTest(unittest.TestCase):
    def test_settings_keys(self):
        frames = parse_command_message({"threshold": 16, "frequency": 50, "duration": 2},
                                       family="RAK4631", confirmed=True)
        self.assertEqual([(f.f_port, f.data, f.confirmed) for f in frames],
                         [(PORT_THRESHOLD, b"016", True),
                          (PORT_FREQUENCY, b"079", True),
                          (PORT_DURATION, b"002", True)])

    def test_raw_port_value(self):
        frames = parse_command_message({"port": 6, "value": "002"})
        self.assertEqual([(f.f_port, f.data) for f in frames], [(6, b"002")])

    def test_rejects_bad_input(self):
        for bad in ({}, {"bogus": 1}, {"port": 6}, {"port": 0, "value": 1},
                    {"port": 224, "value": 1}, {"port": 6, "value": 1, "threshold": 2},
                    {"port": 6, "value": 999}, ["not", "a", "dict"], "nope"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                parse_command_message(bad)


class DownlinkMessageTest(unittest.TestCase):
    def test_topic_and_payload_match_chirpstack_v4(self):
        dl = Downlink(dev_eui=DEV_EUI.upper(), f_port=PORT_THRESHOLD,
                      data=b"016", confirmed=True, note="threshold=16")
        topic, payload = build_message(APP_ID, dl)
        self.assertEqual(topic, f"application/{APP_ID}/device/{DEV_EUI}/command/down")
        obj = json.loads(payload)
        self.assertEqual(obj, {"devEui": DEV_EUI, "confirmed": True, "fPort": 5,
                               "data": base64.b64encode(b"016").decode("ascii")})
        self.assertEqual(obj["data"], "MDE2")


class SenderTest(unittest.TestCase):
    def test_publishes_to_command_topic(self):
        published = []

        class FakeInfo:
            rc = 0

            def wait_for_publish(self, timeout=None):
                pass

        class FakeClient:
            def publish(self, topic, payload, qos=1):
                published.append((topic, payload, qos))
                return FakeInfo()

        sender = DownlinkSender(FakeClient(), wait=True)
        sender.send(APP_ID, Downlink(DEV_EUI, PORT_FREQUENCY, b"004"))
        self.assertEqual(len(published), 1)
        topic, payload, qos = published[0]
        self.assertEqual(qos, 1)
        self.assertIn(f"/device/{DEV_EUI}/command/down", topic)
        self.assertEqual(json.loads(payload)["fPort"], 6)

    def test_publish_error_raises(self):
        class FakeInfo:
            rc = 4  # MQTT_ERR_NO_CONN

            def wait_for_publish(self, timeout=None):
                pass

        class FakeClient:
            def publish(self, topic, payload, qos=1):
                return FakeInfo()

        with self.assertRaises(ConnectionError):
            DownlinkSender(FakeClient()).send(APP_ID, Downlink(DEV_EUI, 5, b"016"))


class TopicMatchTest(unittest.TestCase):
    def test_wildcards(self):
        m = mqttclient.topic_matches
        self.assertTrue(m("loralarm/down/+", "loralarm/down/sensor3"))
        self.assertTrue(m("loralarm/down/#", "loralarm/down/a/b"))
        self.assertTrue(m("application/+/device/+/event/up",
                          "application/1/device/ab/event/up"))
        self.assertFalse(m("loralarm/down/+", "loralarm/down/a/b"))
        self.assertFalse(m("loralarm/down/+", "other/down/sensor3"))
        self.assertFalse(m("loralarm/down/+", "loralarm/down"))


class ResolveDevEuiTest(unittest.TestCase):
    def test_eui_passthrough_and_name_lookup(self):
        devices = {DEV_EUI: {"name": "sensor3"}, OTHER_EUI: {"name": "sensor16"}}
        self.assertEqual(resolve_dev_eui(DEV_EUI.upper(), devices), DEV_EUI)
        self.assertEqual(resolve_dev_eui("sensor3", devices), DEV_EUI)
        self.assertEqual(resolve_dev_eui(" Sensor16 ", devices), OTHER_EUI)
        with self.assertRaises(ValueError):
            resolve_dev_eui("nosuch", devices)
        with self.assertRaises(ValueError):  # name-keyed config cannot give a devEUI
            resolve_dev_eui("sensor3", {"sensor3": {}})


class EngineDownlinkTest(unittest.TestCase):
    def _engine(self, **kwargs):
        sent = []

        class FakeSender:
            def send(self, app_id, dl):
                sent.append((app_id, dl))

        event_log = EventLog(None)
        engine = Engine(make_config(), event_log, downlink_sender=FakeSender(),
                        dl_topic="loralarm/down/+", **kwargs)
        return engine, event_log, sent

    def _seen(self, engine, app_id=APP_ID):
        up = make_uplink()
        up.application_id = app_id
        engine.on_uplink(up)

    def test_uplink_caches_application_id(self):
        engine, _, _ = self._engine()
        self._seen(engine)
        self.assertEqual(engine.app_ids[DEV_EUI], APP_ID)

    def test_resolve_by_eui_and_name(self):
        engine, _, _ = self._engine()
        self._seen(engine)
        eui, cfg = engine.resolve_device(DEV_EUI)
        self.assertEqual((eui, cfg.name), (DEV_EUI, "sensor3"))
        eui, cfg = engine.resolve_device("SENSOR3")
        self.assertEqual(eui, DEV_EUI)
        with self.assertRaises(KeyError):
            engine.resolve_device("nosuch")

    def test_send_downlinks_publishes_and_logs(self):
        engine, event_log, sent = self._engine()
        self._seen(engine)
        frames = parse_command_message({"threshold": 16, "frequency": 50}, family="RAK4631")
        out = engine.send_downlinks("sensor3", frames, source="test")
        self.assertEqual([(a, d.f_port, d.data) for a, d in sent],
                         [(APP_ID, PORT_THRESHOLD, b"016"), (APP_ID, PORT_FREQUENCY, b"079")])
        self.assertEqual(len(out), 2)
        rows = [r for r in event_log.recent() if r[2] == "downlink"]
        self.assertEqual(len(rows), 2)
        details = {r[4] for r in rows}
        self.assertTrue(any("fPort=5" in d and "016" in d for d in details), details)
        self.assertTrue(any("fPort=6" in d and "079" in d for d in details), details)
        self.assertTrue(all(r[3] for r in rows))  # active=1

    def test_send_downlinks_needs_app_id_and_sender(self):
        engine, _, sent = self._engine()
        engine.on_uplink(make_uplink())  # no application_id, config has none either
        with self.assertRaises(ValueError):
            engine.send_downlinks(DEV_EUI, parse_command_message({"threshold": 16}))
        self.assertEqual(sent, [])
        engine2, _, _ = self._engine()
        engine2.downlink_sender = None
        engine2.on_uplink(make_uplink())
        with self.assertRaises(RuntimeError):
            engine2.send_downlinks(DEV_EUI, parse_command_message({"threshold": 16}))

    def test_app_id_falls_back_to_config(self):
        cfg = Config({"defaults": {"application_id": APP_ID},
                      "devices": {DEV_EUI: {"name": "sensor3"}}})
        sent = []

        class FakeSender:
            def send(self, app_id, dl):
                sent.append(app_id)

        engine = Engine(cfg, EventLog(None), downlink_sender=FakeSender())
        engine.on_uplink(make_uplink())  # uplink without applicationId
        engine.send_downlinks(DEV_EUI, parse_command_message({"duration": 2}))
        self.assertEqual(sent, [APP_ID])

    def test_on_dl_command_happy_path(self):
        engine, event_log, sent = self._engine()
        self._seen(engine)

        class Msg:
            topic = "loralarm/down/sensor3"
            payload = json.dumps({"threshold": 16}).encode()

        engine.on_dl_command(Msg())
        self.assertEqual([(a, d.f_port, d.data) for a, d in sent],
                         [(APP_ID, PORT_THRESHOLD, b"016")])
        rows = [r for r in event_log.recent() if r[2] == "downlink"]
        self.assertEqual(len(rows), 1)

    def test_on_dl_command_error_is_logged_not_raised(self):
        engine, event_log, sent = self._engine()
        self._seen(engine)

        class Msg:
            topic = "loralarm/down/sensor3"
            payload = b"not json"

        engine.on_dl_command(Msg())  # must not raise (paho callback guard)
        self.assertEqual(sent, [])
        rows = [r for r in event_log.recent() if r[2] == "downlink"]
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0][3])  # active=0
        self.assertIn("error", rows[0][4])


class WireToHzTest(unittest.TestCase):
    def test_codes_and_indices(self):
        self.assertEqual(wire_to_hz(95, "RAK4631"), 100.0)
        self.assertEqual(wire_to_hz(31, "RAK4631"), 1.0)
        self.assertEqual(wire_to_hz(2, "RAK3172"), 10.0)
        self.assertEqual(wire_to_hz(7, "RAK3172"), 400.0)
        self.assertEqual(wire_to_hz(95.0, ""), 100.0)     # float from JSON, unknown family
        self.assertIsNone(wire_to_hz(99, "RAK4631"))
        self.assertIsNone(wire_to_hz("abc", "RAK4631"))

    def test_frequency_note_includes_hz(self):
        f = settings_frame("frequency", 10, family="RAK4631")
        self.assertEqual(f.note, "frequency wire=47 (10 Hz)")


class StatusLineFormatTest(unittest.TestCase):
    """The `acc settings:` console line: integers without .0, frequency in Hz."""

    def _run(self, devices_entry, obj, profile="RAK4631"):
        sent = []

        class FakeSender:
            def send(self, app_id, dl):
                sent.append(dl)

        engine = Engine(make_config(devices={DEV_EUI: devices_entry}), EventLog(None),
                        downlink_sender=FakeSender(), dl_topic="loralarm/down/+")
        up = make_uplink(profile=profile, obj=obj, time=utc_now())
        up.application_id = APP_ID
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            engine.on_uplink(up)
        return buf.getvalue(), sent

    def test_ints_not_floats_and_frequency_in_hz(self):
        out, sent = self._run(
            {"name": "sensor3", "threshold": 8, "frequency": 10, "duration": 1},
            sensor_obj(acc_threshold=2.0, acc_frequency=95.0, acc_duration=1.0))
        line = next(l for l in out.splitlines() if "acc settings:" in l)
        self.assertIn("threshold=2 want 8 -> sending", line)
        self.assertIn("frequency=100 Hz (code 95) want 10 Hz (code 47) -> sending", line)
        self.assertIn("duration=1 ok", line)
        self.assertNotIn(".0", line)
        self.assertEqual([(d.f_port, d.data) for d in sent],
                         [(PORT_THRESHOLD, b"008"), (PORT_FREQUENCY, b"047")])

    def test_rui3_frequency_shown_as_index(self):
        out, _ = self._run({"name": "sensor16", "frequency": 10},
                           sensor_obj(acc_frequency=5.0), profile="RAK3172")
        line = next(l for l in out.splitlines() if "acc settings:" in l)
        self.assertIn("frequency=100 Hz (index 5) want 10 Hz (index 2) -> sending", line)

    def test_ok_values_print_as_ints(self):
        out, sent = self._run({"name": "sensor3", "threshold": 8},
                              sensor_obj(acc_threshold=8.0))
        line = next(l for l in out.splitlines() if "acc settings:" in l)
        self.assertIn("threshold=8 ok", line)
        self.assertEqual(sent, [])


class CliTest(unittest.TestCase):
    def test_dry_run_resolves_name_and_translates_frequency(self):
        rc = dl_main(["--dev", "sensor16", "--app-id", "test-app", "--family", "RAK3172",
                      "--set", "frequency=50", "--set", "threshold=16",
                      "--config", "devices.example.json", "--dry-run"])
        self.assertEqual(rc, 0)

    def test_no_set_pushes_configured_settings(self):
        # sensor3 in devices.example.json has threshold=8, frequency=10, duration=1,
        # family=RAK4631 -> wire values 008 / 047 / 001
        rc = dl_main(["--dev", "sensor3", "--app-id", "test-app",
                      "--config", "devices.example.json", "--dry-run"])
        self.assertEqual(rc, 0)

    def test_device_application_id_beats_env_default(self):
        buf = io.StringIO()
        with mock.patch.dict(os.environ, {"LORALARM_CS_APP_ID": "env-app"}), \
                mock.patch.dict(os.environ, {"LORALARM_CS_APP_LARM": "cfg-app"}), \
                contextlib.redirect_stdout(buf):
            dl_main(["--dev", "sensor3", "--set", "threshold=8",
                     "--config", "devices.example.json", "--dry-run"])
        self.assertIn("application/cfg-app/", buf.getvalue())

    def test_bad_value_is_a_clean_error_not_a_traceback(self):
        with self.assertRaises(SystemExit) as cm:
            dl_main(["--dev", "sensor3", "--set", "threshold=999",
                     "--config", "devices.example.json", "--dry-run"])
        self.assertEqual(cm.exception.code, 2)  # argparse error exit


class DesiredFramesTest(unittest.TestCase):
    def test_config_keys_become_frames_translated_per_family(self):
        cfg = make_cfg(threshold=8, frequency=10, duration=1, family="RAK4631")
        frames = desired_frames(cfg)
        self.assertEqual([(f.f_port, f.data) for f in frames],
                         [(PORT_THRESHOLD, b"008"), (PORT_FREQUENCY, b"047"),
                          (PORT_DURATION, b"001")])
        cfg = make_cfg(threshold=8, frequency=10, duration=1, family="RAK3172")
        self.assertEqual([(f.f_port, f.data) for f in desired_frames(cfg)],
                         [(PORT_THRESHOLD, b"008"), (PORT_FREQUENCY, b"002"),
                          (PORT_DURATION, b"001")])  # 10 Hz = RUI3 ODR index 2

    def test_unset_keys_produce_no_frames(self):
        self.assertEqual(desired_frames(make_cfg()), [])

    def test_out_of_range_config_value_raises(self):
        with self.assertRaises(ValueError):
            desired_frames(make_cfg(threshold=200))

    def test_config_parsing_and_typo_guard(self):
        cfg = Config({"devices": {DEV_EUI: {"name": "sensor3", "threshold": 8,
                                            "frequency": 10, "duration": 1,
                                            "threshol": 5}}})  # typo: warned + ignored
        dev = cfg.for_event(DEV_EUI, "sensor3", "RAK4631")
        self.assertEqual((dev.threshold, dev.frequency, dev.duration), (8, 10, 1))
        self.assertFalse(hasattr(dev, "threshol"))

    def test_application_id_is_per_device(self):
        # the application is a property of the device registration, not of the
        # family: RAK4631 devices can live in different applications, so the
        # per-device key is the reliable one (live service prefers the
        # uplink-learned id anyway)
        cfg = Config({
            "devices": {
                DEV_EUI: {"name": "sensor3", "family": "RAK4631",
                          "application_id": "applarm"},
                OTHER_EUI: {"name": "sensor16", "family": "RAK4631",
                            "application_id": "appsublarm"},  # same family, other app!
            },
        })
        self.assertEqual(cfg.for_event(DEV_EUI, "sensor3", "RAK4631").application_id,
                         "applarm")
        self.assertEqual(cfg.for_event(OTHER_EUI, "sensor16", "RAK4631").application_id,
                         "appsublarm")

    def test_application_id_in_defaults_or_families_warns(self):
        # mechanically the layering still applies, but it is only correct when the
        # whole layer shares one application — warn so a mixed fleet notices
        with self.assertLogs("loralarm.config", level="WARNING") as cm:
            cfg = Config({
                "defaults": {"application_id": "app-default"},
                "families": {"RAK4631": {"application_id": "applarm"}},
                "devices": {DEV_EUI: {"name": "sensor3", "application_id": "app-override"}},
            })
        self.assertTrue(any("defaults" in m for m in cm.output), cm.output)
        self.assertTrue(any("families.RAK4631" in m for m in cm.output), cm.output)
        # device key wins over the layers
        self.assertEqual(cfg.for_event(DEV_EUI, "sensor3", "RAK4631").application_id,
                         "app-override")
        self.assertEqual(cfg.for_event(OTHER_EUI, "x", "RAK4631").application_id, "applarm")
        self.assertEqual(cfg.for_event("aa00000000000000", "x", "").application_id,
                         "app-default")

    def test_uplink_learned_app_id_wins_over_config(self):
        sent = []

        class FakeSender:
            def send(self, app_id, dl):
                sent.append(app_id)

        cfg = Config({"devices": {DEV_EUI: {"name": "sensor3", "application_id": "cfg-app"}}})
        engine = Engine(cfg, EventLog(None), downlink_sender=FakeSender())
        up = make_uplink()
        up.application_id = APP_ID
        engine.on_uplink(up)
        engine.send_downlinks(DEV_EUI, parse_command_message({"threshold": 16}))
        self.assertEqual(sent, [APP_ID])  # not "cfg-app": the device's own app


class AutoEnforceTest(unittest.TestCase):
    """Config-declared desired settings are pushed when the device-reported
    values in the uplink object differ."""

    def _engine(self, devices_entry, config_path=None):
        sent = []

        class FakeSender:
            def send(self, app_id, dl):
                sent.append((app_id, dl))

        event_log = EventLog(None)
        config = (Config.load(config_path) if config_path
                  else make_config(devices={DEV_EUI: devices_entry}))
        engine = Engine(config, event_log, downlink_sender=FakeSender(),
                        dl_topic="loralarm/down/+",
                        config_path=config_path)
        return engine, event_log, sent

    def _uplink(self, engine, obj, profile="RAK4631", app_id=APP_ID):
        up = make_uplink(profile=profile, obj=obj, time=utc_now())
        up.application_id = app_id
        engine.on_uplink(up)

    def test_mismatched_settings_are_pushed(self):
        engine, event_log, sent = self._engine(
            {"name": "sensor3", "threshold": 8, "frequency": 10, "duration": 1})
        self._uplink(engine, sensor_obj(acc_threshold=2, acc_frequency=31, acc_duration=9))
        self.assertEqual([(a, d.f_port, d.data) for a, d in sent],
                         [(APP_ID, PORT_THRESHOLD, b"008"), (APP_ID, PORT_FREQUENCY, b"047"),
                          (APP_ID, PORT_DURATION, b"001")])
        rows = [r for r in event_log.recent() if r[2] == "downlink"]
        self.assertEqual(len(rows), 3)
        self.assertTrue(all("auto" in r[4] for r in rows), rows)

    def test_matching_settings_send_nothing(self):
        engine, _, sent = self._engine({"name": "sensor3", "threshold": 8, "frequency": 10})
        self._uplink(engine, sensor_obj(acc_threshold=8, acc_frequency=47))  # 10 Hz as code
        self.assertEqual(sent, [])

    def test_frequency_translated_for_rui3_family(self):
        sent = []

        class FakeSender:
            def send(self, app_id, dl):
                sent.append((app_id, dl))

        config = make_config(devices={OTHER_EUI: {"name": "sensor16", "frequency": 10}})
        engine = Engine(config, EventLog(None), downlink_sender=FakeSender(),
                        dl_topic="loralarm/down/+")
        up = make_uplink(dev_eui=OTHER_EUI, device_name="sensor16", profile="RAK3172",
                         obj=sensor_obj(acc_frequency=5), time=utc_now())
        up.application_id = APP_ID
        engine.on_uplink(up)
        # 10 Hz = RUI3 ODR index 2; device reports index 5 -> push index 2
        self.assertEqual([(d.f_port, d.data) for _, d in sent], [(PORT_FREQUENCY, b"002")])

    def test_missing_reported_fields_send_nothing(self):
        engine, _, sent = self._engine({"name": "sensor3", "threshold": 8})
        self._uplink(engine, sensor_obj())  # no acc_* fields in this frame
        self.assertEqual(sent, [])

    def test_invalid_config_disables_auto_downlink_without_raising(self):
        engine, _, sent = self._engine({"name": "sensor3", "threshold": 200})
        self._uplink(engine, sensor_obj(acc_threshold=2))
        self._uplink(engine, sensor_obj(acc_threshold=2))
        self.assertEqual(sent, [])
        self.assertIn(DEV_EUI, engine._dl_bad)

    def test_no_sender_means_no_auto_downlink(self):
        engine, _, _ = self._engine({"name": "sensor3", "threshold": 8})
        engine.downlink_sender = None
        self._uplink(engine, sensor_obj(acc_threshold=2))  # must not raise
        self.assertNotIn(DEV_EUI, engine._dl_bad)

    def test_config_reload_pushes_changed_settings_immediately(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "devices.json"
            path.write_text(json.dumps({"devices": {DEV_EUI: {
                "name": "sensor3", "threshold": 8, "application_id": APP_ID}}}))
            engine, _, sent = self._engine(None, config_path=path)
            self._uplink(engine, sensor_obj(acc_threshold=8))   # matches: no downlink
            self.assertEqual(sent, [])

            path.write_text(json.dumps({"devices": {DEV_EUI: {
                "name": "sensor3", "threshold": 9, "application_id": APP_ID}}}))
            os.utime(path, ns=(path.stat().st_atime_ns, path.stat().st_mtime_ns + 10**9))
            engine.watchdog_tick()  # triggers the reload + push
            self.assertEqual([(d.f_port, d.data) for _, d in sent], [(PORT_THRESHOLD, b"009")])


    def test_same_value_not_requeued_on_every_uplink(self):
        engine, _, sent = self._engine({"name": "sensor3", "threshold": 8})
        for _ in range(3):
            self._uplink(engine, sensor_obj(acc_threshold=2))
        self.assertEqual(len(sent), 1)

    def test_resend_after_cooldown_then_gives_up(self):
        engine, _, sent = self._engine({"name": "sensor3", "threshold": 8})
        for _ in range(service.DL_MAX_ATTEMPTS + 3):
            self._uplink(engine, sensor_obj(acc_threshold=2))
            for rec in engine._dl_sent.values():
                rec.at -= timedelta(seconds=service.DL_RESEND_S + 1)
        self.assertEqual(len(sent), service.DL_MAX_ATTEMPTS)

    def test_applied_value_clears_the_resend_record(self):
        engine, _, sent = self._engine({"name": "sensor3", "threshold": 8})
        self._uplink(engine, sensor_obj(acc_threshold=2))
        self._uplink(engine, sensor_obj(acc_threshold=8))
        self.assertEqual(engine._dl_sent, {})
        self._uplink(engine, sensor_obj(acc_threshold=2))  # drifted again: sent again
        self.assertEqual(len(sent), 2)

    def test_readback_logs_downlink_ok(self):
        engine, event_log, _ = self._engine({"name": "sensor3", "threshold": 8})
        self._uplink(engine, sensor_obj(acc_threshold=2))   # mismatch -> sent
        self._uplink(engine, sensor_obj(acc_threshold=2))   # not applied yet: no ok row
        self.assertEqual([r for r in event_log.recent() if r[2] == "downlink_ok"], [])
        self._uplink(engine, sensor_obj(acc_threshold=8))   # device reports the new value
        rows = [r for r in event_log.recent() if r[2] == "downlink_ok"]
        self.assertEqual(len(rows), 1)
        self.assertIn("threshold", rows[0][4])
        self._uplink(engine, sensor_obj(acc_threshold=8))   # logged once only
        self.assertEqual(len([r for r in event_log.recent() if r[2] == "downlink_ok"]), 1)

    def test_readback_confirms_manual_downlink(self):
        engine, event_log, _ = self._engine({"name": "sensor3"})
        self._uplink(engine, sensor_obj())
        engine.send_downlinks(DEV_EUI, parse_command_message({"threshold": 16}, "RAK4631"))
        self._uplink(engine, sensor_obj(acc_threshold=16))
        self.assertEqual(len([r for r in event_log.recent() if r[2] == "downlink_ok"]), 1)

    def test_publish_failure_does_not_lose_alarm_notification(self):
        notified = []

        class Notifier:
            def notify(self, cfg, kind, active, detail, registry=None):
                notified.append((kind, active))

        class BrokenSender:
            def send(self, app_id, dl):
                raise ConnectionError("broker down")

        engine = Engine(make_config(devices={DEV_EUI: {"name": "sensor3", "threshold": 8}}),
                        EventLog(None), Notifier(), downlink_sender=BrokenSender(),
                        dl_topic="loralarm/down/+")
        up = make_uplink(profile="RAK4631", time=utc_now(),
                         obj=sensor_obj(temperature=-50, acc_threshold=2))
        up.application_id = APP_ID
        with self.assertLogs("loralarm.service", level="ERROR"):
            engine.on_uplink(up)  # must not raise
        self.assertIn(("temp_low", True), notified)
        self.assertNotIn(DEV_EUI, engine._dl_bad)  # transient: retried next uplink


if __name__ == "__main__":
    unittest.main()
