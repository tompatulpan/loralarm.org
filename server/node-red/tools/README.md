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

# new sensor 4 from the unified template (RAK4631 custom frame and RUI3 Cayenne)
python3 nodered_copy_flow.py ../components/flow_sensor_unified.json flow_sensor4_unified.json --to 4

# without the subflow definitions: the new flow reuses the subflows
# already present on the instance (instances keep the template def ids)
python3 nodered_copy_flow.py ../components/flow_sensor_rui.json \
    flow_sensor2_rui.json --to 2 --no-subflows

# bulk copy as ONE import file for the FIRST sensor batch on an
# instance: canonical subflow definitions (template ids) + the sensors
# in the same file; the {n} in the output path becomes the number or
# range.  Import once; in the import dialog deselect the nodes flagged
# as already existing.  See "Subflows and the deployment workflow".
python3 nodered_copy_flow.py ../components/flow_sensor_unified.json \
    'flow_sensor{n}_unified.json' --to-range 2-16 --with-subflows

# later batches on the same instance (subflows already installed):
# plain --no-subflows files -- mistake-proof, see workflow section
python3 nodered_copy_flow.py ../components/flow_sensor_unified.json \
    'flow_sensor{n}_unified.json' --to-range 17-32 --no-subflows
```

Note: with multi-line commands, every line except the last must end with `\`
and nothing may follow it (not even a space) -- otherwise the shell runs the
first line alone and treats the remaining lines as a new command.

### Subflows and the deployment workflow

Node-RED resolves subflow instances by **id**, never by name, and an
instance that is imported *before* its definition exists in the editor
becomes `unknown: subflow` **forever** -- even after the definition
arrives, the already-imported nodes stay broken and must be deleted and
re-imported. That import order is exactly what goes wrong when the
subflow definitions and the sensor copies travel in separate files (and
"Import copy" additionally regenerates definition ids, another way to
break the reference).

The fix: `--with-subflows` puts the canonical subflow definitions and
all sensor copies of the run into **one file, one import**:

```bash
python3 nodered_copy_flow.py ../components/flow_sensor_unified.json \
    'flow_sensor{n}_unified.json' --to-range 2-16 --with-subflows
```

Use the bundle for the **first** batch on an instance (or when the
canonical subflow definitions may be missing). Then:

* The bundle contains the subflow definitions with their **template
  ids, not regenerated** (plus their inner nodes and the shared config
  nodes) and then one flow tab per sensor, each with fresh node ids and
  referencing the template subflow ids.
* In the import dialog, **deselect every node flagged as "already
  exists"** and import the rest. On a first batch those are only the
  shared config nodes (`mqtt-broker` x2, `global-config`).
* **Never "Import copy" this file**: Node-RED then imports the
  definitions as *new copies*, renamed `name (2)` (and further `(3)` …
  on every repeated mistake), and its automatic duplicate detection
  never matches a renamed copy again -- the copies pile up. The import
  itself stays functional (Node-RED re-points the instances of that one
  import consistently), but every batch imported that way leaves
  another pair of subflow copies behind.

**Subsequent batches on the same instance: plain `--no-subflows`
files.** Once the canonical subflows are installed (first bundle
import), do not ship definitions again:

```bash
python3 nodered_copy_flow.py ../components/flow_sensor_unified.json \
    'flow_sensor{n}_unified.json' --to-range 17-32 --no-subflows
```

These files are **mistake-proof**: they contain no subflow
definitions, so no import-dialog choice can duplicate one. Node-RED
resolves the instance references against the live definitions either
way; if "Import copy" is clicked, only the shared config nodes
(`mqtt-broker` etc.) are duplicated -- cosmetic, deselect them to
avoid it. The script prints the required definition ids before
writing, so a missing definition is diagnosed before the import.

**Cleanup when subflow copies crept in** (e.g. from an "Import copy"
on a bundle): delete the sensor tabs of that batch; the copy-renamed
subflow definitions then have no instances left in use and can be
deleted in the flows sidebar (Node-RED refuses to delete definitions
that are still referenced -- delete the tabs first). The canonical
definitions (referenced by the healthy tabs) must stay. Then re-import
the batch as plain `--no-subflows` files.

Re-importing sensors later (e.g. after deleting a tab) works the same
way: a plain `--to N --no-subflows` run, import, done.

The plain `--no-subflows` mode (copies only, referencing the template
definitions ids) stays available for instances that already carry the
canonical subflows; the script prints the required definition ids
before writing. `--subflows-only` writes just the canonical subflow
definitions (+ inner nodes, + config nodes, ids unchanged) as an
optional pre-install helper. `--subflow-map OLD=NEW` (repeatable,
implies `--no-subflows`) is the legacy escape hatch for instances whose
subflow definitions carry regenerated ids: it re-points the subflow
instances from the template definition id `OLD` to a live definition id
`NEW`. Prefer the bundle workflow instead:

```bash
python3 nodered_copy_flow.py ../components/flow_sensor_rui.json \
    flow_sensor2_rui.json --to 2 \
    --subflow-map 80204cbf6e8db977=<live-alarm-counter-v2-id>
```

Cleanup note: if sensor flows were previously imported while their
subflow definition was missing, those tabs contain permanently broken
`unknown: subflow` nodes -- delete them and re-import from a
`--with-subflows` bundle (first batch) or plain `--no-subflows` output
(subsequent batches).

Then import the output file into Node-RED as described below.

### Importing into a running instance

Workflow in the editor:

1. Right-click on a flow in the flows sidebar → **Insert** → **Import**.
2. Select the generated file (e.g. `flow_sensor2_rui.json`).
3. Import into a **new flow** and confirm.
4. Node-RED warns that "some of the nodes you are importing already
   exist" -- click **Import copy**.
5. **Deploy**.

The warning is expected and small: the script regenerates the ids of all
nodes (including the subflow definitions and every wire/link/group
reference), but the shared global config nodes keep their template ids
so an import can reuse the existing ones. Only those config nodes are
listed as duplicates (typically `mqtt-broker` x2, `global-config`, plus
`pushover-keys` for the RUI flow). **Import copy** gives the new flow its
own copies; **View nodes** only lists the conflicts for inspection --
leave all of them selected. Never pick an option that keeps the original
ids / replaces the existing node for *flow* nodes: with this script
there should be no flow-node conflicts at all -- if you see many, you
are importing a raw template, not script output.

The templates' own `ui_tab`/`ui_group` ids are unique to the templates
(they were re-generated once when live flows were found still carrying
them), so an as-is template import no longer hides behind a live
sensor's dashboard tab. Importing a raw template into a running instance
is still not recommended: the flow-node and config-node ids collide with
live flows and need "Import copy", and you get duplicate config nodes.
On a running instance always import script output. For a demo sensor1:
run the script with `--to 1`.

After Deploy, everything in the new flow except the shared global config
nodes is independent by design: the subflow definitions, the ChirpStack
`device downlink` node and all flow nodes got fresh ids, so there is
nothing to clean up or re-point there. With **Import copy**, only the
kept config nodes (`mqtt-broker` x2, `pushover-keys` on RUI flows) are
duplicated -- that works as-is; the only cost is an extra MQTT client
connection per broker. To keep the instance tidy, delete the *new*
broker copies and re-point the flow's MQTT nodes to the existing live
brokers. For `pushover-keys` either keep the imported (empty) copy and
fill it per sensor, or leave it out and re-point to the live copy that
holds the real API keys. Never delete the live copies.

Then continue with the manual steps below.

### What it does

1. Loads the template and regenerates the ids of **all** nodes except
   shared global config nodes (`mqtt-broker`, `pushover-keys`,
   `global-config`), remapping every reference: wires, link-node pairs,
   flow tab, editor groups (including the group membership lists),
   node scope lists (catch/status/complete -- the templates currently
   use none), ui_tab/ui_group and their widget references, and the
   subflow definitions together with their instance types. The
   output therefore collides with nothing in a running instance except
   the few shared config nodes. With `--no-subflows` (or
   `--subflow-map`, see above) the subflow definitions and their inner
   nodes are dropped instead, and the instances reference the existing
   subflows by their original ids.
2. Sets the port on the local `mqtt-broker` config nodes. The TTN broker
   is never touched. The broker address, ChirpStack API and TTN
   application are the `LORALARM_*` variables of the flow file's
   `global-config` node (see `../components/README.md`); the script
   writes `--broker`, `--chirpstack-base` and `--ttn-app` (also the
   derived `LORALARM_TTN_UP_TOPIC`) there.
3. Renames the sensor number in one regex pass (so a `--to` number that
   starts with the template's digits, like 10 or 11, renames exactly
   once), plus one literal replacement:

   | Template value (sensor 1)   | Becomes (sensor N)    |
   |-----------------------------|-----------------------|
   | `Sensor1` / `sensor1` (tab name, deviceName, ntfy topic, env `dashboardName`) | `SensorN` / `sensorN` |
   | `127.0.0.1:8075` (`LORALARM_NTFY_BASE`, Android `ntfyChannel`) | value of `--ntfy-base` |

   The subflow definitions keep their template names (`Sensor (RAK4631)
   v2`, `Sensor (RAK4631 + RUI3) v3`, `Alarm counter v2`) -- only their
   ids are regenerated.

4. Writes the result with `indent=2` so the file stays reviewable in git.
   The script warns when a replacement pattern matches nothing, and
   refuses to write output that is not valid JSON. `Sensor1_Sensorer`
   does not occur literally in
   `flow_sensor_rui.json` -- that is expected and needs no warning: the
   dashboard show/hide calls address groups as `TabName_GroupName`, so
   the registered `dashboardName` (e.g. `Sensor2_Sensorer`) already
   resolves to tab `Sensor2` + group `Sensorer`; the group itself must
   stay named `Sensorer`.

### After running the script (manual steps)

The script does **not** complete the deployment on its own:

- **The `application` global must be real** (both flow types): every
  uplink passes a switch on `global.application.larm`, so the Inputs
  flow's registry must set `larm` to the sensor's actual ChirpStack
  application UUID. The committed `components/flow_inputs.json` carries
  a placeholder UUID for `larm` (`11111111-...`) -- replace it before
  injecting, otherwise every sensor flow silently shows no data.
  (The unified flow picks the key with its `csApp` env: `larm` for
  RAK4631, `sublarm` for RUI3 sensors.)

- **Set the per-sensor values in the subflow instance env (RAK4631/
  Android and unified flows)**: all per-sensor settings -- `devEui`,
  `deviceName`, `dashboardName`, `ttnDeviceName`, `tilt_sense`, `Tk`,
  `ntfyChannel` and pushover `pushoverToken` / `pushoverUser` -- are
  read from the subflow instance env. The unified flow adds `protocol`
  (`auto` / `custom` / `cayenne`), `csApp`, `tempAlarmLimit` and the
  battery voltages `battFull` / `battWarn` / `battEmpty`; see
  `../components/README.md` for the values per sensor type. `devEui` ships empty and must be filled after
  import (it filters MQTT uplinks and keys sensorSettings), then
  **Deploy** -- env values only reach the flow context when the flow
  (re)starts; a running flow that never restarted after the env edit
  silently drops every uplink while the dashboard still renders.
  The filter is a **substring** match (`topic contains devEui`): a
  truncated EUI (dropped trailing characters) or wrong case will
  behave unexpectedly -- a truncation of the real EUI still matches
  (data keeps flowing), uppercase never matches. Always set the full
  16-character lowercase EUI from ChirpStack/TTN. There is no
  registry dependency in these flows.  **One sensor per flow tab**:
  the `init` change node inside the subflow copies the instance env
  (`devEui`, `deviceName`, `dashboardName`) into the *flow context of
  the tab the subflow is placed on* -- subflow nodes have no context of
  their own. Two sensor subflow instances on the same tab would
  overwrite each other's `flow.devEui`; keep each sensor on its own
  flow tab, as the templates do.
- **Register the sensor for RUI flows** in `../components/flow_inputs.json`
  (`devEui`, `deviceName`, `dashboardName`). The RUI flow has no env
  values; it copies its registry entry into flow context ~5 s after the
  flow starts (the `init` inject wired to the change node reading
  `sensorConfig.sensorN.*`) and re-reads it every 60 s. If the registry
  did not contain the sensor at that moment, `flow.devEui` is undefined
  and the flow **silently drops every uplink** -- no error, no output;
  a missed injection heals itself within a minute. For instant effect:
  inject the `flow_inputs` config first, then press the flow's `init`
  inject (or re-deploy), and check `devEui` in the flow's context data
  (sidebar → context). The startup cascade is tiered so the registry is
  written before the flows read it: `flow_inputs` injects at 1 s, the
  RUI flow loads its config at 5 s, the other init nodes (watchdog,
  connection supervision, dashboard labels) run at 10 s.
- **Settings defaults and resend** (both flow types) are keyed by the
  device EUI, not by the registry: settings are saved to the persistent
  `sensorSettings` global context on every settings downlink, and the
  `join events` chain in `flow_inputs` resends the saved values when a
  sensor rejoins (e.g. after battery loss). This silently does nothing
  unless the Node-RED instance has a persistent context store configured
  -- see the section below.
- **Point the ntfy channel** at the topic your clients subscribe to
  (`ntfyChannel` env, default `127.0.0.1:8075/sensorN` after the rename).

## Node-RED instance configuration: persistent context

The configuration memory is **server-side**: the sensor keeps nothing,
the flows save every settings downlink into the global context
(`global.set("sensorSettings", settings, "default")` -- note the explicit
store name `"default"`) and re-push it when the device rejoins. For this
to survive a Node-RED restart, the running instance's `settings.js`
(in the Node-RED userDir, e.g. `~/.node-red/settings.js`) must define a
file-backed store **named `default`**:

```js
contextStorage: {
    default: {
        module: "localfilesystem",
        // optional: shorter write-behind interval than the 30 s default
        config: { flushInterval: 10 }
    },
},
```

Node-RED must be restarted after the edit (settings.js is only read at
startup). Without this block, the built-in store is memory-only:
`sensorSettings` is wiped on every restart, the `join events` resend
finds nothing to send, and the sensor silently falls back to firmware
defaults after a restart or battery loss.

The store writes to `<userDir>/context/global/global.json`. Verify by
setting a sensor setting, restarting Node-RED, and checking that the
`sensorSettings` content is still there (editor sidebar → context data →
global, or the file above). Note that with the default `flushInterval` of
30 s, values written immediately before a crash may be lost.
