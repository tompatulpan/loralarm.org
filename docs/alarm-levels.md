# Alarm levels — consolidated list and sorting

Reference for the **severity** of every alarm kind (the PWA currently mirrors it in
`WARN_KINDS`; see *Implementation status*). The model:
*every parameter set in `families` (defaults) and per device in `devices`
(override) is an alarm* — the thresholds are data, the kinds are behavior.

## The principle

| Level | Meaning | Visual (PWA) |
|---|---|---|
| **ALARM** (red) | threat to property/security, or *lost supervision* — the device can no longer monitor | Left-edge strip (Z0) red + row tint, red blinking LED, red pill |
| **WARNING** (amber) | degraded health, not acute — keep an eye on it, nothing is happening right now | Z0 amber strip + tint, amber LED (steady), amber pill |
| **INFO** (no level) | events without severity: arm/disarm notifications, settings downlinks | no Z0; `AV` (off) pill / log entry |

## ALARM (red)

| Kind | Triggered by | Parameter (family → devices[]) |
|---|---|---|
| `motion` | motion detection (accelerometer interrupt) | `motion_mode`, `motion_window_s`, `motion_min_detections` |
| `door` | door open (contact) | rule in `dragino_lds02`; `kind: door` in devices[] |
| `water_leak` | water detected | rule in `browan_tbwl100`; `kind: water` |
| `circuit` | circuit break / contact pulse | `circuit_mode`, `circuit_hold_s` |
| `tilt` | lift/displacement beyond the baseline | `threshold` (ACC) / `tilt_sense_g` |
| `button` | panic button pressed (pulse) | rule in `milesight_ws101`; `kind: button` |
| `temp_low` | temperature ≤ lower limit (frost risk) | `temp_alarm_min_c` (+`temp_alarm_hysteresis_c`) |
| `temp_high` | temperature ≥ upper limit (`None` = off) | `temp_alarm_max_c` |
| `stale` | no uplink within the watchdog — *supervision lost* (a green "no alarms" would be misleading) | `watchdog_hours` (default 24) |
| `battery_empty` | battery at the empty threshold — the device dies shortly | `batt_empty_v` (+`batt_alarm_hysteresis_v`) |
| `battery_shutdown` | the device has shut down its periodic uplink (critical cell) | flag from the RUI3 firmware (`low_battery_shutdown`) |

## WARNING (amber)

| Kind | Triggered by | Parameter |
|---|---|---|
| `battery_low` | battery below the warning threshold — still working, plan maintenance | `batt_warn_v` (+`batt_alarm_hysteresis_v`) |
| `button_battery` | percentage battery (Milesight) below the threshold | `batt_low_pct` (default 20) |

Note: `battery_empty` and `battery_shutdown` are **alarms** (red), not
warnings — the device stops supervising. `battery_low`/`batt_warn_v` is the
warning you get beforehand.

## INFO (no level)

| Kind | Meaning |
|---|---|
| `armed` (on/off) | arm/disarm notifications + log; the fleet row shows `AV · HH:MM` in its pill |
| `settings_f4..7` | ACC settings pushed as downlinks — log |

## Recent (pulse) alarms

Pulse alarms (e.g. `button`) that fired within the recent window and were not
cleared or dismissed are not INFO: they keep the level of their kind. On the
fleet row they count like active alarms (Z0 strip, pill colour, sort order).
In the banner list they follow the active alarms, newest first.

## Planned

| Kind | Triggered by | Parameter |
|---|---|---|
| `signal_weak` *(WARNING; visual state only, no notification)* | RSSI/SNR below threshold; marginal reception, relocate the device/gateway | proposal: `rssi_warn_dbm` / `snr_warn_db` in `params` (today hardcoded −60/−80/−100 dBm resp. SNR 5/0 in the PWA's signal bars) |

Current status (reviewed 2026-11-23): **not implemented — no server-side
warning exists; the PWA signal bars are the only signal-strength indication,
and they are display-only.**

- Server carries the values but never acts on them: RSSI/SNR from the first
  `rxInfo` entry (`event.py:57`) ride on `Frame` (`frame.py:44`), are kept as
  per-device `last_rssi`/`last_snr` (`alarms.py:363`, persisted in the
  snapshot) and exposed to the PWA in the state API (`webapp.py:301`). There
  is no `SIGNAL_WEAK` in `AlarmKind` (`alarms.py:44`), no detector, no event,
  no notification, no config parameters.
- PWA computes the bars per uplink in the client (`PWA/web/app.js:335-346`:
  4/3/2/1 bars at RSSI ≥ −60/−80/−100 dBm; colour from SNR ≥ 5 ok, ≥ 0 warn,
  else bad — falling back to RSSI ≥ −80/−100 when SNR is unknown). Because
  `signal_weak` is not in `WARN_KINDS` (`app.js:29`), it does not affect
  level sorting, banners, Z0 strips or pushes.
- Implementation gaps to settle before building it: thresholds move from
  `app.js` to config (per-device/family, with the multi-instance per-house
  `devices.json` carrying them naturally); hysteresis or an N-consecutive
  debounce to avoid flapping at the threshold; whether an SNR-less uplink
  falls back to RSSI thresholds as the PWA does today; and whether it should
  forewarn the `stale` watchdog (marginal reception predicts lost uplinks).

## Sorting

1. **ALARM** rows (red)
2. **WARNING** rows (amber)
3. **OK** — armed and quiet (by name)
4. **AV** (off) / dimmed rows last (by name)

Applies to the fleet list (rows with active or recent alarms are leveled by
their worst kind) and to the active alarm banners (red before amber; the
server's order is kept within a level).

A disarmed device is silent everywhere: its active and recent alarms are
hidden from the API (banners, fleet row, counts) and not pushed, so it sorts
as dimmed. Re-arming shows and re-notifies any condition that is still active.

## Implementation — status

- **PWA (done):** `WARN_KINDS` = the warning kinds; fleet + active banners
  sort by level; Z0 strip, pill, LED and banner dot are colored per level
  (`PWA/web/app.js`).
- **Next step (server):** move the level into the `alarms.py` registry
  (`AlarmKind.severity: "alarm" | "warning"`) and expose `level` in
  `/api/state` + `/api/devices` — then `WARN_KINDS` in the PWA can disappear,
  and notification texts share the same truth from one place.
