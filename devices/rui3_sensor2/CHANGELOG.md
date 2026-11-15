# Changelog

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
- `version_byte` = 100 (new line; v33 stays the last of the old one).
- Motion alerts also carry temp/humidity on RAK1906 nodes (found on a real node: the alert had none).
  `read_rak1906_fast()` runs one forced measurement with the gas heater off, then restores the
  heater profile; the full reading takes ~390 ms, the quick one should be tens of ms (to be measured
  on hardware). The sensor table's `read_alert` hook replaces the old `read_in_alert` flag.
- `[ALERT]` log line reports how many sensors were added to each motion frame.
- Verified: builds for RAK3172 (RUI3 BSP 4.3.0) at 172,256 B flash / 32,504 B RAM versus 172,648 B /
  32,360 B for `rui3_sensor` v33, and for RAK4631 (`WisCoreRAK4631Board`) at 327,892 B / 75,488 B versus
  329,840 B / 75,340 B. Not yet flash-tested on hardware.
