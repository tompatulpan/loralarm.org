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

Note: with multi-line commands, every line except the last must end with `\`
and nothing may follow it (not even a space) -- otherwise the shell runs the
first line alone and treats the remaining lines as a new command.

Then import the output file into Node-RED as described below.

### Importing into a running instance

Workflow in the editor:

1. Right-click on a flow in the flows sidebar → **Insert** → **Import**.
2. Select the generated file (e.g. `flow_sensor2_rui.json`).
3. Import into a **new flow** and confirm.
4. Node-RED warns that "some of the nodes you are importing already
   exist" -- click **Import copy**.
5. **Deploy**.

The warning is expected: only the `ui_tab` / `ui_group` ids are
regenerated, every other node in the output still carries the template's
ids, so with a live flow built from the same template about 330 of the
334 nodes are listed as duplicates. **Import copy** assigns fresh ids to
the duplicated nodes and leaves the live flow untouched. **View nodes**
only lists the conflicting nodes for inspection -- leave all of them
selected, do not remove any from the import. If your Node-RED version
offers a per-node choice, keep "copy" for everything and never pick an
option that keeps the original ids / replaces the existing node: the
live sensor's nodes share these ids, and replacing them would overwrite
the running flow with the new sensor's content (sensorConfig refs, ntfy
URL, tab labels, UI widgets).

After Deploy, the new flow brings its own copies of the shared config
nodes (`mqtt-broker` x2, `pushover api`, `pushover-keys`, the ChirpStack
`device downlink` node, the subflow definition). That works as-is -- the
only cost is extra MQTT client connections. To keep the instance tidy,
open the configuration-nodes sidebar, delete the *new* copies and
re-point the new flow's nodes (mqtt in/out, pushover, device downlink,
subflow instances) to the existing live config nodes. Never delete the
live copies, and note the live `pushover-keys` holds the real API keys
while the imported copy ships empty.

Then continue with the manual steps below.

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
   expected: `Sensor1_Sensorer` does not occur in `flow_sensor_rui.json`.
   It is harmless -- the dashboard show/hide calls address groups as
   `TabName_GroupName`, so the registered `dashboardName`
   (e.g. `Sensor2_Sensorer`) already resolves to tab `Sensor2` + group
   `Sensorer`; the group itself must stay named `Sensorer`.

### After running the script (manual steps)

The script does **not** complete the deployment on its own:

- **Register the sensor** in `../components/flow_inputs.json`
  (`devEui`, `deviceName`, `dashboardName`, `hasSettings`). Settings
  uplinks/downlinks are keyed by that registry, so a sensor missing there
  will not get its settings panels. (Extending the script to update the
  registry automatically is a possible future improvement.)

  **Mind the injection order.** Each sensor flow copies its registry
  entry into its own flow context exactly once, ~5 s after the flow
  starts (the `init` inject wired to the change node reading
  `sensorConfig.sensorN.*`). If the registry did not contain the sensor
  at that moment (flow imported before registering, or the registry
  edited later), `flow.devEui` stays undefined and the flow **silently
  drops every uplink** -- no error, no output. After changing the
  registry: inject the `flow_inputs` config first, then press the
  sensor flow's `init` inject (or re-deploy that flow), and check that
  `devEui` is set in the flow's context data (sidebar → context).

  ToDo: automate this -- e.g. let the sensor flow re-read its
  `sensorConfig` entry on every uplink, or on a registry-change
  notification, instead of once at startup, so a missing or mistimed
  injection can never leave a flow silent.
- **Set the per-sensor secrets/values** in the imported flow (subflow
  env): `devEui`, `ttnDeviceName` and pushover `pushoverToken` /
  `pushoverUser` ship empty in the templates.
- **Point the ntfy channel** at the topic your clients subscribe to
  (`ntfyChannel` env, default `127.0.0.1:8075/sensorN` after the rename).
