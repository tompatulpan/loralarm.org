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

Bulk copy (one output file per sensor, e.g. sensors 2-16 with no subflows,
or 17-32 for later scaling) via --to-range; the output path then needs a
{n} placeholder for the sensor number (without one, "-<n>" is inserted
before the file extension).  Every copy is built from a fresh deep copy
of the template, so each file gets its own newly regenerated node ids:
    python3 nodered_copy_flow.py ../components/flow_sensor_rui.json \\
        'flow_sensor{n}_rui.json' --to-range 2-16 --no-subflows

Deployment workflow (deterministic for any target instance, no live id
lookup, no import ordering -- Node-RED resolves subflow instances by
id, never by name): deploy each sensor batch as ONE file with
--no-subflows --with-subflows; it contains the canonical subflow
definitions with their template ids plus all requested sensor copies:
    python3 nodered_copy_flow.py ../components/flow_sensor_unified.json \\
        'flow_sensor{n}_unified.json' --to-range 2-16 --no-subflows \\
        --with-subflows
The {n} in the output path becomes the number or range ("2-16").
Import that single file; in the import dialog DESELECT every node
flagged as already existing (shared config nodes; plus, from the second
batch on, the subflow definitions installed by an earlier batch) and
never click "Import copy" -- that would regenerate the definition ids.
The definitions are in the same import as the instances, so nothing
can arrive out of order; instances imported before their definition
existed stay "unknown: subflow" forever and must be deleted and
re-imported.

Subflow selection:
    --no-subflows   omit the subflow definitions and their inner nodes;
                    the subflow instances reference the template
                    definition ids, which the instance must already have
                    (see step 1 above, --subflows-only) - otherwise
                    Node-RED stops the flow with "missing node types
                    subflow:<id>".
    --subflow-map OLD=NEW   (repeatable, implies --no-subflows)
                    legacy escape hatch for instances whose subflow
                    definitions were imported via "Import copy" or from
                    script output and thus carry regenerated ids:
                    re-points the instances from template definition id
                    OLD to the live definition id NEW.  Prefer fixing
                    the instance once with --subflows-only.
    --subflows-only write the subflow definitions, their inner nodes and
                    the shared global config nodes, ids UNCHANGED, and
                    exit; the sensor-number flags are not allowed here.
                    Optional helper to pre-install the canonical
                    subflows; --with-subflows makes it unnecessary.
    --with-subflows bundle the canonical subflow definitions (template
                    ids, not regenerated) into the same output file as
                    the sensor copies of this run: one file per batch,
                    one import, no ordering dependency.  The output path
                    {n} becomes the sensor number or range.  In the
                    import dialog deselect the nodes flagged as
                    existing -- never "Import copy" this file.

Afterwards, import the output file in Node-RED and fill the sensor's
per-instance values in the subflow env: the RAK4631/Android flow and the
unified flow (flow_sensor_unified.json, RAK4631 + RUI3) read all
per-sensor settings (devEui, deviceName, dashboardName, ttnDeviceName,
tilt_sense, Tk, ntfyChannel, pushover; unified also protocol, csApp,
tempAlarmLimit, battFull/battWarn/battEmpty) from the subflow instance
env.  The RUI flow has no such env values; it reads
devEui/deviceName/dashboardName at flow start from the sensorConfig
registry (components/flow_inputs.json).

Known limitation: the script updates neither the sensorConfig registry
in components/flow_inputs.json nor any subflow env value -- they are
entered per sensor in the editor after import.
"""

import argparse
import copy
import json
import os
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


def extract_subflows(flows):
    """The canonical subflow install: definitions, inner nodes, configs.

    Keeps the ids UNCHANGED (unlike a normal copy, which regenerates
    them): --no-subflows copies reference the template definition ids,
    so the instance must carry the subflows under exactly these ids.
    The shared global config nodes are included because the inner nodes
    reference them by id (mqtt brokers, ntfy/pushover keys, the
    LORALARM_* env of global-config); on a running instance they exist
    already and are deselected in the import dialog.
    Returns the node list to write.
    """
    subflow_ids = {item["id"] for item in flows if item.get("type") == "subflow"}
    return [item for item in flows
            if item.get("type") == "subflow"
            or item.get("z") in subflow_ids
            or item.get("type", "") in CONFIG_TYPES]


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


def sensor_range(spec):
    """argparse type for --to-range: "START-END" -> range(START, END+1)."""
    m = re.match(r"^(\d+)-(\d+)$", spec.strip())
    if not m:
        raise argparse.ArgumentTypeError(
            "expected START-END, e.g. 2-16, got '{}'".format(spec))
    start, end = int(m.group(1)), int(m.group(2))
    if start > end:
        raise argparse.ArgumentTypeError(
            "range start {} is greater than end {}".format(start, end))
    if end - start + 1 > 1000:
        raise argparse.ArgumentTypeError(
            "range {} covers more than 1000 sensors".format(spec))
    return range(start, end + 1)


def resolve_output(output, to_num, multi):
    """Output path for one sensor of a (possibly multi-sensor) run.

    With --to-range the path should contain {n} (or {}); without a
    placeholder the sensor number is inserted before the extension, e.g.
    flow_sensor_rui.json -> flow_sensor_rui-2.json.  A single --to run
    uses the path unchanged.
    """
    if not multi:
        return output
    if "{}" in output:
        return output.format(to_num)
    if "{n}" in output:
        return output.format(n=to_num)
    base, ext = os.path.splitext(output)
    return "{}-{}{}".format(base, to_num, ext)


def build_copy(template, args, to_num, subflow_map, drop_defs=False,
               strip_configs=False):
    """Build one rebranded copy of the template for sensor to_num.

    Works on a deep copy, so every target number gets its own regenerated
    node ids.  drop_defs removes the subflow definitions and their inner
    nodes (--no-subflows); strip_configs removes the shared config node
    objects (the --with-subflows bundle carries them once, alongside the
    definitions).  Returns (data, stats): the JSON text to write and a
    dict of counts for the summary line.
    """
    flows = copy.deepcopy(template)
    stats = {"to": to_num}

    n_defs = 0
    if drop_defs:
        n_total = len(flows)
        flows, template_subflow_ids = drop_subflow_definitions(flows)
        n_defs = sum(1 for item in flows if item.get("type", "").startswith("subflow:"))
        print("dropped {} subflow definition(s) and {} inner node(s) "
              "({} -> {} nodes)".format(
                  len(template_subflow_ids), n_total - len(flows) - len(template_subflow_ids),
                  n_total, len(flows)))

    stats["ids"] = regenerate_ids(flows)

    if subflow_map:
        unknown = [old for old in subflow_map if old not in template_subflow_ids]
        for old in unknown:
            print("WARNING: subflow-map id '{}' is not a subflow definition "
                  "in this template".format(old))
        n_repointed = apply_subflow_map(flows, subflow_map)
        print("re-pointed {} subflow instance(s) via --subflow-map".format(n_repointed))

    stats["brokers"] = apply_broker_port(flows, args.broker_port)
    apply_env_overrides(flows, args)
    if strip_configs:
        # The bundle holds the shared config nodes once, next to the
        # subflow definitions; the copies keep only their references.
        flows = [item for item in flows
                 if item.get("type", "") not in CONFIG_TYPES]

    # Serialize once with indentation so the output is reviewable/diffable,
    # then apply the renames on the text.
    data = json.dumps(flows, ensure_ascii=False, indent=2)
    data = apply_replacements(data, args.from_num, to_num, args.ntfy_base)

    json.loads(data)  # fail loudly instead of writing broken JSON
    stats["nodes"] = len(flows)
    stats["subflow_refs"] = n_defs
    return data, stats


def write_subflows_only(template, args):
    """Write the canonical subflow install file.

    Ids stay as in the template; the per-deployment env overrides and
    broker port still apply, so a fresh instance also gets the correct
    LORALARM_* values.  Returns the JSON text written.
    """
    flows = extract_subflows(template)
    n_defs = sum(1 for item in flows if item.get("type") == "subflow")
    n_brokers = apply_broker_port(flows, args.broker_port)
    apply_env_overrides(flows, args)
    data = json.dumps(flows, ensure_ascii=False, indent=2)
    json.loads(data)  # fail loudly instead of writing broken JSON
    with open(args.output, "w") as f:
        f.write(data)
    print("wrote {} ({} subflow definition(s), {} nodes total, ids "
          "unchanged, {} broker(s) set to port {}, LORALARM_CS_BROKER={})"
          .format(args.output, n_defs, len(flows), n_brokers,
                  args.broker_port, args.broker))
    for item in flows:
        if item.get("type") == "subflow":
            print("subflow definition: {} ({})".format(
                item["id"], item.get("name", "?")))
    return data


def subflow_defs_part(template, args):
    """The canonical subflow definitions part of a --with-subflows bundle.

    Subflow definitions, their inner nodes and the shared config nodes
    with UNCHANGED template ids, plus the env/broker overrides.  The
    copies in the same bundle reference these ids, so nothing depends on
    what the instance happens to carry.  Returns (json_text, flow_list).
    """
    flows = extract_subflows(template)
    apply_broker_port(flows, args.broker_port)
    apply_env_overrides(flows, args)
    data = json.dumps(flows, ensure_ascii=False, indent=2)
    if args.ntfy_base and args.ntfy_base != DEFAULTS["ntfy_base"]:
        data = data.replace(DEFAULTS["ntfy_base"], args.ntfy_base)
    json.loads(data)  # fail loudly instead of writing broken JSON
    return data, flows


def merge_flow_parts(parts):
    """Concatenate pretty-printed JSON flow arrays into one (validated)."""
    combined = []
    for part in parts:
        combined.extend(json.loads(part))
    return json.dumps(combined, ensure_ascii=False, indent=2)


def output_token_path(output, token):
    """Bundle output path: {n} / {} becomes the number or range token."""
    if "{}" in output:
        return output.format(token)
    if "{n}" in output:
        return output.format(n=token)
    base, ext = os.path.splitext(output)
    return "{0}-{1}{2}".format(base, token, ext)


def main():
    parser = argparse.ArgumentParser(
        description="Copy a Node-RED sensor flow template (sensor 1) "
                    "and rebrand it for another sensor number.")
    parser.add_argument("input", help="template flow JSON, e.g. ../components/flow_sensor_rui.json")
    parser.add_argument("output", help="output flow JSON to import in Node-RED")
    parser.add_argument("--from", dest="from_num", type=int, default=1,
                        help="sensor number in the template (default: 1)")
    parser.add_argument("--to", dest="to_num", type=int, default=None,
                        help="sensor number for the new flow, e.g. 2")
    parser.add_argument("--to-range", dest="to_range", type=sensor_range,
                        default=None, metavar="START-END",
                        help="sensor number range for a bulk copy, e.g. "
                             "2-16; writes one output file per sensor and "
                             "the output path needs a {n} placeholder "
                             "(exactly one of --to / --to-range is required)")
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
    parser.add_argument("--with-subflows", action="store_true",
                        help="bundle the canonical subflow definitions "
                             "(template ids, not regenerated) into the same "
                             "output file as the sensor copies of this run: "
                             "one file per batch, one import, no ordering "
                             "dependency; the output path {n} becomes the "
                             "sensor number or range.  In the import dialog "
                             "deselect the nodes flagged as existing -- "
                             "never 'Import copy' this file")
    parser.add_argument("--subflows-only", action="store_true",
                        help="do not copy a sensor flow: write the subflow "
                             "definitions, their inner nodes and the shared "
                             "config nodes with UNCHANGED ids instead; import "
                             "once per instance so the --no-subflows copies "
                             "resolve (incompatible with the sensor-number "
                             "and subflow-map flags)")
    parser.add_argument("--subflow-map", action="append", default=[],
                        metavar="OLD=NEW",
                        help="re-point subflow instances from template "
                             "definition id OLD to live definition id NEW "
                             "(repeatable; implies --no-subflows)")
    args = parser.parse_args()

    if args.subflows_only:
        if args.to_num is not None or args.to_range is not None:
            parser.error("--subflows-only writes no sensor copy: "
                         "do not combine it with --to / --to-range")
        if args.no_subflows or args.subflow_map:
            parser.error("--subflows-only cannot be combined with "
                         "--no-subflows / --subflow-map")
    elif (args.to_num is None) == (args.to_range is None):
        parser.error("exactly one of --to / --to-range is required "
                     "(or use --subflows-only)")
    if args.with_subflows:
        if args.no_subflows or args.subflow_map:
            parser.error("--with-subflows drops the subflow definitions "
                         "from the copies itself: do not combine it with "
                         "--no-subflows / --subflow-map")
        if args.subflows_only:
            parser.error("--with-subflows needs sensor numbers "
                         "(--to / --to-range)")

    subflow_map = {}
    for pair in args.subflow_map:
        if "=" not in pair:
            parser.error("--subflow-map expects OLD=NEW, got '" + pair + "'")
        old, new = pair.split("=", 1)
        subflow_map[old] = new

    with open(args.input, "r") as f:
        template = json.load(f)

    if args.subflows_only:
        data = write_subflows_only(template, args)
        print("NOTE: import '{}' once per instance; in the import dialog "
              "DESELECT every node flagged as already existing (config "
              "nodes, subflow definitions already present) so the subflows "
              "keep their template ids -- never 'Import copy' this file, "
              "that would break every later --no-subflows copy."
              .format(args.output))
        return

    targets = [args.to_num] if args.to_num is not None else list(args.to_range)
    multi = len(targets) > 1

    if args.with_subflows:
        # One file per batch: canonical subflow definitions (template ids)
        # plus all sensor copies, so a single import can never arrive in
        # the wrong order and no live-id knowledge is needed.
        defs_data, defs = subflow_defs_part(template, args)
        parts = [defs_data]
        n_ids_total = 0
        for to_num in targets:
            print("== sensor {} ==".format(to_num))
            data, stats = build_copy(template, args, to_num, subflow_map,
                                     drop_defs=True, strip_configs=True)
            parts.append(data)
            n_ids_total += stats["ids"]
        bundle = merge_flow_parts(parts)
        token = str(targets[0]) if len(targets) == 1 else \
            "{0}-{1}".format(targets[0], targets[-1])
        out_path = output_token_path(args.output, token)
        with open(out_path, "w") as f:
            f.write(bundle)
        print("wrote {} ({} subflow definition(s) with template ids + "
              "copies for sensor(s) {}, {} nodes total, {} copy node ids "
              "regenerated, {} broker(s) set to port {}, "
              "LORALARM_CS_BROKER={})".format(
                  out_path, sum(1 for n in defs if n.get("type") == "subflow"),
                  token, len(json.loads(bundle)), n_ids_total,
                  sum(1 for n in defs if n.get("type") == "mqtt-broker"),
                  args.broker_port, args.broker))
        print("NOTE: import '{}' as ONE file -- the subflow definitions and "
              "their instances are in the same import.  In the import "
              "dialog DESELECT the nodes flagged as already existing "
              "(shared config nodes; plus, from the second batch on, the "
              "subflow definitions of an earlier batch) and import the "
              "rest -- never 'Import copy' this file: that would "
              "regenerate the definition ids.".format(out_path))
        print("NOTE: instances imported before their definition existed "
              "stay 'unknown: subflow' forever -- delete such flows and "
              "re-import them from this bundle.")
    else:
        if args.no_subflows or subflow_map:
            defs = ["{} ({})".format(n["id"], n.get("name", "?"))
                    for n in template if n.get("type") == "subflow"]
            print("the --no-subflows output references existing subflow "
                  "definitions by template id; the instance must have: {}"
                  .format(", ".join(defs)))
            print("missing one? import a --subflows-only file once "
                  "(ids unchanged), or --with-subflows to bundle them "
                  "into this run's output.")

        for to_num in targets:
            if multi:
                print("== sensor {} ==".format(to_num))
            data, stats = build_copy(
                template, args, to_num, subflow_map,
                drop_defs=args.no_subflows or bool(subflow_map))
            out_path = resolve_output(args.output, to_num, multi)
            with open(out_path, "w") as f:
                f.write(data)
            print("wrote {} (sensor {}, {} nodes, {} node ids regenerated, "
                  "{} broker(s) set to port {}, LORALARM_CS_BROKER={})".format(
                      out_path, to_num, stats["nodes"], stats["ids"], stats["brokers"],
                      args.broker_port, args.broker))
            if args.no_subflows or subflow_map:
                print("NOTE: this output contains no subflow definitions ({} instance "
                      "type references); the target instance must already have the "
                      "referenced subflows, otherwise Node-RED imports them as "
                      "'unknown subflow'.".format(stats["subflow_refs"]))
        if multi:
            print("wrote {} file(s) for sensors {}-{}".format(
                len(targets), targets[0], targets[-1]))
    print("NOTE: fill the per-sensor values (devEui etc.) in the imported" +
          " subflow instance env; RUI flows read them from" +
          " components/flow_inputs.json at flow start.")


if __name__ == "__main__":
    main()
