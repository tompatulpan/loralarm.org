# Changelog

## [v30] — Undervoltage hardening

- Raise the RAK3172 direct-supply cutoff from 2.5 V to 2.8 V and include equality in cutoff checks.
- Run the boot guard before AT delay, LoRa configuration/join, flash access, LEDs, and sensors.
- Check voltage before runtime flash writes and peripheral activity; keep a 60 s protection timer while unjoined or telemetry is disabled.
- Suppress the former "final" LoRa transmission at critical voltage to avoid a TX-induced brownout.
- Reject/migrate persisted thresholds below the platform safety minimum (runtime protection can still be explicitly disabled with `ATC+BATCRIT=0`).

All notable changes to this project are documented here.

> The runtime `version_byte` (currently `30`) remains in `loralarm_RUI3_sensor.ino` because
> it is transmitted in every payload. This file holds the human-readable history.

---

## [v29] — Fix joinCallback / setup() double-start race

Adds `timer_started` boolean flag (default `false`) to prevent a narrow race window
on nRF52840 (FreeRTOS) where `joinCallback` could fire after `setup_complete = true`
but before the end-of-setup `njs.get()` check, causing the periodic timer to be
started twice and `sensor_handler` to be called twice on the first boot.

- `joinCallback`: sets `timer_started = true` after arming the timer and calling
  `sensor_handler`, so the end-of-setup block can detect the overlap.
- End-of-setup block: now guarded by `&& !timer_started` — skipped entirely if
  `joinCallback` already handled the first TX.
- No functional change on STM32WLE5 (single-threaded RUI3 scheduler; the race
  cannot occur in practice, but the guard is harmless).

## [v28] — Platform abstraction refactor

Eliminates MCU-specific `#ifdef` spaghetti from app code. New files: `platform.h`,
`platform_stm32.cpp`, `platform_nrf52.cpp`. Five platform functions:
`platform_init()`, `platform_battery_get()`, `platform_pre_sleep()`,
`platform_ble_start()`, `platform_ble_rearm()`, `platform_batt_critical_default_mv()`.

- STM32WLE5 guard: `#if defined(STM32WLE5xx)` (boards.txt `-DSTM32WLE5xx`).
- nRF52840 guard: `#if defined(NRF52840_XXAA)` (CMSIS device define).
- STM32 `platform_pre_sleep()`: explicit ADC ADDIS before Stop 2 — guards against
  RUI3 stack re-enabling ADC (ADEN=1) via internal `bat.get()` for ADR reporting.
- nRF52 `platform_battery_get()`: `analogReadResolution(12)` set before every read
  since RUI3 stack silently resets it to 10-bit (causing ~1 V false readings).
- nRF52 BLE: `platform_ble_start(30)` once at boot for SoftDevice init;
  `platform_ble_rearm(1)` in `sendCallback` re-arms 1 s auto-stop after each TX
  (RUI3 restarts BLE advertising without timeout after LoRaWAN TX → 425 µA).
- Result: RAK3172 ~25 µA, RAK4631 ~35 µA steady-state after BLE window expires.

## [v27] — Fix nRF52840 Mode B sleep current

`analogRead()` on nRF52840 leaves SAADC enabled (ENABLE=1). Unlike STM32WLE5
Stop 2, this does not block sleep entry, but an enabled-idle SAADC draws ~0.5–1 mA
during System ON sleep.

- Fix: add explicit `NRF_SAADC->ENABLE = Disabled` after Mode B battery reads.
- Guard debug-only `api.system.bat.get()` behind `MY_DEBUG>0` to avoid redundant
  SAADC acquisition in production builds.

## [v26] — Fix STM32WLE5 Mode B sleep current regression (~45 µA → ~24 µA)

Root cause: `analogRead(WB_A0)` leaves ADC ADEN=1, blocking Stop 2 entry.

- Fix: skip WB_A0 on RAK3172; return VDDA from Vrefint (pin 254) as battery proxy.
  Vrefint internal-channel path is the only `analogRead()` that clears ADEN on exit.
  VDDA (~3.3 V LDO) droops near depletion — sufficient to catch a dying cell.
- nRF52840 (no Stop 2 restriction) unchanged.

## [v25] — Code review fixes and power improvements

- `motion_detected` flag now reset unconditionally in `send_acc_alert()` so ISR
  re-arms regardless of TX outcome (was stuck if `send_packet()` returned false).
- `setup_rak1904()`: CTRL1 register write-verify with retry; returns false on mismatch.
- `shutdown_rak1904()` added (keeps configured ODR active during MCU sleep — changing
  to 1 Hz broke interrupt timing since duration counter ticks at ODR rate).
- `startup_rak1904()` is now a no-op; ODR is unchanged by shutdown.
- LPS35HW `read_rak1902()`: replaced fixed 100 ms delay with a 40 ms polling loop on
  the pressure-data-ready bit (P_DA). Typical latency drops from 100 ms to ~12 ms.
- Battery ADC (Mode B): added 2 ms inter-sample delay so S&H cap fully charges between
  samples for accurate averaging.
- RAK1901/RAK1902 reads guarded with `!has_rak1906` to prevent LPP channel 2/3/4
  overwrite when BME680 is present (shared channels T/H/pressure).
- OPT3001: removed redundant `Wire.begin()` from `init_rak1903()`; log now shows raw
  float lux (no precision-losing `uint16_t` cast).
- RAK1906 gas resistance: rounded to nearest kΩ (+ 500 before /1000) instead of floor.
- `save_at_setting()`: added flash read-back verification (`memcmp`) after write.
- `get_at_setting()`: fresh flash now initialises `batt_critical_mv = BATT_CRITICAL_MV_DEFAULT`.
- STATUS AT command: AppKey/NwkSKey/AppSKey output wrapped in `#if MY_DEBUG > 0`.
- `MYLOG` and `AT_PRINTF` macros: replaced `delay(100)` with `Serial.flush()` —
  eliminates 100 ms blocking stall per debug print.

## [v24] — Fix OTAA race condition on cached session restore

When RUI3 restores a cached OTAA session, `+EVT:JOINED` fires inside the
`api.lorawan.join()` call in `setup()`, which means `joinCallback` runs (sets
ADR=on, TXP=POST_JOIN_TXP) **before** `setup()` reaches that code. `setup()` then
overwrites with ADR=off, TXP=0 and calls `api.lorawan.join()` again. That second join
attempt triggers a new OTAA exchange; if the gateway doesn't respond in time the session
is lost and the device spends 255 s waiting to retry.

- Fix: guard the join-profile + `join()` block with `api.lorawan.njs.get()` — skip it
  entirely when already joined.

## [v23] — Reduce TX power immediately after join

Set POST_JOIN_TXP (index 2 ≈ 12 dBm EIRP) immediately after OTAA join succeeds, in
both `joinCallback` and the already-joined end-of-setup path. ADR was enabled but TXP
stayed at JOIN_MAX_TXP=0 (+16 dBm) until the network sent a `LinkADRReq` — in
close-range deployments this saturated the gateway LNA causing wild RSSI swings
(−30 to −120 dBm) and COLLISION_PACKET errors on the RX1 downlink. Starting at a
moderate power lets ADR adjust in either direction without the first uplinks
overwhelming the gateway.

## [v22] — Add RAK1906 (BME680) environmental sensor

Added RAK1906 (BME680): temperature, humidity, pressure on shared channels 2/3/4
(replaces RAK1901+RAK1902 on those channels), gas resistance (kΩ) on channel 14 via
`addVoc_index` (LPP type 0x8A).

- Fixed RAK4631-R Serial6 not starting properly: `Serial6.begin()` was called at end of
  `setup()` but `MYLOG` used `Serial6.printf()` throughout — moved `Serial6.begin()` to
  immediately after `Serial.begin()`.

## [v21] — Support RAK4631; fix join retry

- Supporting RAK4631.
- Fixed join retry after gateway-down scenario: `JOIN_RETRY_SEC` was 300 s which exceeds
  the RUI3 valid range (7–255 s) — join auto-retry silently never started. Lowered to 30 s.
- Changed attempt count from 0 (may mean "stop" on some builds) to 255 (retry ~35 h then stop).
- Added return-value check on `api.lorawan.join()`.

## [v20] — RAK19009 battery supervision fix (final)

Fix Vrefint read in both modes. Root cause: AVREF = pin 195 on WisDuoRAK3172Eval board
(`PNUM_ANALOG_BASE + NUM_ANALOG_INPUTS + 1`), but `uhal_adc_read` only handles internal
channels at pins 253/254/255 — pin 195 returns `UDRV_NACK`, `*value` is uninitialized
stack memory (=8192). `api.system.bat.get()` reads WB_A0 (voltage-divider pin on
RAK19007), which floats near VDD on RAK19009 giving a false 4.2 V.

- Fix: `analogRead(254)` = `UDRV_ADC_CHANNEL_VREFINT` at 12-bit resolution; HAL
  auto-enables VREFEN via `HAL_ADC_ConfigChannel`.
  `VBAT = (3000 * cal) / (raw * 1000)`.
- Also fixes Mode B: same AVREF bug + WB_A0 now explicitly read at 12-bit so the
  `/4095.0f` divisor is correct (was broken at RUI3 default 10-bit resolution).

## [v19] — Two-mode battery sensing via STM32WLE5 Vrefint

Compile-time `BATT_SENSE_MODE` flag selects:

- **Mode A** (VBAT=VDD): Vrefint only — true cell voltage, shutdown reliable at 2.7 V.
- **Mode B** (VBAT>VDD, e.g. RAK19007): WB_A0 voltage divider on VBAT (÷0.481);
  Vrefint calibrates VDDA for an accurate result. If `analogRead(AVREF)` does not map
  to the internal Vrefint channel, firmware detects out-of-range VDDA and falls back to
  hardcoded 3.3 V.
- Mode B default threshold lowered to 3100 mV (below the 3.3 V LDO output) to prevent
  false-positive shutdowns on a healthy battery.
- `ATC+BATCRIT=0` disables runtime shutdown (boot guard still fires at compile-time default).
- `ATC+BATCRIT` range expanded: 0 (disabled) or 2000–4200 mV.

## [v18] — Configurable battery shutdown threshold

Configurable battery shutdown threshold (`ATC+BATCRIT` in mV, stored in flash).
Simplified battery voltage read: unified path via `getBatteryVoltage()` (lower bound 0.5 V).

## [v17] — Explicit join process

Explicit join process: OTAA credential check, enforce NJM/band/class in code,
ADR off + max TXP for join, ADR on after success, timer started only after join,
auto-retry via RUI3 join API.

## [v16] — Battery protection

Boot guard + runtime shutdown below 2.7 V.

## [v15] — Debug logging cleanup

Clean up the debug logging and fixed finding two RAK1904 devices on the I2C bus
(SDO pin high/low).

## [v14] — Switch debounce fix

Fixed switch_gpio debounce logic and added direct switch state refresh in
`sensor_handler` to catch any missed FALLING-edge wakeups. Added more logging.

## [v13] — Fix channel conflicts and double TX

Fixed channel conflicts for switch state and acc alert. Fixed double TX on gpio switch.

## [v12] — Fix spurious ACC TX and LPP channel conflicts

Fixed spurious ACC TX by moving `setup_rak1904` out of `manageSensors` (now called at
init and on downlink only). Fixed LPP channel conflicts (unique channels 1–11). Removed
duplicate `wisblock_cayenne.h` include and redundant `Wire.begin` in `init_rak1904`.
Reduced warmup to 200 ms, battery samples to 3 (no delay).

## [v11] — ACC ISR safety and flash fixes

Fixed ACC ISR safety (deferred to RAK_TIMER_1), fixed flash save return value, restored
`min_interval` from flash, fixed uninitialized register read in `rak1904_readRegister`,
preserved LPM bit in `setup_rak1904`.

## [v10] — Battery reading improvements

Improved battery reading with averaging and validation. Reduced sensor warmup time to 1 second.

## [v9]

Send digital output channel 1–4 every hour and on boot. Send data directly after joining network.

## [v8] — `RUI3_RAK3172_sensor_07.3`

Send battery data and digital outputs (channels 1–4) every hour and on first boot.

## [v7] — `RUI3_RAK3172_sensor_07.2`

Send battery data every hour.

## [v6] — `RUI3_RAK3172_sensor_07`

Only send data for "Digital_Outputs" and battery if values has changed.

## [v5] — `RUI3_RAK3172_sensor_06`

Adding switch sensor using RAK19007 and adding the results to the normal payload.

## [v4] — `RUI3_RAK3172_sensor_05`

Rearranged payload data channels to more logical layout for both up- and downlink.

## [v3] — `RUI3_RAK3172_sensor_04`

Read Downlink and update the RAK1904 with `setup_rak1904()`.

## [v2] — `RUI3_RAK3172_sensor_03`

Added RX and minor clean up.

## [v1] — `RUI3_RAK3172_sensor_02`

Added version byte and changed switch output to digital output at 1904 interrupt.
