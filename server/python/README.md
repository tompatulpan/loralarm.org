# server/python/ — loralarm Python service (migration spec §7)

The Node-RED → ChirpStack + InfluxDB + Python migration target
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
| `loralarm/config.py` | Per-device config, JSON keyed by devEUI; layering `defaults` < `families[profile]` < `devices[devEUI]`; family defaults = spec §4 (RAK3172 vs RAK4631 Tk/battery); `${ENV_VAR}` strings in the JSON are expanded from the environment |
| `loralarm/alarms.py` | Pure port of the Node-RED alarm subflows: temp (after Tk), battery %/status, motion, circuit, tilt (baseline ± tilt_sense per axis), watchdog; fire-once/clear-once via `AlarmState` |
| `loralarm/service.py` | The live service (`python3 -m loralarm`): Engine + watchdog tick + MQTT wiring + state persistence |
| `loralarm/mqttclient.py` | Shared paho-mqtt client builder used by service and replay (guarded callbacks, connect retry) |
| `loralarm/notify.py` | ntfy + Pushover delivery on a worker queue with retries; RFC 2047-encoded titles |
| `loralarm/events.py` | SQLite event log + per-device state snapshots (fixed-width UTC timestamps) |
| `loralarm/replay.py` | Test app CLI: replay captured events or subscribe live MQTT |
| `devices.example.json` | Tracked config template — placeholders only, no secrets |
| `tests/` | `python3 -m unittest discover -s tests -t .` (stdlib only) |

Codecs stay in ChirpStack (`../chirpstack/payload_codec_*.js`); this package never
decodes fPort 2 itself — it trusts `object`.

## Secrets

Never put real credentials in tracked files. `devices.example.json` uses
`"${LORALARM_PUSHOVER_USER}"` / `"${LORALARM_PUSHOVER_TOKEN}"` placeholders that
`config.py` expands from the environment. Copy it to `devices.json` (untracked,
see `.gitignore`) for the real config, or export the variables. The MQTT password
comes from `--pass-file` or `LORALARM_MQTT_PASSWORD`, not `--pass` (visible in `ps`).

## Run

```bash
cd server/python
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt   # paho-mqtt only

# offline replay of captured real events (no broker needed):
.venv/bin/python3 -m loralarm.replay ../chirpstack/log/*.json --config devices.example.json

# the live service:
.venv/bin/python3 -m loralarm --mqtt <lora-box> --config devices.json \
    --ntfy-url http://<lora-box>:8075 [--tls] [--dry-run]
```

Expected replay output: the two `* alarm` logs each raise one `Rörelselarm`
(motion_alert=1); sensor3 shows `temp=24.0 °C` (raw 32 − Tk 8) and battery 37.8 %.

## Delivery and reliability notes

- Notifications are queued and sent by one worker with retries (1 s / 5 s backoff);
  a broken `notify_*` template or title falls back to the built-in text.
- `devices.json` is hot-reloaded; a broken edit is logged and the previous config
  stays active. Values of the wrong type are ignored in favour of the lower layer.
- The service uses a fixed MQTT client id (`--client-id`, default `loralarm-service`)
  with a persistent session and QoS 1, so the broker can queue uplinks while the
  service is down — this only helps if ChirpStack publishes at QoS ≥ 1
  (`[integration.mqtt] qos`). Run one service instance per client id.

## Circuit alarm (`circuit_mode`)

- `level` (RAK3172/RUI3): the switch state is in every heartbeat → fire on open, clear on close.
- `event` (RAK4631, default for that family): the firmware sends the circuit bit in
  **one** frame when the contact opens and nothing when it closes, and every other
  frame (timer, ACC alarm) has the bit clear. So each pulse is a fire-only alarm
  (pulses within `circuit_hold_s`, default 30 s, count as one) and no "kvitterad" is
  sent. Override per family or device in `devices.json` with `circuit_mode`.
## Motion alarm (`motion_mode`)

- The firmware sends `motion_alert=1` in **one** frame per accelerometer interrupt
  and never in heartbeats, so the bit is a pulse, not a level.
- `event` (**default**): every detection is reported — each pulse fires a
  fire-only `Rörelselarm` (no "kvitterad"), so shaking a device shows every
  detection. Detections are counted in a sliding `motion_window_s` (default 30 s)
  window and included in the alarm detail ("3 detekteringar inom 30 s").
- `motion_min_detections` (default 1) sets how many detections within
  `motion_window_s` trigger the alarm: 1 = every detection (transparent, current
  behaviour); set it to 3 to alarm only on "3 detections within 30 s".
- `level`: the old fire-once/clear-once behaviour (fires on the first pulse, clears
  on the next frame without the bit, re-arms after that). Override per family or
  device in `devices.json` with `motion_mode`.

## Tests

```bash
cd server/python
python3 -m unittest discover -s tests -t .        # stdlib only
# or: pytest                                       # if installed
```

## Next steps toward fleet dashboards (spec §7/§9)

1. ChirpStack REST registry cache (devEUI → name/profile) at startup + interval refresh.
2. Downlinks via ChirpStack MQTT downlink topic (ports 4–7, ASCII decimal).
3. Status page (FastAPI, spec §9) reading the events SQLite log.
4. Optional: hysteresis/debounce on temp and battery thresholds to avoid flapping
   at the boundary.
