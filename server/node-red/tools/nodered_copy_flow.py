#!/usr/bin/env python3
"""
nodered_copy_flow.py -- copy a Node-RED sensor flow template and rebrand it
for a new sensor number.

Templates (in ../components/) are normalized to sensor number 1:
dashboardName "Sensor1_Sensorer", tab "Sensor1", deviceName "sensor1",
ntfy topic "sensor1".  This script copies such a template and renames
everything to the target sensor number, regenerates the ids of all nodes
except shared global config nodes, and remaps every id reference
(wires, links, tabs/groups, subflow definitions and their instances), so
the output can be imported into a running instance without id conflicts.
It can also override per-deployment settings (MQTT broker, ntfy,
ChirpStack, TTN application id).

Usage:
    python3 nodered_copy_flow.py <input.json> <output.json> --to 2
    python3 nodered_copy_flow.py ../components/flow_sensor_rui.json \\
        flow_sensor2_rui.json --to 2 --broker 192.168.2.26

Afterwards, import the output file in Node-RED and fill the sensor's
per-instance values in the subflow env: the RAK4631/Android flow reads
all per-sensor settings (devEui, deviceName, dashboardName,
ttnDeviceName, tilt_sense, Tk, ntfyChannel, pushover) from the
subflow instance env.  The RUI flow has no such env values; it reads
devEui/deviceName/dashboardName at flow start from the sensorConfig
registry (components/flow_inputs.json).

Known limitation: the script updates neither the sensorConfig registry
in components/flow_inputs.json nor any subflow env value -- they are
entered per sensor in the editor after import.
"""

import argparse
import json
import random

# Global config node types: shared across flows and instances, so their ids
# are kept.  The output then reuses the existing config nodes on import (or,
# with "Import copy", gets its own copies).  Everything else -- including
# z-scoped config nodes like "device downlink" -- is referenced via wires and
# gets a fresh id.
CONFIG_TYPES = {
    "mqtt-broker",
    "pushover-keys",
    "global-config",
}

# Per-deployment defaults.  Values that are None keep the template value.
DEFAULTS = {
    "broker": "192.168.2.26",        # local MQTT broker (TTN broker is never touched)
    "broker_port": "1883",
    "ntfy_base": "127.0.0.1:8075",   # self-hosted ntfy, used as ntfy_base/<topic>
    "chirpstack_base": "localhost:8080",  # ChirpStack API for downlinks
    "ttn_app": "larm-test@ttn",      # TTN application id used in MQTT topics
}


def generate_uid():
    """Node-RED style id: 16 lowercase hex chars."""
    return "%016x" % random.getrandbits(64)


def remap_nid(nid, id_map):
    """Map an old node id to its new id (unchanged for kept config ids)."""
    return id_map.get(nid, nid)


def regenerate_ids(flows):
    """Give every non-config node a fresh id and remap all references.

    Remapped references: node ids, wires, z (flow tab / subflow definition),
    g (editor groups), ui_group.tab, ui_*.group, link in/out links,
    subflow definition in/out ports, and subflow instance node types.
    Returns the number of regenerated ids.
    """
    id_map = {}
    for item in flows:
        if item.get("type", "") in CONFIG_TYPES:
            continue
        id_map[item["id"]] = generate_uid()

    for item in flows:
        t = item.get("type", "")
        item["id"] = remap_nid(item["id"], id_map)
        if "z" in item:
            item["z"] = remap_nid(item["z"], id_map)
        if "g" in item:
            item["g"] = remap_nid(item["g"], id_map)
        if t.startswith("subflow:"):
            item["type"] = "subflow:" + remap_nid(t.split(":", 1)[1], id_map)
        if t == "ui_group" and "tab" in item:
            item["tab"] = remap_nid(item["tab"], id_map)
        if t.startswith("ui_") and "group" in item:
            item["group"] = remap_nid(item["group"], id_map)
        if t in ("link in", "link out") and isinstance(item.get("links"), list):
            item["links"] = [remap_nid(link, id_map) for link in item["links"]]
        if t == "subflow":
            for port in item.get("in", []) + item.get("out", []):
                for wire in port.get("wires", []):
                    if "id" in wire:
                        wire["id"] = remap_nid(wire["id"], id_map)
        if isinstance(item.get("wires"), list):
            for port in item["wires"]:
                if isinstance(port, list):
                    for i, target in enumerate(port):
                        port[i] = remap_nid(target, id_map)
    return len(id_map)


def apply_broker_settings(flows, broker, port):
    """Set address/port on local mqtt-broker config nodes (never TTN)."""
    updated = 0
    for item in flows:
        if item.get("type") != "mqtt-broker":
            continue
        if "thethings.network" in item.get("broker", ""):
            continue  # TTN broker is deployment independent
        item["broker"] = broker
        item["port"] = port
        updated += 1
    return updated


def build_replacements(from_num, to_num, overrides):
    rules = [
        ("Sensor{0}_Sensorer".format(from_num), "Sensor{0}_Sensorer".format(to_num)),
        ("Sensor{0}".format(from_num), "Sensor{0}".format(to_num)),
        ("sensor{0}".format(from_num), "sensor{0}".format(to_num)),
    ]
    if overrides["ntfy_base"]:
        rules.append((DEFAULTS["ntfy_base"], overrides["ntfy_base"]))
    if overrides["chirpstack_base"]:
        rules.append((DEFAULTS["chirpstack_base"], overrides["chirpstack_base"]))
    if overrides["ttn_app"]:
        rules.append((DEFAULTS["ttn_app"], overrides["ttn_app"]))
    # Longest match first so e.g. "Sensor1_Sensorer" is replaced before "Sensor1".
    return sorted(rules, key=lambda r: len(r[0]), reverse=True)


def main():
    parser = argparse.ArgumentParser(
        description="Copy a Node-RED sensor flow template (sensor 1) "
                    "and rebrand it for another sensor number.")
    parser.add_argument("input", help="template flow JSON, e.g. ../components/flow_sensor_rui.json")
    parser.add_argument("output", help="output flow JSON to import in Node-RED")
    parser.add_argument("--from", dest="from_num", type=int, default=1,
                        help="sensor number in the template (default: 1)")
    parser.add_argument("--to", dest="to_num", type=int, required=True,
                        help="sensor number for the new flow, e.g. 2")
    parser.add_argument("--broker", default=DEFAULTS["broker"],
                        help="local MQTT broker address (default: %(default)s)")
    parser.add_argument("--broker-port", default=DEFAULTS["broker_port"],
                        help="local MQTT broker port (default: %(default)s)")
    parser.add_argument("--ntfy-base", default=DEFAULTS["ntfy_base"],
                        help="ntfy address:port (default: %(default)s)")
    parser.add_argument("--chirpstack-base", default=DEFAULTS["chirpstack_base"],
                        help="ChirpStack API address:port (default: %(default)s)")
    parser.add_argument("--ttn-app", default=DEFAULTS["ttn_app"],
                        help="TTN application id (default: %(default)s)")
    args = parser.parse_args()

    with open(args.input, "r") as f:
        flows = json.load(f)

    n_ids = regenerate_ids(flows)
    n_brokers = apply_broker_settings(flows, args.broker, args.broker_port)

    # Serialize once with indentation so the output is reviewable/diffable,
    # then apply the string replacements on the text.
    data = json.dumps(flows, ensure_ascii=False, indent=2)

    rules = build_replacements(args.from_num, args.to_num, vars(args))
    for old, new in rules:
        count = data.count(old)
        if count == 0:
            print("WARNING: '{}' not found in template".format(old))
        elif old != new:
            print("replaced '{}' -> '{}': {} occurrence(s)".format(old, new, count))
        data = data.replace(old, new)

    json.loads(data)  # fail loudly instead of writing broken JSON

    with open(args.output, "w") as f:
        f.write(data)

    print("wrote {} ({} nodes, {} node ids regenerated, {} broker(s) set to {}:{})".format(
        args.output, len(flows), n_ids, n_brokers, args.broker, args.broker_port))
    print("NOTE: fill the per-sensor values (devEui etc.) in the imported" +
          " subflow instance env; RUI flows read them from" +
          " components/flow_inputs.json at flow start.")


if __name__ == "__main__":
    main()
