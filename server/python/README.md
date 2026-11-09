# server/python/ — loralarm Python service (migration spec §7)

First milestone of the Node-RED → ChirpStack + InfluxDB + Python migration
(`../chirpstack/loralarm-node-red-→-chirpstack-+-influxdb-+-python-migration-spec.md`).

**Ingest answer:** yes, the existing Mosquitto broker is the ingest path. ChirpStack
already publishes every uplink to `application/+/device/+/event/up` (the topic the
Node-RED flows subscribe to), with the codec-decoded payload in `object`. This
package consumes that feed in parallel with Node-RED — no custom gateway code.
Verify on the lora box with `mosquitto_sub -h localhost -t 'application/+/device/+/event/up' -v`.

## Layout

| File | Role |
|---|---|
| `loralarm/event.py` | Parse a ChirpStack up event → `Uplink` (devEUI, fPort, `object`, rssi/snr; fPort 4–7 ASCII settings frames parsed here, not by the codec) |
| `loralarm/config.py` | Per-device config, JSON keyed by devEUI; layering `defaults` < `families[profile]` < `devices[devEUI]`; family defaults = spec §4 (RUI3 vs RAK4631 Tk/battery) |
| `loralarm/alarms.py` | Pure port of the Node-RED alarm subflows: temp (after Tk), battery %/status, motion, circuit, tilt (baseline ± tilt_sense per axis), watchdog; fire-once/clear-once via `AlarmState` |
| `loralarm/replay.py` | Test app CLI: replay captured events or subscribe live MQTT |
| `devices.example.json` | Example config for sensor3 (RAK4631) + sensor16 (RUI3) |

Codecs stay in ChirpStack (`../chirpstack/payload_codec_*.js`); this package never
decodes fPort 2 itself — it trusts `object`.

## Run the test app

```bash
cd server/python
python3 -m loralarm.replay ../chirpstack/log/*.json --config devices.example.json   # offline, captured real events
python3 -m loralarm.replay --mqtt <lora-box>                                        # live (pip install paho-mqtt)
```

Expected: the two `* alarm` logs each raise one `Rörelselarm` (motion_alert=1);
sensor3 shows `temp=24.0 °C` (raw 32 − Tk 8) and battery 37.8 %.

## Next steps toward the full service (spec §7)

1. Replace the replay loop with an asyncio `paho-mqtt` consumer + periodic watchdog tick.
2. Notifications: ntfy + Pushover per transition (config keys already in place).
3. ChirpStack REST registry cache (devEUI → name/profile) at startup + interval refresh.
4. Downlinks via ChirpStack MQTT downlink topic (ports 4–7, ASCII decimal).
5. Events log (SQLite) + status page (FastAPI, spec §9).
