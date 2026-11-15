/**
 * @file uplink.h
 * @brief Heartbeat (sensor_handler) and packet transmission.
 */
#pragma once

#include <Arduino.h>

/**
 * RAK_TIMER_0 callback, every custom_parameters.send_interval ms (default set
 * with ATC+SENDINT). Supervises the battery, reads all sensors and sends the
 * heartbeat. Also runs once right after a join.
 */
void sensor_handler(void *unused);

/**
 * Append battery voltage (ch 1) and the low-battery latch flag (ch 13) to g_solution_data.
 * Every frame that is not a heartbeat (motion alert, switch event) carries this, so the
 * server never sees a latched node as healthy.
 */
void append_supply_state(void);

/** Send whatever is in g_solution_data over LoRaWAN or P2P. */
bool send_packet(void);

/** Create RAK_TIMER_0 and start the boot-time cadence (battery guard / P2P heartbeat). */
void uplink_timer_setup(void);

/** Join succeeded (or was already cached): start the send timer and send the first heartbeat. */
void uplink_start_after_join(void);
