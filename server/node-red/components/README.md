# components/

Node-RED flow components for the LoRaLarm server.

## Flow files

- **flow_sensor_rui.json** — dashboard + alarm flow template for a sensor
  running the RUI3 firmware (`devices/rui3_sensor`). Normalized to
  **sensor 1**: tab `Sensor1`, deviceName `sensor1`, ntfy topic
  `127.0.0.1:8075/sensor1`, group names `Sensorer` / `Statuspanel`.
  Includes pushover (`pushover-keys` node holds no secrets in this file)
  and the self-hosted ntfy node `ntfy local`.
- **flow_sensor_android_rak4631.json** — dashboard + alarm flow template
  for an Android sensor on RAK4631 hardware
  (`devices/android_rak4631`). Also normalized to **sensor 1**; the
  per-sensor values live in the `Sensor (RAK4631)` subflow env
  (`deviceName`, `devEui`, `dashboardName`, `ntfyChannel`, ...).
- **flow_inputs.json** — settings registry: maps devEUI to
  `deviceName` / `dashboardName` for all deployed sensors (currently
  sensor3–16 plus doors/water). Settings uplinks/downlinks are keyed by
  this registry.
- **flow_EUI_key_generator.json** — helper UI that generates EUI/keys
  for onboarding a new device in ChirpStack/TTN.

## Deployment settings (`LORALARM_*` env)

Values that differ per installation are **not** hardcoded in the nodes.
Each flow file carries them in its `global-config` node (Node-RED menu →
Settings → Environment after import) and nodes reference them as
`${LORALARM_...}` (or `env.get(...)` in function nodes). Node-RED only
substitutes a property that is *exactly* `${VAR}`, so every variable holds
a complete value (e.g. a full file path, not just a directory).

| Variable | Default in the templates | Used by |
|---|---|---|
| `LORALARM_LOG_ALARMS` | `/var/log/loralarm/log_alarms.csv` | alarm log (`file`), monthly delete and download in `flow_inputs.json` |
| `LORALARM_LOG_TX` | `/var/log/loralarm/log_tx.csv` | TX log, monthly delete and download |
| `LORALARM_CS_BROKER` | `192.168.2.26` | local ChirpStack MQTT broker (`mqtt-broker` node "Lora Server") |
| `LORALARM_CS_API` | `localhost:8080` | ChirpStack API for `device downlink` nodes |
| `LORALARM_TTN_BROKER` | `eu1.cloud.thethings.network` | TTN MQTT broker |
| `LORALARM_TTN_APP` | `larm-test@ttn` | TTN downlink topics (function nodes) |
| `LORALARM_TTN_UP_TOPIC` | `v3/larm-test@ttn/devices/+/up` | TTN uplink subscription -- must match `LORALARM_TTN_APP` |
| `LORALARM_NTFY_BASE` | `127.0.0.1:8075` | RUI flow: the `ntfy url` node builds `<base>/<deviceName>` |
| `LORALARM_LAT` / `LORALARM_LON` | Stockholm | `time-switch` node (sunrise/sunset) |

The log directory must exist and be writable by the Node-RED user
(`createDir` is off). The Android flow's per-sensor ntfy URL stays in its
`ntfyChannel` subflow env. The ChirpStack join listener in
`flow_inputs.json` subscribes to all applications and only acts on the
one in the `application.sublarm` registry entry.

## Persisting sensor settings across restarts (context storage)

All runtime sensor state lives in Node-RED **context**, in the store
named `default`:

- `global.sensorSettings` — per-sensor settings
  (`interrupt_threshold`, `acc_frequency`, `interrupt_duration`),
  keyed by devEUI; written by the settings UI and re-sent on join by
  `resend settings on join` in `flow_inputs.json`.
- `global.sensorConfig` and `global.application` — the devEUI →
  deviceName registry and app name.
- flow context (`dashboardName`, `tilt_sense`, ...) — per-sensor-tab
  state.

Node-RED's built-in `default` store is **memory-only**: after a power
cycle or restart everything above is lost and `init sensorSettings
defaults` starts from empty. To make the settings memory persistent,
configure a file-backed context store in the server's `settings.js`
(usually `~/.nodered/settings.js`, or the file your systemd service
points to via `NODE_RED_SETTINGS`):

```js
contextStorage: {
    default: {           // must be named "default": the flows address
        module: "localfilesystem",   // that store explicitly
        config: {
            dir:  "/var/lib/node-red/context", // writable by Node-RED user
            cache: false    // synchronous writes: survive abrupt power loss
        }
    }
}
```

- The store key must be exactly `default` — function nodes pass the
  store name explicitly (`global.set("sensorSettings", s, "default")`),
  so a persistent store under any other name is ignored.
- `cache: false` writes every change to disk immediately; `cache: true`
  (the module default) batches writes every ~30 s and only flushes on a
  *clean* shutdown, so the last changes are lost on a power cut.
- The context directory (`~/.node-red/context` if `dir` is unset) must
  exist and be writable by the Node-RED user. Treat it like the flows
  file: back it up, and do not commit it (it may contain devEUIs).
- Verify after configuring: restart Node-RED, change a sensor setting,
  restart again and confirm the change survived.

## Conventions

- Templates always use sensor number 1 (`Sensor1` / `Sensor1_Sensorer` /
  `sensor1`). Use `../tools/nodered_copy_flow.py` to create a flow for
  another sensor number.
- ntfy topic is `sensorN` on the self-hosted instance
  (`127.0.0.1:8075`).
- Hardware naming follows `devices/`: RAK4631 for the Android sensor.

## Secrets / threat model

- No credentials are stored in these files: pushover token/user, MQTT
  passwords and per-sensor `devEui` / `ttnDeviceName` are empty and live
  in Node-RED's credential store or are set on import. (The Android
  template originally exported with a live pushover token in the
  subflow instance env — removed; rotate the token if it was ever
  shared elsewhere.)
- However, **identifiers are committed on purpose**: real devEUIs and
  device names in `flow_inputs.json`, the TTN application id
  (`larm-test@ttn`) and broker addresses inside the flows. Treat them as
  non-secret but identifying data: anyone reading this repo can see how
  many sensors exist and which LoRa network/application they belong to.
  Do not commit credential files (`flows_cred.json`, settings secrets).
