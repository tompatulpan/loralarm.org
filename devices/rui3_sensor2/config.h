/**
 * @file config.h
 * @brief Compile-time configuration: logging, pins, LoRaWAN join policy, battery
 *        policy, timing and the Cayenne LPP channel map.
 *
 * Everything a user may reasonably want to change when building for their own
 * hardware lives here. Runtime state is in state.h.
 */
#pragma once

#include <Arduino.h>

// ---------------------------------------------------------------------------
// Debug logging
// ---------------------------------------------------------------------------
// Debug output set to 0 to disable app debug output
#ifndef MY_DEBUG
#define MY_DEBUG 1
#endif

#if MY_DEBUG > 0
#if defined(STM32WLE5xx)
// Single-call printf avoids RTOS task-switch tearing between tag and body
#define MYLOG(tag, fmt, ...)                                 \
	do                                                       \
	{                                                        \
		Serial.printf("[%s] " fmt "\n", tag, ##__VA_ARGS__); \
		Serial.flush();                                      \
	} while (0)
#else // RAK4630 || RAK11720
#define MYLOG(tag, fmt, ...)                                    \
	do                                                          \
	{                                                           \
		Serial.printf("[%s] " fmt "\r\n", tag, ##__VA_ARGS__);  \
		Serial6.printf("[%s] " fmt "\r\n", tag, ##__VA_ARGS__); \
		Serial.flush();                                         \
		Serial6.flush();                                        \
	} while (0)
#endif
#else
#define MYLOG(...)
#endif

// ---------------------------------------------------------------------------
// Firmware identity
// ---------------------------------------------------------------------------
// Reported on ch 7 in heartbeats. This line (modular rewrite, wire-compatible with
// rui3_sensor v33) starts at 100 so it can never be mistaken for the old 3x series.
#define FW_VERSION_BYTE 102

// Version string reported by the RUI3 stack (AT+VER / api.system.firmwareVersion).
// Must track FW_VERSION_BYTE: it is the only way to identify the firmware without a join.
#define FW_VERSION_STR "rui3_sensor2-v102"

// ---------------------------------------------------------------------------
// Pins
// ---------------------------------------------------------------------------
#define SW_INT_PIN WB_IO1

// Switch / door contact input on SW_INT_PIN (interrupt on both edges, 50 ms debounce).
// 1 = enabled: immediate uplink on every change (ch 11) and the state in every heartbeat.
// 0 = disabled (default): no interrupt is attached and ch 11 is never sent.
// POWER: INPUT_PULLUP draws ~75 uA extra in sleep while the switch is closed; see the note in
// state.cpp for the external 1 MOhm pull-up that avoids it.
#ifndef SWITCH_ENABLED
#define SWITCH_ENABLED 0
#endif

// Accelerometer interrupt pin — property of the BASE BOARD slot, not the MCU module.
// Both RAK3172 and RAK4631-R on RAK19003 Slot D use WB_IO5.
// Change if your RAK1904 is in a different slot.
#define ACC_INT_PIN WB_IO5 // Slot D on RAK19003

// Battery sense divider input for LDO boards (RAK3172 only; active when ATC+BATMODE=1).
// The carrier board feeds the cell through a high-impedance divider to this ADC-capable pin;
// the ratio is calibrated with BATT_SENSE_DIVIDER in platform_stm32.cpp.
// Ignored on RAK4631, which always reads the fixed WB_A0 divider. Set to match carrier wiring.
#ifndef BATT_SENSE_PIN
#define BATT_SENSE_PIN WB_A0
#endif

// ---------------------------------------------------------------------------
// LoRaWAN join policy (set in code, not by AT commands)
// ---------------------------------------------------------------------------
#define LORA_REGION 4			// EU868 (0=EU433, 4=EU868, 5=US915 — see regions_list in custom_at.cpp)
#define JOIN_DR 3				// Join data rate: DR3=SF9@125kHz EU868; use DR0 for maximum range robustness
#define JOIN_MAX_TXP 0			// LoRaWAN TXP index 0 = max EIRP for region (LoRaWAN std: 0=max, 7=min)
#define JOIN_RETRY_SEC 255		// Seconds between auto-rejoin attempts (valid RUI3 range: 7-255 s)
#define JOIN_MAX_ATTEMPTS 255	// 255 attempts x JOIN_RETRY_SEC (~18 h) before the stack gives up
// Rejoin retry: if still unjoined after the initial series, start a short new series every
// REJOIN_RETRY_TICKS ticks of REJOIN_CHECK_INTERVAL_MS (RAK_TIMER_4).
// PRODUCTION (current): 3600000UL (1 h) x 24 ticks = daily; the period must stay longer than
// the initial series (~18 h) so a series is never restarted while one is still running.
// TEST: 60000UL (1 min) x 10 ticks = every 10 min. In that mode the retry also interrupts the
// initial series and writes 2 KB of flash per series, so never deploy it.
// 5 x 255 s is ~21 min per series.
#define REJOIN_CHECK_INTERVAL_MS 3600000UL
#define REJOIN_RETRY_TICKS 24
#define REJOIN_RETRY_ATTEMPTS 5

// Link check (joined nodes): every LINK_CHECK_TICKS ticks of REJOIN_CHECK_INTERVAL_MS the next
// uplink is sent as CONFIRMED with LINK_CHECK_RETRIES retries. If the network answers nothing,
// the session is dead (device deleted/reset in the network server, frame counters out of
// step, gateway gone) and the node forces a fresh join with the long series (JOIN_MAX_ATTEMPTS).
// PRODUCTION (current): 24 ticks of 1 h = daily. TEST: 10 ticks of 1 min = every 10 min.
#define LINK_CHECK_TICKS 24
#define LINK_CHECK_RETRIES 3
// The tick counters in lorawan.cpp are uint8_t
static_assert(REJOIN_RETRY_TICKS < 256 && LINK_CHECK_TICKS < 256, "tick counters are uint8_t");
// Link policy after a successful join (also applied when a cached session is restored).
// Fixed full power, ADR off: the node may be moved closer to or further from the gateway
// and must still reach it, and the network never tunes it down. Trade-offs: more TX
// energy per uplink than a tuned link, and no automatic DR change, so a node that ends up
// far away must be fixed by hand (POST_JOIN_DR / AT+DR).
// Alternative that was used before: POST_JOIN_ADR 1 and POST_JOIN_TXP 2. Close to a gateway
// full power can saturate its LNA (wild RSSI swings, COLLISION_PACKET on the RX1 downlink).
// EU868 TXP index: 0=+16 dBm, 1=+14, 2=+12, 3=+10, 4=+8, 5=+6 dBm EIRP.
#define POST_JOIN_ADR 0			// 1 = network adapts DR/TXP (ADR), 0 = fixed
#define POST_JOIN_TXP JOIN_MAX_TXP // TXP index kept after join (0 = max)
#define POST_JOIN_DR JOIN_DR	// DR kept after join; only applied when POST_JOIN_ADR is 0

// ---------------------------------------------------------------------------
// Battery supervision
// ---------------------------------------------------------------------------
#define BATTERY_CHECK_INTERVAL_MS 60000UL // Protection cadence while unjoined or periodic TX is disabled
#define BATT_RESUME_MARGIN_V 0.05f		  // Hysteresis: supply must exceed the critical threshold by this margin
										  // before telemetry resumes after a low-battery shutdown
#define BATT_LATCH_CONFIRM_READS 3		  // A low reading must repeat this many times in a row (BATT_LATCH_CONFIRM_GAP_MS apart)
#define BATT_LATCH_CONFIRM_GAP_MS 100	  // before the shutdown latch is set; one outlier never latches the device
#define BATT_LEGACY_DEFAULT_MV 2800		  // v30/v31 default; a stored value equal to it is migrated to a lower platform default
#define BOOT_GUARD_RECHECK_MS 60000UL	  // Boot-guard re-check cadence while the supply is critical at boot:
										  // timed sleep so the MCU wakes even with no external wake source
										  // (no GPIO interrupts/timers/LoRa configured yet at that point)

// ---------------------------------------------------------------------------
// Timing and payload
// ---------------------------------------------------------------------------
const uint16_t SENSOR_WARMUP_TIME = 200;	  // 200ms: OPT3001 needs 100ms conversion + margin
const uint8_t MAX_PAYLOAD_SIZE = 255;		  // Cayenne buffer cap (P2P frames and backstop only).
											  // The live LoRaWAN budget is the FRMPayload limit at
											  // the current DR (DR0-DR2: 43, DR3-DR5: 107 bytes in
											  // EU868) — see payload_budget.h and uplink_arm_frame_budget()
const uint8_t BATTERY_SEND_INTERVAL = 1;	  // Send battery status and digital outputs every N heartbeats
#define INTERVAL_MAX_S 2147483UL			  // Largest AT+SENDINT / AT+MININT in seconds: x1000 must fit a uint32_t (ms)

// ---------------------------------------------------------------------------
// Cayenne LPP channel map — each sensor/measurement gets a unique channel.
// The server codecs (server/chirpstack/codecs/payload_codec_rui3.js) rely on it.
// ---------------------------------------------------------------------------
#define LPP_CHANNEL_BATT_1 1   // Battery voltage
#define LPP_CHANNEL_TEMP_1 2   // RAK1901 / RAK1906 temperature
#define LPP_CHANNEL_HUMID_1 3  // RAK1901 / RAK1906 humidity
#define LPP_CHANNEL_PRESS_1 4  // RAK1902 / RAK1906 pressure
#define LPP_CHANNEL_LIGHT_1 5  // RAK1903 light
#define LPP_CHANNEL_ACC_1 6	   // RAK1904 accelerometer
#define LPP_CHANNEL_DIG_1 7	   // version byte
#define LPP_CHANNEL_DIG_2 8	   // RAK1904 threshold level
#define LPP_CHANNEL_DIG_3 9	   // RAK1904 duration value
#define LPP_CHANNEL_DIG_4 10   // RAK1904 acc-sensor update frequency
#define LPP_CHANNEL_DIG_5 11   // switch state (GPIO)
#define LPP_CHANNEL_DIG_6 12   // RAK1904 motion detection alert
#define LPP_CHANNEL_DIG_7 13   // low-battery shutdown latch state (1 = latched), sent in heartbeats and motion frames
#define LPP_CHANNEL_GAS_1 14   // RAK1906 gas resistance (kΩ)
