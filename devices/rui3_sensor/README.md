# loralarm.org Project

Welcome to the **loralarm.org** project!  
Firmware for LoRaWAN sensor/alarm nodes based on the RAKWireless RAK3172 (STM32WLE5) module and WisBlock ecosystem.

---

## Hardware

### Supported WisBlock Core modules

| Module | MCU | Status |
|---|---|---|
| RAK3172 | STM32WLE5 | Supported |
| RAK4631-R | nRF52840 (RAK4630 module) | Supported |

### WisBlock sensors and base board

| Module | Function | Slot |
|---|---|---|
| RAK1901 | Temperature & Humidity (SHTC3) | Any I²C slot |
| RAK1902 | Barometric Pressure (LPS35HW / LPS22HB) | Any I²C slot |
| RAK1903 | Ambient Light (OPT3001) | Any I²C slot |
| RAK1904 | Accelerometer / Motion (LIS3DH) | **Slot D** (INT1 → WB_IO5) — default |
| RAK1906 | Environmental (BME680): temp, humidity, pressure, gas resistance | Any I²C slot |
| RAK19003 | Mini base board with switch/alarm input (WB_IO1) | — |
| RAK19007 | Full base board with switch/alarm input (WB_IO1) | — |

> **RAK1904 slot:** Slot D routes INT1 to `WB_IO5` on both RAK19003 and RAK19007. This is the default configured in `app.h` (`ACC_INT_PIN WB_IO5`). If you move the RAK1904 to a different slot, update that one define to match the new slot's interrupt pin.

**Switch input wiring:** the firmware uses the MCU's **internal pull-up** by default (`INPUT_PULLUP` on `WB_IO1`), drawing ~100 µA when the switch is closed. Adding an external 1 MΩ pull-up to Vcc and removing the internal pull-up reduces this to ~25 µA.

**Battery voltage divider (WB_A0):** only required on **RAK4631-R**. RAK19007 and RAK19003 have this divider built in. RAK3172 does **not** use WB_A0 for battery sensing (Mode A reads the internal Vrefint only), so no external divider is needed on RAK3172 regardless of base board.

---

## Features

- LoRaWAN OTAA
- Deep sleep between events (~24 µA idle on RAK3172; ~40 µA on RAK4631-R due to RUI3/SoftDevice overhead)
- Immediate uplink on switch open/close and motion alert
- Periodic heartbeat uplink (configurable interval via AT command)
- Switch state always included in every heartbeat
- Downlink commands to tune accelerometer sensitivity
- AT commands for send interval, minimum interval, and device status
- Cayenne LPP payload format
- RAK1906 BME680 environmental sensor (temperature, humidity, pressure, gas resistance)
- Settings persisted to flash

---

## Payload Channels (Cayenne LPP)

| Channel | LPP type | Content | Sent when |
|---|---|---|---|
| 1 | Voltage | Battery voltage | Adjustable interval |
| 2 | Temperature | Temperature (°C) — RAK1901 or RAK1906 | Every heartbeat |
| 3 | Humidity | Relative humidity (%) — RAK1901 or RAK1906 | Every heartbeat |
| 4 | Barometric Pressure | Pressure (hPa) — RAK1902 or RAK1906 | Every heartbeat |
| 5 | Luminosity | Light (lux) — RAK1903 | Every heartbeat |
| 6 | Accelerometer | X / Y / Z (g) — RAK1904 | Every heartbeat |
| 7 | Digital Input | Firmware version byte |
| 8 | Digital Input | ACC interrupt threshold | Same as above |
| 9 | Digital Input | ACC interrupt duration | Same as above |
| 10 | Digital Input | ACC ODR frequency index | Same as above |
| 11 | Digital Input | Switch state (0x01 = open, 0x00 = closed) | Every heartbeat + immediately on change |
| 12 | Digital Input | Motion alert flag (0x01) | Immediately on motion; deferred to next heartbeat if radio busy |
| 13 | Digital Input | Low-battery shutdown flag (0x01) | Final packet before entering indefinite sleep |
| 14 | Digital Input | Gas resistance (kΩ) — RAK1906 | Every heartbeat |

---

## Downlink Commands

Send to the device on the specified fPort with a 3-byte ASCII decimal value (e.g. `"010"` = 10):

| fPort | Parameter | Valid range | Example |
|---|---|---|---|
| 5 | ACC interrupt threshold | 1–127 | `"016"` |
| 6 | ACC ODR frequency index | 1–7 | `"002"` (10 Hz) |
| 7 | ACC interrupt duration | 0–127 | `"001"` |

Config is applied on the next heartbeat tick and saved to flash.

---

## AT Commands

| Command | Description |
|---|---|
| `ATC+SENDINT=<s>` | Set heartbeat interval in seconds (0 = off) |
| `ATC+SENDINT?` | Get current interval |
| `ATC+MININT=<s>` | Set minimum send interval (must be < SENDINT) |
| `ATC+MININT?` | Get current minimum interval |
| `ATC+BATCRIT=<mv>` | Set battery shutdown threshold in mV (0 = disable runtime check) |
| `ATC+BATCRIT?` | Get current threshold |
| `ATC+STATUS?` | Print device status (mode, region, keys, interval) |

---

## Battery Sensing

### How it works per platform

| Platform | Base board | Mode | What is measured | Accuracy |
|---|---|---|---|---|
| RAK3172 (STM32WLE5) | Any | **A (forced)** | VDDA via Vrefint internal reference | ✅ True cell voltage when no LDO (RAK19009); ⚠️ LDO output ~3.0–3.3V when LDO present (RAK19007) |
| RAK4631-R (nRF52840) | RAK19007 / RAK19003 | **B (fixed)** | True VBAT via WB_A0 voltage divider | ✅ Full Li-ion curve 3.5–4.2V |

**RAK3172 is always Mode A.** Any `analogRead()` on a GPIO pin (WB_A0 in Mode B) leaves the STM32WLE5 ADC `ADEN=1` after the call, which prevents the MCU from entering Stop 2 sleep — current rises from ~24 µA to ~45 µA. The Vrefint internal channel (pin 254) is the only `analogRead()` path in RUI3 that properly clears `ADEN` on exit. Mode A uses only Vrefint, so sleep current is unaffected.

**RAK4631-R is always Mode B.** The nRF52840 SAADC has no equivalent restriction. WB_A0 gives true VBAT through the resistor divider on RAK19007/RAK19003. RAK4631-R is not used in direct-battery configurations so Mode A is not needed.

### What you see on the server

| Platform | Base board | Reported value | Range |
|---|---|---|---|
| RAK3172 | RAK19009 (no LDO) | True battery voltage | 2.8 → 4.2V operating range |
| RAK3172 | RAK19007 / RAK19003 (3.3V LDO) | LDO output (VDDA) | Flat ~3.34V until LDO droops near cell depletion |
| RAK4631-R | RAK19007 / RAK19003 | True battery voltage | 3.5 → 4.2V |

> On RAK3172 + RAK19007/RAK19003: the reported voltage stays flat at ~3.34V throughout most of the battery's life. It drops below 3.0V only when the cell is nearly dead and the LDO enters dropout. This is sufficient to trigger the shutdown alarm before over-discharge, but it is **not a battery fuel gauge**.

### Calibration

Mode A (STM32WLE5) applies a board-level correction after the Vrefint formula:

```
reported = (vdda_raw × BATT_MODE_A_CAL_GAIN) + BATT_MODE_A_CAL_OFFSET
```

Defaults `gain=1.102`, `offset=0.022` — calibrated on RAK19009. Adjust in `app.h` if your board reads differently.

Mode B (nRF52840) uses a fixed divider ratio. Default `BAT_ADC_DIVIDER = 0.587` for RAK4631-R + RAK19003.

To recalibrate: `new_divider = current_divider × (reported_V / actual_V)`

### Battery shutdown thresholds

Two independent checks protect the battery from deep discharge:

| Check | Configured by | When it runs |
|---|---|---|
| **Boot guard** | Platform default in `platform_stm32.cpp` / `platform_nrf52.cpp` | Immediately after platform init, before AT delay, flash, LoRa, or sensors |
| **Runtime check** | `ATC+BATCRIT` stored in flash | Before every heartbeat; every 60 s while unjoined or telemetry is disabled |

| Platform | Default threshold | Rationale |
|---|---|---|
| RAK3172 (Mode A) | **2800 mV** | Leaves margin above 2.5 V for radio/sensor load transients and measurement error |
| RAK4631-R (Mode B) | **2800 mV** | True VBAT via divider; 2.8V = deeply discharged LiPo |

Useful AT commands:

```
ATC+BATCRIT?         read current runtime threshold
ATC+BATCRIT=2800     recommended for RAK3172
ATC+BATCRIT=2800     recommended for RAK4631-R
ATC+BATCRIT=0        disable runtime check entirely (boot guard still active)
```

Values below the platform minimum are rejected and obsolete lower values in flash are migrated to the platform default. At the critical threshold the firmware **does not transmit a final LoRa packet**, because that current pulse is exactly when a marginal supply is most likely to brown out.

> Software sleep reduces load but cannot physically disconnect a failing source. For guaranteed protection, use a regulator/load switch with UVLO or a supervisor that holds reset/removes power below the required voltage. Add local bulk and ceramic decoupling and verify the voltage at the module during a maximum-power LoRa TX with an oscilloscope.

---

## Firmware Flow

### Boot / Restart
1. Serial init, 5 s AT+BOOT window
2. AT commands registered, flash settings restored (intervals, ACC config)
3. GPIO interrupt on `WB_IO1` initialized (`INPUT_PULLUP`, `CHANGE` mode)
4. Heartbeat timer (RAK_TIMER_0) started if `send_interval != 0`
5. Sensors auto-detected via I2C; shut down after init
6. LPM enabled
7. **LoRaWAN:** timer and first uplink triggered after join completes (see Join section below); **LoRa P2P:** timer started immediately and `sensor_handler()` called
8. Enters deep sleep in `loop()`

### LoRaWAN Join

Join configuration is enforced in code on every boot (`AT+NJM=OTAA`, `AT+BAND`, `AT+CLASS=A` are overwritten regardless of flash state).

#### Cold boot (no cached session)

1. OTAA credentials (AppEUI + AppKey) verified — join is skipped if either is all-zero
2. Join profile applied: ADR=off, TXP=`JOIN_MAX_TXP` (index 0, +16 dBm EIRP), DR=`JOIN_DR` (DR3 = SF9)
3. `api.lorawan.join()` started — retries every `JOIN_RETRY_SEC` (255 s) for up to 255 attempts (~18 h total)
4. On success, `joinCallback` fires:
   - ADR enabled
   - TX power stepped to `POST_JOIN_TXP` (index 2, ~12 dBm EIRP) — **v23 fix** (see below)
   - Heartbeat timer started
   - First `sensor_handler()` uplink triggered once `setup_complete` is set

#### Cached session (already joined)

RUI3 can restore a valid OTAA session from flash. `+EVT:JOINED` fires inside `api.lorawan.join()` before `setup()` returns, so `joinCallback` runs mid-setup. A guard on `api.lorawan.njs.get()` skips the join-profile block entirely — **v24 fix** (see below). After setup completes, the end-of-setup block detects the joined state and starts the timer + fires the first TX.

#### v23 — TX power step-down after join

Without this fix, the device stayed at `JOIN_MAX_TXP` (+16 dBm EIRP) until the network sent a `LinkADRReq` downlink. In close-range deployments this saturated the gateway LNA, causing RSSI swings from −30 to −120 dBm and `COLLISION_PACKET` errors on the RX1 window.

Fix: `joinCallback` now immediately sets `POST_JOIN_TXP` (~12 dBm EIRP). ADR can then adjust in either direction from this sane baseline.

#### v24 — Cached session race condition

When `api.lorawan.join()` restores a cached session, `joinCallback` runs mid-`setup()` and correctly sets ADR=on and `POST_JOIN_TXP`. Without the guard, `setup()` would overwrite those settings with ADR=off and `JOIN_MAX_TXP`, then call `join()` a second time — triggering a new OTAA exchange. If the gateway did not respond, the valid session was lost and the device waited up to ~18 h before recovering.

Fix: the join-profile block is wrapped in `if (!api.lorawan.njs.get())` — skipped entirely when a cached session is already active.

### Periodic Heartbeat
Every `send_interval` seconds:
1. Wake sensors, read all present (RAK1901/1902/1903/1904/1906)
2. Every heartbeat, or on firmware/ACC config change: send battery + version + ACC config channels
3. Always read `WB_IO1` pin directly and include switch state in payload
4. Include deferred motion alert if a previous immediate send failed
5. Send full payload → sleep

### Switch Opens (HIGH / alarm triggered)
1. RISING edge ISR → pin state captured immediately, 100 ms debounce timer starts
2. After 100 ms: pin re-read to confirm; 10 ms send timer starts
3. After 10 ms: **immediate uplink** with only `LPP_CHANNEL_DIG_5 = 0x01`

### Switch Closes (LOW / alarm reset)
- **If FALLING edge wakes device**: same 110 ms chain → immediate uplink with `LPP_CHANNEL_DIG_5 = 0x00`
- **If FALLING edge missed by LPM** (known RUI3 deep-sleep limitation): caught on next heartbeat via direct pin read; worst-case latency = one `send_interval`

### Motion Alert (RAK1904)
1. LIS3DH interrupt on `WB_IO5` → ISR defers to RAK_TIMER_1
2. RAK_TIMER_1: builds alert payload, sends immediately
3. If send fails (radio busy): `motion_detected` flag set → included in next heartbeat

### Downlink / ACC Config Update
1. `receiveCallback` validates 3-byte ASCII payload, updates threshold/frequency/duration
2. Sets `acc_config_pending = true` (I2C never called inside LoRa callback)
3. Next heartbeat: applies config to LIS3DH, saves to flash, reports updated values

---

## Getting Started

1. Clone this repository
2. Open in VS Code with the Arduino extension
3. Select your board via the RUI3 BSP:
   - **RAK3172:** `rak_rui:stm32:WisDuoRAK3172EvaluationBoard`
   - **RAK4631-R:** `rak_rui:nrf52:WisBlockRAK4631`
4. Verify `ACC_INT_PIN` in `app.h` matches the physical slot your RAK1904 is in (default: `WB_IO5` = Slot D on RAK19003 and RAK19007)
5. Set the battery sensing mode in `app.h` (`BATT_SENSE_MODE_A` or `BATT_SENSE_MODE_B` — see Battery Sensing Configuration below)
6. Configure OTAA keys with WisToolBox or AT commands (`AT+DEVEUI`, `AT+APPEUI`, `AT+APPKEY`)
7. Set send interval: `ATC+SENDINT=300` (5 minutes)
8. Build and upload `loralarm_RUI3_RAK3172_sensor.ino`

### Dependencies (Arduino libraries)
- CayenneLPP
- RAKwireless RAK1901 SHTC3
- LPS35HW
- ClosedCube OPT3001
- Adafruit LIS3DH + BusIO + Unified Sensor
- Adafruit BME680

---

## Backend Integration

Designed to work with a Node-RED backend via a LoRaWAN network server (e.g. ChirpStack or TTN).

- Decode payloads using a Cayenne LPP decoder
- MQTT topics and credentials must match your network server configuration
- See [loralarm.org](https://loralarm.org) for Node-RED flow examples and integration guides

---

## Contributing

Contributions are welcome — open issues or pull requests for bug fixes, features, or documentation improvements.

## License

MIT License — see [LICENSE](LICENSE) for details.

---
*Project maintained by the loralarm.org community.*
