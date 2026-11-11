"""Shared helpers for the loralarm tests."""
from __future__ import annotations

from datetime import datetime, timezone

from loralarm import alarms as _alarms
from loralarm.config import Config, DeviceConfig
from loralarm.definitions import builtin_registry
from loralarm.event import Uplink

DEV_EUI = "ac1f09fffe1003ae"
OTHER_EUI = "ac1f09fffe12da63"

T0 = datetime(2026, 11, 9, 15, 33, 14, 527766, tzinfo=timezone.utc)

# the shared built-in registry: tests only *read* it; anything that registers
# rules/profiles must work on `builtin_registry().fork()`
REG = builtin_registry()


def evaluate(cfg, up, registry=REG):
    return _alarms.evaluate(cfg, up, registry)


def clear_detail(kind, cfg, reading, registry=REG):
    return _alarms.clear_detail(kind, cfg, reading, registry)


class AlarmState(_alarms.AlarmState):
    """AlarmState bound to the built-in registry unless a test passes its own."""

    def update(self, cfg, up, registry=REG):
        return super().update(cfg, up, registry)

    def restore(self, snap, registry=REG):
        return super().restore(snap, registry)


def make_uplink(dev_eui: str = DEV_EUI, device_name: str = "sensor3",
                profile: str = "RAK4631", f_port: int = 2, obj: dict | None = None,
                time: datetime = T0, f_cnt: int = 1, data_b64: str = "",
                rssi: int | None = -35, snr: float | None = 13.2) -> Uplink:
    return Uplink(dev_eui=dev_eui, device_name=device_name, application_name="Larm",
                  device_profile_name=profile, f_port=f_port, f_cnt=f_cnt,
                  time=time, data_b64=data_b64, obj=obj if obj is not None else {},
                  rssi=rssi, snr=snr)


def sensor_obj(**overrides) -> dict:
    obj = {"temperature": 32.0, "battery_voltage": 3.9, "motion_alert": 0,
           "motion_switch": 0, "accelerometer": {"x": 0.1, "y": 0.0, "z": 1.0}}
    obj.update(overrides)
    return obj


def make_cfg(**overrides) -> DeviceConfig:
    """RAK4631-flavoured config with sane thresholds."""
    kwargs = dict(dev_eui=DEV_EUI, name="sensor3", family="RAK4631",
                  tk_correction_c=8.0, temp_alarm_limit_c=2.0,
                  batt_full_v=4.2, batt_warn_v=3.5, batt_empty_v=3.2,
                  tilt_sense_g=0.3, watchdog_hours=24.0, ntfy_topic="sensor3")
    kwargs.update(overrides)
    return DeviceConfig(**kwargs)


def make_config(devices: dict | None = None, defaults: dict | None = None,
                families: dict | None = None, registry=None) -> Config:
    return Config({"defaults": defaults or {}, "families": families or {},
                   "devices": devices if devices is not None else {DEV_EUI: {"name": "sensor3"}}},
                  registry)
