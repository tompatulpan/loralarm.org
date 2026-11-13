<p align="right"><img src="docs/loralarm-owl.svg" width="150" alt="LoRaLarm owl mascot"></p>

# loralarm.org

**An open, battery-powered LoRaWAN alarm and sensor system** — from firmware on the sensor nodes to the gateways, network server and notification service, collected in one repository. Published at [loralarm.org](https://loralarm.org).

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Website](https://img.shields.io/badge/site-loralarm.org-orange)](https://loralarm.org)
[![LoRaWAN](https://img.shields.io/badge/radio-868%20MHz%20LoRaWAN-green)](devices/rui3_sensor/README.md)

## What is LoRaLarm?

LoRaLarm is a DIY alarm and monitoring system built on LoRaWAN sensors (RAK WisBlock hardware) that report switch state, motion, temperature, battery voltage and more over the air. Uplinks travel through a LoRa gateway to ChirpStack, where a rule-driven service evaluates every frame and pushes notifications (ntfy, Pushover) when something happens — a door opens, motion is detected, a battery runs low, a device goes silent. Everything is open source under the MIT license.

Highlights:

- **Battery-first firmware** — LoRaWAN OTAA, deep sleep between events (~24 µA idle on RAK3172), immediate uplink on switch change or motion, periodic heartbeats, AT-command configuration and downlink support
- **Modular hardware** — RAK3172 (STM32WLE5) and RAK4631 (nRF52840) core modules with pluggable WisBlock sensors (temperature/humidity, pressure, light, accelerometer, BME680) and a switch/alarm input
- **Meshtastic-friendly test bed** — the RAK4631 is one of the most popular Meshtastic nodes out there, so chances are you already own the hardware: wiping it and flashing LoRaLarm firmware for a spin against your LoRaWAN gateway takes minutes, and going back to Meshtastic is just as easy
- **Data-driven server core** — device families, alarm rules and field transforms are defined in JSON, not hardcoded; adding a sensor type means one file (see [examples](#no-server-code-required-some-things-json-can-do))
- **Rule-based alarms** — temperature, battery, motion and circuit rules plus watchdog monitoring of silent devices, with per-device/family/default configuration layers
- **Notification fan-out** — ntfy and Pushover delivery with retries, alarm time windows, per-device settings
- **Event history** — every uplink and alarm transition is logged to SQLite with per-device state snapshots
- **Downlink control** — tune accelerometer sensitivity, send intervals and device settings over the air

## No server code required — some things JSON can do

Everything below is live configuration for [`server/python/`](server/python/). No server code required.

### The family: rules come built in, tune the numbers once

The LDS02 runs on 2 × AAA (~3.0 V fresh). One block sets how that battery behaves for **every** LDS02 in the fleet — the full/warn/empty voltage thresholds and how long silence may pass before the watchdog alerts:

```json
"families": {
  "Dragino Switch LDS02": {
    "watchdog_hours": 24,
    "batt_full_v": 3.0,
    "batt_warn_v": 2.5,
    "batt_empty_v": 2.2
  }
}
```

### Then the doors

The door-open alarm rule itself is built into the family. So the first door needs only a name — and the second takes just what makes it different: a tighter watchdog and night-only alarms with a custom message:

```json
"devices": {
  "a84041a5d7598dd0": {
    "name": "Door1",
    "family": "Dragino Switch LDS02"
  },
  "a84041e3d189503d": {
    "name": "Door2",
    "family": "Dragino Switch LDS02",
    "watchdog_hours": 2,
    "alarm_window": "21:00-09:00",
    "notify_fire": { "door": "🚪 {name} is open — {detail}" }
  }
}
```

Values layer **defaults < family < device**, the most specific one wins — Door2 overrides only the two settings it cares about and inherits the rest. Outside the alarm window the door alarm is still logged; it just doesn't notify in the middle of the day.

## Structure

| Directory | Contents |
|---|---|
| [`site/`](site/) | Source for the [loralarm.org](https://loralarm.org) website — Hugo, auto-published on Cloudflare Pages (see [`site/README.md`](site/README.md)) |
| [`devices/`](devices/) | LoRa client firmware: [`rui3_sensor/`](devices/rui3_sensor/) (RAK3172/RAK4631 sensor firmware, Cayenne LPP) and [`android_rak4631/`](devices/android_rak4631/) (Arduino-based alarm node) |
| [`server/`](server/) | Server side: [`chirpstack/`](server/chirpstack/) (payload codecs, fixtures, migration spec), [`node-red/`](server/node-red/) (the original flow-based glue), [`python/`](server/python/) (the current `loralarm` service — ingest, alarm engine, notifications, downlinks) |
| [`PWA/`](PWA/) | Design brief and mockups for the planned installable notification + fleet-status web app |
| [`docs/`](docs/) | Project-wide diagrams and documentation |

## Data flow

![Signal path: LoRa devices via a gateway to ChirpStack, which writes decoded fields to InfluxDB (queried by Grafana) and publishes uplinks on MQTT; the loralarm Python service evaluates alarms, writes the SQLite event log, sends downlinks and pushes notifications to phones, with Grafana also able to alert the phone; Node-RED consumes the same feed as the legacy path](docs/loralarm-flow.svg)

1. **Devices** (battery-powered RAK nodes) send uplinks — heartbeats and event-triggered frames — over 868 MHz LoRaWAN.
2. A **gateway** forwards them to **ChirpStack**, where the per-family payload codec decodes the frame into an `object`.
3. ChirpStack writes the decoded fields and metadata to **InfluxDB** — visualized in **Grafana** dashboards — and publishes every uplink on MQTT (**Mosquitto**).
4. The **loralarm Python service** (with Node-RED as the legacy parallel consumer) subscribes to that feed, evaluates alarm rules against per-device configuration, and logs events and state to **SQLite**; downlink commands go back over the same broker.
5. Alarm transitions fan out as **push notifications** (ntfy / Pushover) to your phone — and Grafana can send its own dashboard alerts there too.

See [`server/python/ARCHITECTURE.md`](server/python/ARCHITECTURE.md) for the design of the service and [`server/chirpstack/loralarm-node-red-→-chirpstack-+-influxdb-+-python-migration-spec.md`](server/chirpstack/loralarm-node-red-%E2%86%92-chirpstack-+-influxdb-+-python-migration-spec.md) for the migration story behind it.

### Tech stack

- **Firmware:** RAK RUI3 / Arduino (STM32WLE5, nRF52840), Cayenne LPP payloads
- **Network server:** ChirpStack with JavaScript payload codecs, Mosquitto MQTT
- **Service:** Python (stdlib-only core, paho-mqtt, SQLite), rule engine driven by JSON device-family definitions
- **Glue (legacy/parallel):** Node-RED flows
- **Website:** Hugo on Cloudflare Pages
- **Observability:** InfluxDB + Grafana (planned PWA for end-user status)

## Getting started

- **Build device firmware:** see [`devices/rui3_sensor/README.md`](devices/rui3_sensor/README.md) for supported hardware, wiring notes and AT commands.
- **Run the service:** see [`server/python/README.md`](server/python/README.md) — `python3 -m loralarm` subscribes to the ChirpStack MQTT feed; tests run with `python3 -m unittest discover -s tests -t .` (stdlib only).
- **Edit the website:** run `hugo server` in `site/` — see [`site/README.md`](site/README.md) for the workflow and Cloudflare Pages build settings. Every `git push` publishes automatically.

## Trying it out with a Meshtastic RAK4631

Already running [Meshtastic](https://meshtastic.org) on a RAK4631? Then half the work is done — it's the same board, and the nRF52840 + SX1262 radio it carries is exactly what LoRaLarm firmware expects. Flash the firmware over USB through the board's built-in bootloader, join it to your LoRaWAN gateway via OTAA, and you have a working sensor node for testing. Going back to Meshtastic is just a re-flash away.

1. Build and flash the Arduino firmware from [`devices/android_rak4631/`](devices/android_rak4631/) to the RAK4631 over USB (Arduino IDE with the RAK nRF52 board package and the `SX126x-Arduino` and `SparkFun LIS3DH` libraries). Copy `example.secrets.h` to `secrets.h` and fill in the OTAA credentials (`NODE_DEVICE_EUI`, `NODE_APP_EUI`, `NODE_APP_KEY`, with `DO_OTAA` set to `true`). The region is hard-coded to EU868 in `lora_handler.cpp`; change it there if your gateway uses another band.
2. Register the device (OTAA) in ChirpStack with the bundled [`payload_codec_rak4631.js`](server/chirpstack/codecs/payload_codec_rak4631.js) codec.
3. Watch the uplinks arrive and the alarms fire — no soldering required to get started.

## License

[MIT](LICENSE) © Tomas · SM0ONR
