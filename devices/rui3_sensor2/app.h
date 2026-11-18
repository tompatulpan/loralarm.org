/**
 * @file app.h
 * @brief Umbrella header: every module includes this one file.
 *
 * Original file by Bernd Giesecke (RAKwireless RUI3 sensor application header),
 * restructured into modules. See README.md for the module map.
 */
#pragma once

#include <Arduino.h>
#include <CayenneLPP.h>

#include "config.h"
#include "wisblock_cayenne.h"
#include "platform.h"
#include "state.h"
#include "sensors.h"
#include "battery.h"
#include "uplink.h"
#include "lorawan.h"

// Persistent settings and custom AT commands (custom_at.cpp)
bool init_status_at(void);
bool init_interval_at(void);
bool init_min_interval_at(void);
bool init_batt_crit_at(void);
bool init_batt_mode_at(void);
bool get_at_setting(void);
bool save_at_setting(void);
/** Register all custom AT commands (STATUS, SENDINT, MININT, BATCRIT, BATMODE). */
void at_register_all(void);

// Circuit switch (switch_gpio.cpp)
bool init_gpio(void);
void switch_int_handler(void);
void switch_bounce_check(void *unused);
void handle_gpio(void *unused);
