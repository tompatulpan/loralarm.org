/**
 * @file lorawan.h
 * @brief LoRaWAN / P2P stack configuration and callbacks.
 */
#pragma once

#include <Arduino.h>

/**
 * Configure the radio stack: register callbacks, enforce OTAA/region/class,
 * verify credentials and start the join (LoRaWAN), or register the P2P
 * callbacks. Call once from setup() after the AT window.
 */
void lorawan_setup(void);

/**
 * Call at the very end of setup(). If the join already completed while setup()
 * was running (cached session), joinCallback deferred its work; do it now.
 */
void lorawan_resume_cached_join(void);

/** Log the confirmed/retry TX settings (LoRaWAN only). */
void lorawan_log_tx_settings(void);
