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
#define FW_VERSION_BYTE 100

// ---------------------------------------------------------------------------
// Pins
// ---------------------------------------------------------------------------
#define SW_INT_PIN WB_IO1

// Accelerometer interrupt pin — property of the BASE BOARD slot, not the MCU module.
// Both RAK3172 and RAK4631-R on RAK19003 Slot D use WB_IO5.
// Change if your RAK1904 is in a different slot.
#define ACC_INT_PIN WB_IO5 // Slot D on RAK19003

// ---------------------------------------------------------------------------
// LoRaWAN join policy (set in code, not by AT commands)
// ---------------------------------------------------------------------------
#define LORA_REGION 4			// EU868 (0=EU433, 4=EU868, 5=US915 — see regions_list in custom_at.cpp)
#define JOIN_DR 3				// Join data rate: DR3=SF9@125kHz EU868; use DR0 for maximum range robustness
#define JOIN_MAX_TXP 0			// LoRaWAN TXP index 0 = max EIRP for region (LoRaWAN std: 0=max, 7=min)
#define JOIN_RETRY_SEC 255		// Seconds between auto-rejoin attempts (valid RUI3 range: 7-255 s)
// After a successful join the device stays at JOIN_MAX_TXP (max EIRP) until the network
// sends a LinkADRReq. In close-range deployments this saturates the gateway LNA, causing
// wild RSSI swings and COLLISION_PACKET on the RX1 downlink. Starting at a moderate level
// lets ADR adjust upward (if range demands it) or further downward.
// EU868 TXP index: 0=+16 dBm, 1=+14, 2=+12, 3=+10, 4=+8, 5=+6 dBm EIRP.
#define POST_JOIN_TXP 2			// ~12 dBm EIRP after join; ADR manages from here

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
const uint8_t MAX_PAYLOAD_SIZE = 255;
const uint8_t BATTERY_SEND_INTERVAL = 1;	  // Send battery status and digital outputs every N heartbeats

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
