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
| `loralarm/families/*.json` | One built-in device-family definition per file (RAK3172, RAK4631, Dragino LDS02): config defaults (spec §4), data ports, field map, downlink settings, frequency table, alarm rules. See "Adding a device" |
| `loralarm/definitions.py` | Loads those files and the `"profiles"` section of `devices.json` into the registries (validated; user definitions override built-ins and are undone on reload) |
| `loralarm/profiles.py` | `DeviceProfile` / `SettingSpec` classes and the profile registry; unknown families get `GENERIC` |
| `loralarm/config.py` | Per-device config, JSON keyed by devEUI; layering `defaults` < `families[profile]` < `devices[devEUI]` (family defaults come from `profiles.py`); `kind` selects the alarm rules, `params` carries rule-specific thresholds; `${ENV_VAR}` strings in the JSON are expanded from the environment |
| `loralarm/alarms.py` | Rule-driven port of the Node-RED alarm subflows: an `AlarmRule` registry (temp after Tk, battery low/empty, motion, circuit; each limited to device kinds) plus stateful detectors in `AlarmState` (tilt baseline ± tilt_sense per axis, pulse-style motion/circuit, watchdog); fire-once/clear-once |
| `loralarm/service.py` | The live service (`python3 -m loralarm`): Engine + watchdog tick + MQTT wiring + state persistence |
| `loralarm/mqttclient.py` | Shared paho-mqtt client builder used by service and replay (guarded callbacks, connect retry) |
| `loralarm/notify.py` | ntfy + Pushover delivery on a worker queue with retries; RFC 2047-encoded titles |
| `loralarm/events.py` | SQLite event log + per-device state snapshots (fixed-width UTC timestamps) |
| `loralarm/downlink.py` | Per-device downlinks: ChirpStack MQTT `command/down`, ports 4–7 ASCII-decimal, frequency translation, CLI (`python3 -m loralarm.downlink`) |
| `loralarm/replay.py` | Test app CLI: replay captured events or subscribe live MQTT |
| `devices.example.json` | Tracked config template — placeholders only, no secrets |
| `tests/` | `python3 -m unittest discover -s tests -t .` (stdlib only) |

Codecs stay in ChirpStack (`../chirpstack/payload_codec_*.js`); this package never
decodes fPort 2 itself — it trusts `object`.

### Canonical codec fields

The codec is the device boundary: whatever the wire format, it must emit these
`object` field names (omit a field the device does not have):

| field | type | used by |
|---|---|---|
| `temperature` | °C, raw (family `tk_correction_c` is subtracted here) | `temp` rule |
| `battery_voltage` | V | `battery_low` / `battery_empty` rules, % in console |
| `accelerometer` | `{x,y,z}` in g | tilt detector |
| `motion_alert` | 0/1 | `motion` rule / event-mode pulse detector |
| `motion_switch` | 0/1 | `circuit` rule / event-mode pulse detector |
| `acc_threshold`, `acc_duration`, `acc_frequency` | device-reported wire values | downlink read-back (`SettingSpec.obj_field`) |
| `acc_frequency_hz` | Hz | informational |
| `door_open` | 0/1 (1 = open) | `door` rule (device kind `door`) |
| anything else (`humidity`, `leak`, ...) | any | read by your own `AlarmRule` via `obj` |

## Adding a device or sensor

Devices are **data, not code**: one JSON file per family in
[`loralarm/families/`](loralarm/families) (`rak3172.json`, `rak4631.json`,
`dragino_lds02.json`), and the same schema works in a top-level `"profiles"`
object of `devices.json` (a definition there overrides a built-in of the same
name and is removed again when you delete it — `devices.json` is hot-reloaded;
a bad definition is logged and skipped). Full schema: `loralarm/definitions.py`.

1. **Codec** in ChirpStack. If you cannot change its field names or fPort
   (third-party codec), map them in the definition: `field_map` (codec field →
   canonical field, originals kept) and `data_ports`.
2. **Definition** (`families/<name>.json` or `devices.json` → `"profiles"`):

   ```json
   "profiles": {
     "Dragino Switch LDS02": {
       "aliases": ["lds02"],
       "data_ports": [10],
       "field_map": {"BAT_V": "battery_voltage", "DOOR_OPEN_STATUS": "door_open"},
       "settings": [], "settings_kinds": [],
       "defaults": {"kind": "door", "batt_warn_v": 2.5},
       "rules": [{"kind": "door", "label": "Dörrlarm", "field": "door_open",
                  "when": "truthy", "fire": "dörren är öppen", "clear": "dörren är stängd",
                  "device_kinds": ["door"],
                  "notify_fire": "🚪 DÖRRLARM: {name} — {detail}"}]
     }
   }
   ```

   - `settings` omitted = the standard ACC settings, `[]` = none, else a list of
     `{key, port, lo, hi, obj_field, kind}` (desired values go in the device's
     `params`, pushed and read back like `threshold`).
   - `rules`: `when` is `truthy | falsy | eq | ne | gt | ge | lt | le`; the
     comparisons use `value`, or `cfg.params[param]` when the device sets that
     param (`"params": {"humidity_max": 80}`, merged defaults < family < device).
     `fire` may use `{value} {limit} {field}`, `clear` `{limit} {field}`.
     `device_kinds` limits the rule; `group` makes two severities an escalation.
   - `defaults` are the family layer of the config (any device config key,
     including `ntfy_topic`, `pushover_*`, `notify_*`).
3. **Device entry** in `devices.json` under `"devices"` with `"family"` (the
   ChirpStack device-profile name) — see below.

Anything that needs code (a stateful detector, a new derived reading) is still
a Python `register_rule(AlarmRule(...))` / `register_profile(DeviceProfile(...))`
call; stateful detectors like tilt are registered label-only (`check=None`).
`kind` selects which rules run — `sensor` (everything, the default), `door`
(temp, battery, circuit, plus rules naming `door`), others (temp, battery, plus
rules naming that kind) — and whether the auto-downlink settings apply
(`settings_kinds`). An unknown `kind` logs a warning once.

### Every device needs its own `devices` entry for notifications

ntfy topic and Pushover credentials are **per-device config**, not part of the
family. A device that is not under `"devices"` still alarms, but with the
default ntfy topic (`loralarm`) and **no Pushover**; the service logs
`device X is not in the config: using defaults …` once when it first sees it.
Add it (see Door1/Door2 in `devices.example.json`), or put `ntfy_topic` /
`pushover_user` / `pushover_token` in `"defaults"` to cover every device:

```json
"a84041a5d7598dd0": {"name": "Door1", "family": "Dragino Switch LDS02",
                     "ntfy_topic": "door1", "watchdog_hours": 2,
                     "pushover_user": "${LORALARM_PUSHOVER_USER}",
                     "pushover_token": "${LORALARM_PUSHOVER_TOKEN}"}
```

### Dragino LDS02 door switch

Built in as `families/dragino_lds02.json` (profile name = the ChirpStack
device profile `Dragino Switch LDS02`): fPort 10, `DOOR_OPEN_STATUS` →
`door_open`, `BAT_V` → `battery_voltage`, kind `door`, no downlinks, battery
limits for 2 × AAA (full 3.0 / warn 2.5 / empty 2.2 V). Opening the door raises
`Dörrlarm`, closing it clears it ("kvitterad"); heartbeats while open do not
repeat it.

Stateful detectors (like tilt) still belong in `AlarmState`; register them
label-only with `AlarmRule(kind, label, check=None, ...)`.

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

## Downlinks (per device, spec §7.4)

Path (confirmed against the ChirpStack v4 MQTT integration docs): publish to
`application/<APPLICATION_ID>/device/<DEV_EUI>/command/down` with
`{"devEui": "...", "confirmed": false, "fPort": N, "data": "<base64>"}` — raw
`data` bypasses the device-profile codec (`object` would go through
`encodeDownlink`). The ChirpStack **application is a property of each device
registration, not of its profile/family** — RAK4631 sensors can sit in `larm`
and in `sublarm` at the same time. The live service therefore learns the
application ID per device from uplink events (`deviceInfo.applicationId`,
cached in `Engine.app_ids`) and that value always wins; no config needed once
a device has sent one uplink. For the CLI / devices that have not uplinked
yet, set `application_id` **per device** in `devices.json`
(`devices.<devEui>.application_id`, e.g. `"${LORALARM_CS_APP_LARM}"` — one env
var per application, referenced from whichever devices live there). Putting
`application_id` in `defaults`/`families` is only correct when that whole
layer shares one application; `config.py` logs a warning when it sees it
there. CLI precedence: `--app-id` > the device's `application_id` in the config
(device > family > defaults) > `LORALARM_CS_APP_ID` env (a last-resort fallback,
so one global env var cannot override a device that lives in another application).
A wrong app UUID would publish the downlink to
an application the device is not in, so unknown = clean error, never a guess.

Device-side protocol, both families (3-digit zero-padded ASCII decimal, 0–255):

| fPort | meaning | RAK4631 | RUI3 |
|---|---|---|---|
| 4 | command (test alarm etc.) | yes | ignored by firmware |
| 5 | interrupt threshold 1–127 | `"016"` | `"016"` |
| 6 | ODR frequency | register code 31/47/63/79/95 | index 1–7 (`"002"` = 10 Hz) |
| 7 | interrupt duration 0–127 | `"001"` | `"001"` |

Frequency translation (spec §7.4): 31/47/63/79/95 = 1/10/25/50/100 Hz ↔ RUI3
index 1–5. Accepts Hz, register codes, RUI3 indices, or explicit
`hz:`/`code:`/`index:` prefixes; translated per the device's family
(`--family` / `family` in config / profile learned from uplinks). RUI3-only
rates 200/400 Hz (index 6/7) have no RAK4631 code and are rejected.

### 1. Desired settings in `devices.json` (recommended, automatic)

Declare the settings once per device — the service pushes them as downlinks
whenever the device's own report (every fPort-2 uplink carries
`acc_threshold` / `acc_frequency` / `acc_duration`) differs, and immediately
when `devices.json` is edited and hot-reloaded (ChirpStack queues the downlink
until the device's next uplink, so sleeping Class A devices are fine):

```json
"devices": {
  "ac1f09fffe1003ae": {
    "name": "sensor3",
    "threshold": 8,
    "frequency": 10,
    "duration": 1
  }
}
```

Keys: `threshold` (1–127), `duration` (0–127), `frequency` (Hz / code / index,
see above) — the settings of the standard (`GENERIC`/RAK) profile; a profile
registered with other `SettingSpec`s defines its own keys. `command` (port 4) is
one-shot only — not from the config. Devices whose `kind` is not in the
profile's `settings_kinds` (e.g. `door`) are never auto-pushed. Unknown
keys (e.g. a `threshol` typo) are ignored with a warning at load. Keys can also
go in `defaults` / `families` to apply fleet-wide. Set `family` (device profile
name, e.g. `RAK4631`/`RAK3172`) on the device entry so the CLI can translate
`frequency` without `--family`; the live service takes the family from each
uplink's device profile instead.

Console status on each sensor uplink (integers without `.0`, frequency in Hz
with the wire code/index in parens):

```
    acc settings: threshold=2 want 8 -> sending, frequency=100 Hz (code 95) want 10 Hz (code 47) -> sending, duration=1 ok
```

Read-back: every settings downlink sent by the service (auto, config-reload,
MQTT command)
is remembered until a sensor uplink reports that value in `acc_threshold` /
`acc_frequency` / `acc_duration`; the service then logs `downlink OK <<<` and
a `kind="downlink_ok"` row. A `downlink` row without a later `downlink_ok`
means the device has not applied it (frame not delivered, or rejected by the
firmware). Query: `select ts, device_name, kind, detail from events where
kind like 'downlink%' order by ts desc`.

Resend limits: a queued downlink is only delivered after the device's next
uplink, which still reports the old value, so the same value is not re-sent
within `DL_RESEND_S` (1 h) and is given up on after `DL_MAX_ATTEMPTS` (5) sends
without the device reporting it back (e.g. firmware rejects the value). A
config reload resets both. A failed publish is logged and retried on the next
uplink; it never suppresses alarm notifications.

### 2. One-shot per device (CLI or MQTT)

Both log one `kind="downlink"` row per frame to the events table:

```bash
# CLI, one-shot (resolves --dev by devEUI or configured name); with no --set/--raw
# it pushes the device's configured threshold/frequency/duration:
.venv/bin/python3 -m loralarm.downlink --mqtt <lora-box> --config devices.json \
    --dev sensor3                       # push what devices.json says, now
.venv/bin/python3 -m loralarm.downlink --mqtt <lora-box> --config devices.json \
    --dev sensor16 --set frequency=50 --set threshold=16 [--confirmed] [--dry-run]
# raw frame:
.venv/bin/python3 -m loralarm.downlink --mqtt <lora-box> --config devices.json \
    --dev ac1f09fffe12da63 --raw --fport 6 --value 002

# live service: subscribes loralarm/down/<devEui-or-name> (--dl-topic),
# payload {"threshold":16,"frequency":50} or {"port":6,"value":"002"}:
mosquitto_pub -h <lora-box> -t 'loralarm/down/sensor16' -m '{"frequency": 50}'
```

## Next steps toward fleet dashboards (spec §7/§9)

1. ChirpStack REST registry cache (devEUI → name/profile) at startup + interval refresh.
2. ~~Downlinks via ChirpStack MQTT downlink topic~~ — done, see above (`loralarm/downlink.py`).
3. Status page (FastAPI, spec §9) reading the events SQLite log; trigger
   downlinks over `loralarm/down/<device>`.
4. Optional: hysteresis/debounce on temp and battery thresholds to avoid flapping
   at the boundary.
