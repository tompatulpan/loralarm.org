# rui3_sensor2 — modular LoRaLarm RUI3 firmware

Second-generation RUI3 firmware for **RAK3172** (STM32WLE5) and **RAK4631-R** (nRF52840) with
WisBlock sensors RAK1901/1902/1903/1904/1906 and a switch input. It is a **drop-in replacement for
[`../rui3_sensor`](../rui3_sensor)** (v33): same LoRaWAN behaviour, same Cayenne LPP channel map, same
AT commands, same downlinks, so the server and codecs need no change. The old firmware stays as the
reference until this one has run in the field.

What changed is the structure: the 1,100-line `.ino` is split into modules with one job each, the
decision logic is pure and unit-tested on the host, and adding a sensor is one table row.

For the payload channel table, hardware list, battery-sensing modes, power notes and AT commands see
the [`rui3_sensor` README](../rui3_sensor/README.md); everything there still applies.

## Module map

| File | Responsibility |
|---|---|
| `rui3_sensor2.ino` | `setup()` sequencing and `loop()` — nothing else |
| `config.h` | Compile-time settings: logging, pins, join/battery policy, timing, LPP channel map, `FW_VERSION_BYTE` |
| `state.h` / `state.cpp` | All cross-module runtime state in one place, with which context may touch it |
| `sensors.h` / `sensors.cpp` | **Sensor registry** — one table drives init, wake-up, read, shutdown, alert reads and conflicts |
| `RAK1901…1906_*.cpp` | Sensor drivers (unchanged apart from the registry hooks) |
| `battery.h` / `battery.cpp` | Boot guard, confirmed-critical check, shutdown latch, per-tick supervision |
| `uplink.h` / `uplink.cpp` | Heartbeat (`sensor_handler`), packet send, send timer |
| `lorawan.h` / `lorawan.cpp` | Join policy, callbacks, downlink handling, P2P callbacks |
| `custom_at.cpp` | Flash settings and the custom AT commands (`SENDINT`, `MININT`, `BATCRIT`, `STATUS`) |
| `switch_gpio.cpp` | Debounced switch input |
| `platform*.{h,cpp}` | MCU differences (battery ADC, BLE, power rail) — no `#ifdef` for the MCU elsewhere |
| `battery_policy.h`, `send_policy.h`, `downlink_parse.h` | **Pure** decision rules, no Arduino includes, host-tested |

### Adding a sensor
1. Write `init_x / read_x / startup_x / shutdown_x` in its own `.cpp` and declare them in `sensors.h`.
2. Add a `SensorId` and one row in the table in `sensors.cpp`. Set `suppressed_by` if it writes the
   same LPP channels as another sensor (as the RAK1906 does for RAK1901/1902) and `read_in_alert`
   if it is fast enough for a motion frame.
3. Add its I2C address to `i2c_known[]` (diagnostics) and, if it has a new measurement, a channel in `config.h`.

No change is needed in `sensor_handler`, `setup()` or the alert path.

## Behaviour notes (differences from v33)

- **Version byte is 100** (`FW_VERSION_BYTE`), so a node on this line is distinguishable on ch 7.
- **Downlink range check fixed.** v33 computed the 3-digit value in a `uint8_t`, so `300` wrapped to
  `44` and was accepted on port 5. It is now validated as a real number and rejected.
- Motion alerts carry live X/Y/Z and (when a RAK1901 is present and no RAK1906) temp/humidity,
  exactly as in v33; `heartbeat_busy` still defers an alert that fires mid-heartbeat.
- Everything else — join/ADR/TXP handling, battery latch and hysteresis, boot guard, caching of the
  session, BLE re-arm on nRF52 — is a straight port.

## Build

Arduino CLI / IDE with the RUI3 BSP (tested with `rak_rui` 4.3.0):

```
arduino-cli compile --fqbn <fqbn> devices/rui3_sensor2
```

| Board | FQBN |
|---|---|
| RAK3172 | see `sketch.yaml` (EU868 only, AT + LoRaWAN) |
| RAK4631-R | `rak_rui:nrf52:WisCoreRAK4631Board:debug=l0,supportEU868=1` (other regions `=2` to save flash) |

Libraries: CayenneLPP, RAKwireless RAK1901 SHTC3, LPS35HW, ClosedCube OPT3001, Adafruit LIS3DH,
Adafruit BME680 (+ BusIO, Unified Sensor).

Verify `ACC_INT_PIN` and the battery sensing mode (see `platform_*.cpp`) match your hardware.

## Host tests

The battery, heartbeat-content and downlink rules need no hardware:

```
make -C devices/rui3_sensor2/tests/host
```

Sensor drivers, the radio and the sleep behaviour can only be verified on a device — flash-test
after any change to them.
