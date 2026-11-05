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
ChirpStack, TTN application id).  Those live as LORALARM_* variables in
the flow file's global-config env (referenced as ${LORALARM_...} by the
nodes), so the overrides edit that env and nothing else.

Usage:
    python3 nodered_copy_flow.py <input.json> <output.json> --to 2
    python3 nodered_copy_flow.py ../components/flow_sensor_rui.json \\
        flow_sensor2_rui.json --to 2 --broker 192.168.2.26

Subflow selection (default: the output includes the subflow definitions
with fresh ids, so every import gets its own subflow copies):
    --no-subflows   omit the subflow definitions and their inner nodes;
                    the subflow instances reference the ids of the
                    template definitions, which must already exist on
                    the target instance (e.g. import the raw template's
                    subflows once) - otherwise Node-RED imports the
                    instances as "unknown subflow".
    --subflow-map OLD=NEW   (repeatable, implies --no-subflows)
                    re-point the subflow instances from template
                    definition id OLD to the live definition id NEW;
                    use it when the target instance's subflows were
                    imported from script output and thus carry
                    regenerated ids.

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
import re

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
    g (editor groups), group membership lists ("nodes"), ui_group.tab,
    ui_*.group, link in/out links, node scope lists (catch/status/complete
    nodes -- empty scope means all nodes and needs no list), subflow
    definition in/out ports, and subflow instance node types.
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
        if t == "group" and isinstance(item.get("nodes"), list):
            # Group membership list.  Node-RED rebuilds it from each
            # member's "g" on import, but stale ids are wrong and risky.
            item["nodes"] = [remap_nid(nid, id_map) for nid in item["nodes"]]
        if isinstance(item.get("scope"), list):
            # Scope list of catch/status/complete nodes holds node ids
            # (or type strings, which pass through remap_nid unchanged).
            item["scope"] = [remap_nid(s, id_map) for s in item["scope"]]
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


def drop_subflow_definitions(flows):
    """Remove the subflow definitions and their inner nodes.

    Subflow instances (type "subflow:<id>") are kept and reference the
    definitions by their original (template) ids, which must already
    exist on the target instance.  Returns (kept_flows, subflow_ids).
    """
    subflow_ids = {item["id"] for item in flows if item.get("type") == "subflow"}
    kept = [item for item in flows
            if item.get("type") != "subflow"
            and item.get("z") not in subflow_ids]
    return kept, subflow_ids


def apply_subflow_map(flows, mapping):
    """Re-point subflow instances at other definition ids."""
    remapped = 0
    for item in flows:
        t = item.get("type", "")
        if t.startswith("subflow:"):
            old = t.split(":", 1)[1]
            if old in mapping:
                item["type"] = "subflow:" + mapping[old]
                remapped += 1
    return remapped


def apply_broker_port(flows, port):
    """Set the port on local mqtt-broker config nodes (never TTN).

    The broker address itself is the LORALARM_CS_BROKER env var."""
    updated = 0
    for item in flows:
        if item.get("type") != "mqtt-broker":
            continue
        if item.get("broker") == "${LORALARM_TTN_BROKER}":
            continue  # TTN broker is deployment independent
        item["port"] = port
        updated += 1
    return updated


def set_env(flows, name, value):
    """Set a variable in the global-config env of the flow file.

    Returns True when the template defines the variable."""
    for item in flows:
        if item.get("type") != "global-config":
            continue
        for var in item.get("env", []):
            if var.get("name") == name:
                var["value"] = value
                return True
    return False


def apply_env_overrides(flows, args):
    """Write the per-deployment settings into the global-config env."""
    values = {
        "LORALARM_CS_BROKER": args.broker,
        "LORALARM_CS_API": args.chirpstack_base,
        "LORALARM_TTN_APP": args.ttn_app,
        "LORALARM_TTN_UP_TOPIC": "v3/{0}/devices/+/up".format(args.ttn_app),
    }
    for name, value in values.items():
        if value and not set_env(flows, name, value):
            print("note: '{}' not used by this template".format(name))


def apply_replacements(data, from_num, to_num, ntfy_base):
    r"""Rename the sensor number and override the ntfy base on flow text.

    The sensor number is renamed in one regex pass: sequential string
    replacements double-apply whenever the target number starts with the
    source digits (e.g. --to 11 first turns Sensor1 into Sensor11 and a
    second rule would turn that into Sensor111).  The negative lookahead
    (?!\d) also keeps a template for sensor 1 from matching inside e.g.
    "sensor10".  re.sub with a function avoids backslash escaping in
    the replacement.
    """

    pattern = re.compile("(Sensor|sensor){0}(?!\\d)".format(from_num))
    n = len(pattern.findall(data))
    if n == 0:
        print("WARNING: 'Sensor{0}' not found in template".format(from_num))
    else:
        data = pattern.sub(lambda m: m.group(1) + str(to_num), data)
        print("renamed Sensor{0}/sensor{0} -> Sensor{1}/sensor{1}: "
              "{2} occurrence(s)".format(from_num, to_num, n))
    if ntfy_base:
        # Covers LORALARM_NTFY_BASE and the per-sensor ntfyChannel subflow env.
        n = data.count(DEFAULTS["ntfy_base"])
        if n == 0:
            print("WARNING: '{}' not found in template".format(DEFAULTS["ntfy_base"]))
        elif ntfy_base != DEFAULTS["ntfy_base"]:
            print("replaced '{}' -> '{}': {} occurrence(s)".format(
                DEFAULTS["ntfy_base"], ntfy_base, n))
            data = data.replace(DEFAULTS["ntfy_base"], ntfy_base)
    return data


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
    parser.add_argument("--no-subflows", action="store_true",
                        help="omit the subflow definitions and their inner "
                             "nodes; the subflow instances reference the "
                             "existing subflows (template definition ids)")
    parser.add_argument("--subflow-map", action="append", default=[],
                        metavar="OLD=NEW",
                        help="re-point subflow instances from template "
                             "definition id OLD to live definition id NEW "
                             "(repeatable; implies --no-subflows)")
    args = parser.parse_args()

    subflow_map = {}
    for pair in args.subflow_map:
        if "=" not in pair:
            parser.error("--subflow-map expects OLD=NEW, got '" + pair + "'")
        old, new = pair.split("=", 1)
        subflow_map[old] = new

    with open(args.input, "r") as f:
        flows = json.load(f)

    n_defs = 0
    if args.no_subflows or subflow_map:
        n_total = len(flows)
        flows, template_subflow_ids = drop_subflow_definitions(flows)
        n_defs = sum(1 for item in flows if item.get("type", "").startswith("subflow:"))
        print("dropped {} subflow definition(s) and {} inner node(s) "
              "({} -> {} nodes)".format(
                  len(template_subflow_ids), n_total - len(flows) - len(template_subflow_ids),
                  n_total, len(flows)))

    n_ids = regenerate_ids(flows)

    if subflow_map:
        unknown = [old for old in subflow_map if old not in template_subflow_ids]
        for old in unknown:
            print("WARNING: subflow-map id '{}' is not a subflow definition "
                  "in this template".format(old))
        n_repointed = apply_subflow_map(flows, subflow_map)
        print("re-pointed {} subflow instance(s) via --subflow-map".format(n_repointed))

    n_brokers = apply_broker_port(flows, args.broker_port)
    apply_env_overrides(flows, args)

    # Serialize once with indentation so the output is reviewable/diffable,
    # then apply the renames on the text.
    data = json.dumps(flows, ensure_ascii=False, indent=2)
    data = apply_replacements(data, args.from_num, args.to_num, args.ntfy_base)

    json.loads(data)  # fail loudly instead of writing broken JSON

    with open(args.output, "w") as f:
        f.write(data)

    print("wrote {} ({} nodes, {} node ids regenerated, {} broker(s) set to port {}, "
          "LORALARM_CS_BROKER={})".format(
              args.output, len(flows), n_ids, n_brokers, args.broker_port, args.broker))
    if args.no_subflows or subflow_map:
        print("NOTE: this output contains no subflow definitions ({} instance "
              "type references); the target instance must already have the "
              "referenced subflows, otherwise Node-RED imports them as "
              "'unknown subflow'.".format(n_defs))
    print("NOTE: fill the per-sensor values (devEui etc.) in the imported" +
          " subflow instance env; RUI flows read them from" +
          " components/flow_inputs.json at flow start.")


if __name__ == "__main__":
    main()
