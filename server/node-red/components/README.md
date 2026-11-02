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
