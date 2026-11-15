/**
 * @file battery.h
 * @brief Low-battery supervision: boot guard, confirmed-critical check, shutdown latch.
 *
 * Decision rules are in battery_policy.h (pure, host-tested); this module adds
 * the hardware side (ADC reads, sleeping) and owns battery_shutdown_latched.
 */
#pragma once

#include <Arduino.h>

/**
 * Call right after platform_init(), before any flash/radio use. If the supply
 * is critical at boot, sleeps (timed) until it recovers. Uses the platform
 * default threshold because flash settings are not loaded yet.
 */
void battery_boot_guard(void);

/** True only if @p first_reading is confirmed by fresh follow-up readings. */
bool battery_confirmed_critical(float first_reading);

/** True once the supply is above critical + BATT_RESUME_MARGIN_V. */
bool battery_recovered(float voltage);

/** Latch the shutdown state (radio TX suppressed by sensor_handler). */
void enterLowBatteryShutdown(float voltage);

/**
 * Per-tick supervision for sensor_handler(): clears the latch on recovery and
 * re-latches on a confirmed critical reading.
 * @return true if the caller must stay quiet (latched) and skip this heartbeat.
 */
bool battery_supervise(float voltage);

/**
 * Call from loop() on every wake: while latched, re-check the supply and, on
 * recovery, clear the latch and send an immediate heartbeat.
 */
void battery_loop_check(void);
