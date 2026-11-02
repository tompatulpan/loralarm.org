# tools/

Helper scripts for maintaining the Node-RED flows.

## nodered_copy_flow.py

Copies a sensor flow template (in `../components/`) and rebrands it for a
new sensor number. Use it when you deploy a new sensor and need a matching
Node-RED flow with its own dashboard tab.

### Usage

```bash
# new sensor 2, all deployment defaults from the script header
python3 nodered_copy_flow.py ../components/flow_sensor_rui.json flow_sensor2_rui.json --to 2

# new sensor 3 with deployment overrides
python3 nodered_copy_flow.py ../components/flow_sensor_android_rak4631.json \
    flow_sensor3_android.json --to 3 \
    --broker 192.168.2.26 --broker-port 1883 \
    --ntfy-base 127.0.0.1:8075 \
    --chirpstack-base localhost:8080 \
    --ttn-app larm-test@ttn
```

Then import the output file in Node-RED (Menu → Import).

### What it does

1. Loads the template and regenerates the ids of all `ui_tab` / `ui_group`
   nodes (and every reference to them), so the new flow gets its own
   dashboard tab and groups instead of colliding with the template's.
2. Sets address/port on the local `mqtt-broker` config nodes. The TTN
   broker (`eu1.cloud.thethings.network`) is never touched.
3. Applies string replacements, longest match first:

   | Template value (sensor 1)   | Becomes (sensor N)    |
   |-----------------------------|-----------------------|
   | `Sensor1_Sensorer` (env `dashboardName`) | `SensorN_Sensorer` |
   | `Sensor1` (tab name)        | `SensorN`            |
   | `sensor1` (deviceName, ntfy topic `127.0.0.1:8075/sensor1`) | `sensorN` |
   | `127.0.0.1:8075` (ntfy)     | value of `--ntfy-base` |
   | `localhost:8080` (ChirpStack downlink API) | value of `--chirpstack-base` |
   | `larm-test@ttn` (TTN application id) | value of `--ttn-app` |

4. Writes the result with `indent=2` so the file stays reviewable in git.
   The script warns when a replacement pattern matches nothing, and
   refuses to write output that is not valid JSON. One warning is
   expected: `Sensor1_Sensorer` does not occur in
   `flow_sensor_rui.json` (the RUI template has no `dashboardName` env).

### After running the script (manual steps)

The script does **not** complete the deployment on its own:

- **Register the sensor** in `../components/flow_inputs.json`
  (`devEui`, `deviceName`, `dashboardName`, `hasSettings`). Settings
  uplinks/downlinks are keyed by that registry, so a sensor missing there
  will not get its settings panels. (Extending the script to update the
  registry automatically is a possible future improvement.)
- **Set the per-sensor secrets/values** in the imported flow (subflow
  env): `devEui`, `ttnDeviceName` and pushover `pushoverToken` /
  `pushoverUser` ship empty in the templates.
- **Point the ntfy channel** at the topic your clients subscribe to
  (`ntfyChannel` env, default `127.0.0.1:8075/sensorN` after the rename).
