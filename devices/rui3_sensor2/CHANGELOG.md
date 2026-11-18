# Changelog

## [v101] — RAK3172 battery sensing mode (ATC+BATMODE)

RAK3172 ships in two supply layouts: the cell feeds VDD directly (no regulator), or the cell
sits behind an LDO on the carrier board. The old Vrefint/VDDA sensing can only measure the
first — behind an LDO it reports the regulator output and low-battery supervision silently
stops working (the boot guard and latch can never trip until the cell collapses).

- New flash-backed `ATC+BATMODE` (RAK3172 only): `0` = Mode A (default) — VDDA via the internal
  Vrefint reference, for cells on VDD. `1` = Mode B — external high-impedance divider
  (>= 10 MOhm total, 100 nF at the pin; ~0.4 uA at 4 V) from the cell to `BATT_SENSE_PIN`
  (`config.h`, default `WB_A0`, set to match carrier wiring), with the ADC scale taken from
  Vrefint so LDO tolerance cancels. Calibrate the ratio as `BATT_SENSE_DIVIDER` in
  `platform_stm32.cpp`: `new = current * multimeter_V / code_V`; uncalibrated it reads low,
  which fails safe. Ignored on RAK4631, which always reads its fixed `WB_A0` divider.
- The new flash field sits in the parameter struct's always-zero padding, so existing devices
  keep their stored settings and start in Mode A. `ATC+STATUS` reports the mode.
- `battery_boot_guard()` still runs before settings load and measures the default mode: on LDO
  devices the boot guard sees the regulated VDDA (passes); the runtime latch in `loop()` then
  supervises the cell with the saved mode. No change for direct-battery devices.
- RAK3172 sleep current is unchanged: the Mode B GPIO read leaves ADC ADEN set, which
  `platform_pre_sleep()` already clears with `ADC_CR_ADDIS` before Stop 2 entry.
- Version byte 101 (`FW_VERSION_BYTE` / `FW_VERSION_STR`); payload unchanged.

## [v100] — Modular rewrite (wire-compatible with rui3_sensor v33)

New firmware line, `devices/rui3_sensor2`, ported from `rui3_sensor` v33. No payload, AT-command,
downlink or server change; see README.md for the module map.

- `.ino` reduced to `setup()` / `loop()`; logic moved to `battery`, `uplink`, `lorawan`, `sensors`
  and `state` modules.
- Sensor registry (`sensors.cpp`): one table replaces five hand-written `has_rakXXXX` blocks in
  init, wake-up, read and shutdown. The RAK1906-supersedes-RAK1901/1902 rule is the `suppressed_by`
  field, used by both the heartbeat and the motion alert, so the two can no longer disagree.
- All shared globals declared in `state.h` with the contexts that may touch them; unused
  `rx_data_*` and dead commented-out code removed.
- Pure decision rules extracted to `battery_policy.h`, `send_policy.h`, `downlink_parse.h` with
  host tests (`tests/host`, 51 checks).
- Fix: downlink values above 255 wrapped in a `uint8_t` (`300` -> `44`) and were accepted on
  port 5; they are now rejected as out of range.
- Link policy after join is now configurable (`POST_JOIN_ADR/TXP/DR` in `config.h`) and defaults to
  **fixed full power, ADR off, DR pinned to JOIN_DR (DR3)** instead of 12 dBm + ADR: the node may be
  moved closer to or further from the gateway and must still reach it. Applied the same way after a
  fresh join and after a restored cached session (previously the cached path only did it when
  `SENDINT` != 0 and always switched ADR on). Trade-offs are documented at the defines; set
  `POST_JOIN_ADR 1` / `POST_JOIN_TXP 2` for the old behaviour. `lorawan_enforce_link_policy()` runs
  before every LoRaWAN uplink; if ADR/TXP/DR have drifted from the policy (e.g. a LinkADRReq
  from the network) it logs `Link drift: ...` and restores them. Each uplink logs its DR/TXP/ADR.
- Fixed the join comment: 255 attempts x 255 s is ~18 h, not ~35 h (`JOIN_MAX_ATTEMPTS`).
- Rejoin retry: the stack gives up after the initial ~18 h join series, which left a node that was
  offline longer (gateway down, moved) unjoined until a reboot. A periodic tick (`RAK_TIMER_4`,
  `REJOIN_CHECK_INTERVAL_MS`) now starts a new short series (`REJOIN_RETRY_ATTEMPTS` = 5, ~21 min)
  after every `REJOIN_RETRY_TICKS` ticks of being unjoined, at max power and DR3, until a join
  succeeds. Production values are 1 h x 24 (daily), which are the current settings; the bench test
  values (1 min x 10) are in the `config.h` comments. It does nothing while joined and is skipped
  while the low-battery latch is active. Verified on a node: out of range, retry at 10 min, rejoin.
- Link check: "joined" only means the node holds a session, so a node whose device was deleted
  or reset in the network server kept sending into the void and never rejoined. Every
  `LINK_CHECK_TICKS` ticks (production: 24 x 1 h = daily, the current setting)
  the next uplink is sent **confirmed** with `LINK_CHECK_RETRIES` = 3 retries. The stack calls the
  send callback only after its retries: status 0 = the network answered (`Link check OK`), anything
  else = no answer, `Link check FAILED (status n)`, and `loop()` forces a fresh join with the long
  series (255 attempts). Skipped while the low-battery latch is active; if `SENDINT` is 0 the
  check waits for the next uplink. Verified on hardware (Stage A bench, 2026-11-16, `tests/bench`):
  a confirmed uplink with no answer reports status 4 (+EVT:SEND_CONFIRMED_FAILED(4)), the forced
  rejoin runs while the stack still reports joined, and the node recovered ~3 min after the
  gateway returned (incl. gateway boot). The serial log also prints the stack's own
  `+EVT:SEND_CONFIRMED_OK/FAILED(n)`.
- Stage B bench (2026-11-16, `tests/bench`, production values, RAK3172 EVB): the RUI3 session
  cache does **not** survive a reset on this BSP. Every reboot reads `njs=0`, `join()` returns
  false ("restore in progress or stack busy"), and the stack silently re-joins over the air
  seconds later (fCnt reset observed 3 -> 0 via MQTT; B1.5). With no coverage it prints
  `join fail! Next retry in 255 s (auto-rejoin active)` — the stack retries on its own every
  255 s, heartbeats skip (`Not joined, skip sending`), and the node rejoins within one
  interval after coverage returns (recovery incl. gateway boot <= ~6 min; B2). So plan
  outcome "U (boots unjoined)" for both soft reset and power cycle; a cached-session boot
  (outcome C) has never been observed on this board. Bench harness notes: Rui3's
  background join, the MQTT probe (ChirpStack v3 REST is gone) and the third boot outcome
  are all driven/validated by `tests/bench/bench.py` (see its README).
- Review fixes (code review of the first version):
  - `sensor_handler()` is now a thin wrapper that owns `heartbeat_busy` for the whole run and drops a
    second call that arrives mid-run (timer, `loop()` after a battery recovery, join callback).
    `battery_loop_check()` also waits instead of calling it while busy.
  - `handle_gpio()` (switch) now defers while a heartbeat is running and carries battery voltage
    and the latch flag like a motion alert (`append_supply_state()`), so a latched node never sends
    a healthy-looking switch frame.
  - `send_acc_alert()` also waits until `setup_complete`: its ISR is attached in `init_rak1904()`
    before the RAK1906 init, so an early motion event could touch I2C mid-init.
  - `ATC+SENDINT` / `ATC+MININT` reject values above 2,147,483 s (`INTERVAL_MAX_S`); larger ones
    wrapped the ms multiply (4294968 s -> 704 ms, a sub-second uplink loop with a flash write each).
  - `AT+STATUS` no longer indexes `regions_list` out of range; log text in `setup_rak1904()` said
    port 6 where the duration is port 7; compile-time check that the tick counts fit a `uint8_t`.
  - Docs/comments: the stale nRF52 SAADC text in `platform.h` / `platform_nrf52.cpp`, the motion
    comments (a failed immediate send is lost, not retried by the heartbeat) and the LORA-context
    exception in `lorawan.cpp`.
  - Switch input: `gpioInterruptEnabled` was never set anywhere, so the switch never ran. It now
    follows the new `SWITCH_ENABLED` define in `config.h` (default 0 = off, same behaviour as
    before; set 1 to enable, not yet tested on hardware).
  - Not changed: `MININT` is stored but never read (v33 compatibility).
- `version_byte` = 100 (new line; v33 stays the last of the old one).
- Motion alerts also carry temp/humidity on RAK1906 nodes (found on a real node: the alert had none).
  `read_rak1906_fast()` runs one forced measurement with the gas heater off, then restores the
  heater profile; the full reading takes ~390 ms, the quick one should be tens of ms (to be measured
  on hardware). The sensor table's `read_alert` hook replaces the old `read_in_alert` flag.
- BME680 IIR filter switched off (`BME680_FILTER_SIZE_0`, was size 3). With one forced reading per
  heartbeat each reading only moved part of the way to the real value (T decayed over several
  heartbeats on a real node after it was handled), delaying temperature alarms. Readings are now
  slightly noisier but follow changes at once. Hardware check pending.
- `[ALERT]` log line reports how many sensors were added to each motion frame.
- Verified: builds for RAK3172 (RUI3 BSP 4.3.0) at 172,256 B flash / 32,504 B RAM versus 172,648 B /
  32,360 B for `rui3_sensor` v33, and for RAK4631 (`WisCoreRAK4631Board`) at 327,892 B / 75,488 B versus
  329,840 B / 75,340 B. Not yet flash-tested on hardware.
