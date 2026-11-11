---
title: "Tested devices"
---

## Hardware tested in the real world

Every device below runs in a real LoRaLarm installation and is part of the
project's test suites: their captured uplinks are used as test data, and their
payload codecs and family definitions live in the repo. In other words, these
units don't just "should work" — they are covered by tests on every change.

### RAK3172 – build your own sensor (WisBlock)

| | |
|---|---|
| **Type** | DIY sensor node (RAK3172, STM32WLE5) |
| **Firmware** | `devices/rui3_sensor/` in the repo (RAK RUI3, Cayenne LPP) |
| **Battery** | 3 V cell, ~24 µA sleep current — battery life in years, not days |
| **Sensors** | RAK1901 (temp/humidity), RAK1902 (pressure), RAK1903 (light), RAK1904 (accelerometer/motion), RAK1906 (BME680) |
| **Inputs** | Switch/alarm input on the RAK19003/RAK19007 base boards |
| **Configuration** | AT commands and downlinks (send interval, accelerometer sensitivity), persisted to flash |
| **Status** | Tested — switch state in every heartbeat, immediate uplink on motion or an open circuit |

### RAK4631 – the same board as Meshtastic

| | |
|---|---|
| **Type** | DIY alarm node (RAK4631, nRF52840) |
| **Firmware** | `devices/android_rak4631/` in the repo (Arduino) |
| **Battery** | LiPo cell, ~40 µA sleep current |
| **Sensors** | LIS3DH accelerometer (motion/tilt), temperature |
| **Inputs** | Switch/alarm input (GPIO), state reported in every transmission |
| **Configuration** | Downlinks for accelerometer threshold, frequency and duration |
| **Status** | Tested — extra easy to try: the board is one of the most common Meshtastic platforms, so you may already have one in a drawer |

### Dragino LDS02 – off-the-shelf door contact

| | |
|---|---|
| **Type** | Complete door contact (Dragino Switch LDS02) |
| **Battery** | 2 × AAA (~3.0 V) |
| **Function** | Door state on every change plus a heartbeat; battery voltage in every transmission |
| **Alarm** | Door alarm (open/closed) with an optional time window, e.g. nights only |
| **Status** | Tested — no temperature data, no downlink settings |

### Browan TBWL100 – off-the-shelf water-leak sensor

| | |
|---|---|
| **Type** | Complete water/leak sensor (Browan TBWL100) |
| **Battery** | 3.6 V Li-SOCl2 cell |
| **Function** | Leak state sent immediately and repeated while wet; keep-alive every 60 minutes with temperature, humidity and battery |
| **Alarm** | Water alarm (leak detected / dry again) |
| **Status** | Tested — no downlink configuration; battery limits are tunable per device |

---

## Building out a WisBlock node

The DIY firmware (`rui3_sensor`) supports the following RAK modules — pick the
sensors your application needs:

| Module | Function | Slot |
|---|---|---|
| RAK1901 | Temperature & humidity (SHTC3) | Any I²C |
| RAK1902 | Barometric pressure (LPS35HW / LPS22HB) | Any I²C |
| RAK1903 | Ambient light (OPT3001) | Any I²C |
| RAK1904 | Accelerometer / motion (LIS3DH) | Slot D (INT1 → WB_IO5) |
| RAK1906 | Environmental (BME680): temp, humidity, pressure, gas | Any I²C |
| RAK19003 | Mini base board with switch/alarm input | — |
| RAK19007 | Full-size base board with switch/alarm input | — |

## Third-party devices

The system is not locked to the units above. The server is data-driven: a new
sensor type is described in a single JSON file (a family definition) plus a
payload codec in ChirpStack — no server code needs to change. See
`server/python/README.md` in the repo for how a device is added.
