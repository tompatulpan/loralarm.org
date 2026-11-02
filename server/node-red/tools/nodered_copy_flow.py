#!/usr/bin/env python3
"""
nodered_copy_flow.py -- copy a Node-RED sensor flow template and rebrand it
for a new sensor number.

Templates (in ../components/) are normalized to sensor number 1:
dashboardName "Sensor1_Sensorer", tab "Sensor1", deviceName "sensor1",
ntfy topic "sensor1".  This script copies such a template and renames
everything to the target sensor number, regenerates the ids of all
ui_tab / ui_group nodes (and the references to them), and optionally
overrides per-deployment settings (MQTT broker, ntfy, ChirpStack,
TTN application id).

Usage:
    python3 nodered_copy_flow.py <input.json> <output.json> --to 2
    python3 nodered_copy_flow.py ../components/flow_sensor_rui.json \
        flow_sensor2_rui.json --to 2 --broker 192.168.2.26

Afterwards, import the output file in Node-RED and set the sensor's real
devEUI (subflow env `devEui` / `devEuiGlobal.sensorN`).

Known limitation: the sensor registry in components/flow_inputs.json is
NOT updated by this script.  A new sensor must also be registered there
(devEui, deviceName, dashboardName) for settings uplinks/downlinks to work.
"""

import argparse
import json
import random

# Per-deployment defaults.  Values that are None keep the template value.
DEFAULTS = {
    "broker": "192.168.2.26",        # local MQTT broker (TTN broker is never touched)
    "broker_port": "1883",
    "ntfy_base": "127.0.0.1:8075",   # self-hosted ntfy, used as ntfy_base/<topic>
    "chirpstack_base": "localhost:8080",  # ChirpStack API for downlinks
    "ttn_app": "larm-test@ttn",      # TTN application id used in MQTT topics
}


def generate_uid():
    """Node-RED style id: two hex numbers separated by a dot."""
    return "{0:x}.{1:x}".format(random.randint(0, 0xFFFFFFFF),
                                random.randint(0, 0xFFFFFF))


def regenerate_ui_ids(flows):
    """Give ui_tab/ui_group nodes fresh ids and remap all references."""
    replace_map = {}

    for item in flows:
        if item.get("type", "").startswith("ui_tab"):
            new_uid = generate_uid()
            replace_map[item["id"]] = new_uid
            item["id"] = new_uid

    for item in flows:
        if item.get("type", "").startswith("ui_group"):
            new_uid = generate_uid()
            item["tab"] = replace_map.get(item.get("tab"), item.get("tab"))
            replace_map[item["id"]] = new_uid
            item["id"] = new_uid

    for item in flows:
        if item.get("type", "").startswith("ui_"):
            if "group" in item:
                item["group"] = replace_map.get(item["group"], item["group"])

    return len(replace_map)


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

    n_ids = regenerate_ui_ids(flows)
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

    print("wrote {} ({} nodes, {} ui tab/group ids regenerated, {} broker(s) set to {}:{})".format(
        args.output, len(flows), n_ids, n_brokers, args.broker, args.broker_port))
    print("NOTE: register the new sensor in components/flow_inputs.json "
          "(devEui/deviceName/dashboardName) and set its devEUI after import.")


if __name__ == "__main__":
    main()
