/**
 * @file state.cpp
 * @brief Definitions for everything declared in state.h.
 */
#include "app.h"

custom_param_s custom_parameters;

WisCayenne g_solution_data(255);
volatile bool heartbeat_busy = false;

uint8_t version_byte = FW_VERSION_BYTE;
bool g_confirmed_mode = false;
uint8_t g_confirmed_retry = 0;
uint8_t g_data_rate = 3;
uint8_t set_fPort = 2;

bool setup_complete = false;
bool timer_started = false;

uint8_t transmission_count = 0;
bool first_boot = true;
ReportedConfig g_last_reported = {0, 0, 0, 0};

bool battery_shutdown_latched = false;

// POWER NOTE: Enabling the switch feature adds ~75uA in sleep when the switch is closed (GND),
// because INPUT_PULLUP uses the internal ~35kohm pull-up (3.3V/35k ≈ 94uA).
// To keep sleep current near ~25uA regardless of switch state, add an external 1Mohm
// pull-up resistor between VCC and WB_IO1, then change INPUT_PULLUP to INPUT in init_gpio().
bool gpioInterruptEnabled = false;
volatile bool switch_state = false;
uint8_t previous_switch_state = 0;

volatile bool motion_detected = false;
volatile time_t last_trigger = 0;
bool acc_config_pending = false;
uint8_t interrupt_threshold = 0x08;
uint8_t acc_frequency = 2;
uint8_t interrupt_duration = 0x01;
