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

/**
 * Call before every LoRaWAN uplink: if the ADR flag, TX power or DR no longer match the
 * POST_JOIN_* policy (e.g. the network sent a LinkADRReq), log it and restore them.
 */
void lorawan_enforce_link_policy(void);

/** Log the confirmed/retry TX settings (LoRaWAN only). */
void lorawan_log_tx_settings(void);

/**
 * Link check (see LINK_CHECK_* in config.h). send_packet() asks whether this uplink must be
 * confirmed; true means "send confirmed with LINK_CHECK_RETRIES retries" and arms the result
 * handling in the send callback. If the stack then refuses the send, call
 * lorawan_link_check_send_failed() so the check is tried again with the next uplink.
 */
bool lorawan_take_link_check(void);
void lorawan_link_check_send_failed(void);

/** Call from loop() on every wake: starts the fresh join after a failed link check. */
void lorawan_loop_check(void);
