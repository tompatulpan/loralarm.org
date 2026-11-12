# examples/

Copy-and-edit starting points, one per device type. **The service never reads
these files**; your real config is `server/python/devices.json`.

| File | Device type |
|---|---|
| `rak3172.json` | RAK3172 (RUI3 / Cayenne) |
| `rak4631.json` | RAK4631 (Android, LiPo) |
| `dragino_lds02.json` | Dragino LDS02 door switch |
| `milesight_ws101.json` | Milesight WS101 smart button (short press = ON, long press = OFF) |

Each file starts with a `"_readme"` field that explains its keys (JSON has no
comments, and the config loader ignores that field).

## Adding a device from an example

Each example has two devices and a `"families"` section: the settings that are
the same for every device of the type are written **once** there, and each
device only lists what is its own. To add a third device of that type, copy
the *smallest* entry (the second one) and change its devEUI, name and topic.

1. Open the example and copy a device entry under `"devices"` (the block that
   starts with the devEUI). The first device shows the normal case, the second
   one shows a minimal entry with one or two overrides.
2. Paste it into the `"devices"` object of `devices.json`, next to your other
   devices. Mind the comma between entries.
3. Change in the pasted entry:
   - the key: your sensor's devEUI, lowercase, 16 hex characters (ChirpStack →
     Devices);
   - `name`: what shows up in notifications and the event log;
   - `ntfy_topic`: the topic you subscribe to for this device;
   - `family`: leave it, unless your ChirpStack device profile has another name
     (it must match the profile name or one of the aliases of a type).
4. The shared `"families"` section: copy it into `devices.json` too, **once**
   per type (or put those keys in your own `"families"`). Without it the
   device gets only the type's built-in defaults.
5. Make sure the secrets exist in the environment the service runs in:
   `LORALARM_PUSHOVER_USER`, `LORALARM_PUSHOVER_TOKEN` and, for RAK devices
   that get downlinks, the ChirpStack application ID the entry refers to
   (`LORALARM_CS_APP_LARM` / `LORALARM_CS_APP_SUBLARM`). A missing variable is
   logged and becomes an empty value.
6. Save. `devices.json` is hot-reloaded: a broken edit is logged and the
   previous config stays active. Check the log for `reloaded config`.
7. Trigger the device (open the door, shake the sensor) and watch ntfy.

Good to know: `application_id` is set per device, not in `"families"`, because
devices of one type can live in different ChirpStack applications (the loader
warns if you put it in a shared layer). A dict such as `notify_fire` is
replaced as a whole by a more specific layer, not merged key by key.

## Trying an example without touching your config

```bash
cd server/python
.venv/bin/python3 -m loralarm.replay ../chirpstack/fixtures/*.json --config examples/rak4631.json
```

This replays the captured real uplinks through the example (dry, nothing is
sent) and prints what would alarm. Use `python3 -m loralarm.downlink --config
examples/rak4631.json --dev sensor3 --app-id X --dry-run` to see the settings
frames an entry would push.

## Only a few keys are needed

A minimal entry is just a name and a family; the type's defaults fill the rest:

```json
"a84041a5d7598dd0": {"name": "Door1", "family": "Dragino Switch LDS02", "ntfy_topic": "door1"}
```

Add keys only where this device should differ from its type. To change
something for *all* devices (a topic, Pushover keys, a watchdog time), put it in
`"defaults"`; for all devices of one type, in `"families"."<type>"`. Order of
precedence and the full list of keys: the main
[README](../README.md#adding-a-device-or-sensor).
