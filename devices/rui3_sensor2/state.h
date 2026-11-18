/**
 * @file state.h
 * @brief All cross-module runtime state, declared in one place.
 *
 * Definitions live in state.cpp. Execution contexts on RUI3:
 *   LOOP   - loop() and anything it calls
 *   TIMER  - RAK_TIMER_x callbacks (sensor_handler, send_acc_alert, switch timers)
 *   ISR    - GPIO interrupt handlers (keep minimal: set a flag, start a timer)
 *   LORA   - LoRaWAN stack callbacks (join/rx/tx); never do I2C or flash here
 */
#pragma once

#include <Arduino.h>
#include "config.h"
#include "wisblock_cayenne.h"
#include "send_policy.h"

/** Custom flash parameters structure (persisted by save_at_setting()) */
struct custom_param_s
{
	uint8_t valid_flag = 0xAA;
	uint32_t send_interval = 0;
	uint32_t min_interval = 0;
	uint8_t interrupt_threshold = 0x08;
	uint8_t interrupt_duration = 0x01;
	uint8_t acc_frequency = 2;
	uint16_t batt_critical_mv = 2500; // Shutdown threshold in millivolts (ATC+BATCRIT); 0 = disabled. get_at_setting() applies the platform default
	uint8_t batt_sense_mode = 0;	   // RAK3172 only (ATC+BATMODE): 0 = Mode A VDDA via Vrefint (cell feeds VDD directly), 1 = Mode B external
									   // divider on BATT_SENSE_PIN (cell behind an LDO). Ignored on RAK4631, which always reads the WB_A0 divider.
};

/** Custom flash parameters. Written by: AT handlers, get_at_setting() (LOOP/TIMER). */
extern custom_param_s custom_parameters;

// --- Payload ---------------------------------------------------------------
/** Shared payload buffer. Owner is whoever holds heartbeat_busy, else the alert/switch timer. */
extern WisCayenne g_solution_data;
/** True while sensor_handler owns g_solution_data and the I2C bus; send_acc_alert() defers. */
extern volatile bool heartbeat_busy;

// --- Firmware identity and LoRaWAN TX settings -----------------------------
extern uint8_t version_byte;
extern bool g_confirmed_mode;	// AT+CFM
extern uint8_t g_confirmed_retry; // AT+RETY
extern uint8_t g_data_rate;
extern uint8_t set_fPort;

// --- Boot / join sequencing ------------------------------------------------
/** Set at the end of setup(); guards joinCallback TX. Written LOOP, read LORA. */
extern bool setup_complete;
/** Set once the send timer + first TX are armed; prevents double-start. */
extern bool timer_started;

// --- Heartbeat bookkeeping (TIMER only) ------------------------------------
extern uint8_t transmission_count;
extern bool first_boot;
/** Config values as last reported to the server (see send_policy.h). */
extern ReportedConfig g_last_reported;

// --- Battery ---------------------------------------------------------------
/** Low-battery shutdown latch; clears when the supply recovers (see battery.cpp). */
extern bool battery_shutdown_latched;

// --- Switch input (RAK19003 WB_IO1) ----------------------------------------
extern bool gpioInterruptEnabled;	  // from SWITCH_ENABLED in config.h
extern volatile bool switch_state;	  // written ISR/TIMER
extern uint8_t previous_switch_state; // TIMER

// --- Accelerometer (RAK1904) -----------------------------------------------
extern volatile bool motion_detected;	 // written ISR/TIMER, read TIMER
extern volatile time_t last_trigger;	 // not used for decisions at the moment
extern bool acc_config_pending;			 // set by LORA (downlink), applied by TIMER in sensor_handler
extern uint8_t interrupt_threshold;		 // INT1 threshold, 1/32 of full scale for +-2g (1-127)
extern uint8_t acc_frequency;			 // ODR index: 1=1Hz, 2=10Hz, 3=25Hz, 4=50Hz, 5=100Hz, 6=200Hz, 7=400Hz
extern uint8_t interrupt_duration;		 // duration = value / ODR (0-127)
