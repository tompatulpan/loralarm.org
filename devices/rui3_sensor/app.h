/**
 * @file app.h
 * @author Bernd Giesecke (bernd@giesecke.tk)
 * @brief Defines, includes, global definitions
 * @version 0.1
 * @date 2023-05-04
 * 
 * @copyright Copyright (c) 2023
 * 
 */
#include <Arduino.h>
#include "wisblock_cayenne.h"
#include <CayenneLPP.h>

extern bool gpioInterruptEnabled;
extern volatile bool switch_state;
extern uint8_t previous_switch_state;
extern volatile bool motion_detected;

extern uint8_t rx_data_0;
extern uint8_t rx_data_1;
extern uint8_t rx_data_2;

extern uint8_t interrupt_threshold;
extern uint8_t acc_frequency;
extern uint8_t interrupt_duration;
extern bool acc_config_pending;  // Set in receiveCallback, applied safely in sensor_handler


// Debug
// Debug output set to 0 to disable app debug output
#ifndef MY_DEBUG
#define MY_DEBUG 1
#endif

#if MY_DEBUG > 0
#if defined(STM32WLE5xx)
// Single-call printf avoids RTOS task-switch tearing between tag and body
#define MYLOG(tag, fmt, ...)                          \
	do                                                \
	{                                                 \
		Serial.printf("[%s] " fmt "\n", tag, ##__VA_ARGS__); \
		Serial.flush();                               \
	} while (0)
#else // RAK4630 || RAK11720
#define MYLOG(tag, fmt, ...)                                   \
	do                                                         \
	{                                                          \
		Serial.printf("[%s] " fmt "\r\n", tag, ##__VA_ARGS__); \
		Serial6.printf("[%s] " fmt "\r\n", tag, ##__VA_ARGS__); \
		Serial.flush();                                        \
		Serial6.flush();                                       \
	} while (0)
#endif
#else
#define MYLOG(...)
#endif

/** Custom flash parameters structure */
struct custom_param_s
{
	uint8_t valid_flag = 0xAA;
	uint32_t send_interval = 0;
	uint32_t min_interval = 0;
	uint8_t interrupt_threshold = 0x08;
	uint8_t interrupt_duration = 0x01;
	uint8_t acc_frequency = 2;
	uint16_t batt_critical_mv = 2800;  // Shutdown threshold in millivolts (ATC+BATCRIT); 0 = disabled
};

/** Custom flash parameters */
extern custom_param_s custom_parameters;

#define SW_INT_PIN WB_IO1

// Accelerometer interrupt pin — property of the BASE BOARD slot, not the MCU module.
// Both RAK3172 and RAK4631-R on RAK19003 Slot D use WB_IO5.
// Change if your RAK1904 is in a different slot.
#define ACC_INT_PIN WB_IO5  // Slot D on RAK19003

// Platform abstraction — all MCU-specific code (battery ADC, Serial6, BLE,
// WB_IO2 power rail) lives in platform_stm32.cpp / platform_nrf52.cpp.
// This header exposes the device-agnostic API used by the rest of the app.
#include "platform.h"

// LoRaWAN join configuration (set in code, not by AT commands)
#define LORA_REGION    4    // EU868 (0=EU433, 4=EU868, 5=US915 — see regions_list in custom_at.cpp)
#define JOIN_DR        3    // Join data rate: DR3=SF9@125kHz EU868; use DR0 for maximum range robustness
#define JOIN_MAX_TXP   0    // LoRaWAN TXP index 0 = max EIRP for region (LoRaWAN std: 0=max, 7=min)
#define JOIN_RETRY_SEC 255  // Seconds between auto-rejoin attempts (valid RUI3 range: 7-255 s)
#define BATTERY_CHECK_INTERVAL_MS 60000UL // Protection cadence while unjoined or periodic TX is disabled
// After a successful join the device stays at JOIN_MAX_TXP (max EIRP) until the network
// sends a LinkADRReq. In close-range deployments this saturates the gateway LNA, causing
// wild RSSI swings and COLLISION_PACKET on the RX1 downlink. Starting at a moderate level
// lets ADR adjust upward (if range demands it) or further downward.
// EU868 TXP index: 0=+16 dBm, 1=+14, 2=+12, 3=+10, 4=+8, 5=+6 dBm EIRP.
#define POST_JOIN_TXP  2    // ~12 dBm EIRP after join; ADR manages from here

// Forward declarations
void sensor_handler(void* unused);
bool send_packet(void);
void enterLowBatteryShutdown(float voltage);
bool init_status_at(void);
bool init_interval_at(void);
bool init_min_interval_at(void);
bool init_batt_crit_at(void);
bool get_at_setting(void);
bool save_at_setting(void);
extern volatile time_t last_trigger;

// LoRaWAN stuff
// Cayenne LPP Output Channel numbers — each sensor/measurement gets a unique channel
#define LPP_CHANNEL_BATT_1  1    // Battery voltage
#define LPP_CHANNEL_TEMP_1  2    // RAK1901 temperature
#define LPP_CHANNEL_HUMID_1 3    // RAK1901 humidity
#define LPP_CHANNEL_PRESS_1 4    // RAK1902 pressure
#define LPP_CHANNEL_LIGHT_1 5    // RAK1903 light
#define LPP_CHANNEL_ACC_1   6    // RAK1904 accelerometer
#define LPP_CHANNEL_DIG_1   7    // version byte
#define LPP_CHANNEL_DIG_2   8    // RAK1904 threshold level
#define LPP_CHANNEL_DIG_3   9    // RAK1904 duration value
#define LPP_CHANNEL_DIG_4   10   // RAK1904 acc-sensor update frequency
#define LPP_CHANNEL_DIG_5   11   // switch state (GPIO)
#define LPP_CHANNEL_DIG_6   12   // RAK1904 motion detection alert
#define LPP_CHANNEL_DIG_7   13   // low-battery shutdown notification
#define LPP_CHANNEL_GAS_1   14   // RAK1906 gas resistance (kΩ)

extern WisCayenne g_solution_data;

// Sensor functions
bool init_rak1901(void);
void read_rak1901(void);
void startup_rak1901(void);
void shutdown_rak1901(void);
bool init_rak1902(void);
void read_rak1902(void);
void startup_rak1902(void);
void shutdown_rak1902(void);
bool init_rak1903(void);
void read_rak1903();
void startup_rak1903(void);
void shutdown_rak1903(void);
bool init_rak1906(void);
void read_rak1906(void);
void startup_rak1906(void);
void shutdown_rak1906(void);

// Acceleration stuff
bool rak1904_writeRegister(uint8_t chip_reg, uint8_t dataToWrite);
bool rak1904_readRegister(uint8_t *outputPointer, uint8_t chip_reg);
bool init_rak1904(void);
void int_callback_rak1904(void);
void send_acc_alert(void* unused);
void clear_int_rak1904(void);
void read_rak1904(void);
void startup_rak1904(void);
void shutdown_rak1904(void);
bool setup_rak1904(void);

// Circuit Switch stuff
bool init_gpio(void);
void switch_int_handler(void);
void switch_bounce_check(void* unused);  // Changed
void handle_gpio(void* unused);          // Changed
