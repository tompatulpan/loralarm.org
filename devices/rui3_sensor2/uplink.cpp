/**
 * @file uplink.cpp
 * @brief Heartbeat assembly (sensor_handler) and packet transmission.
 */
#include "app.h"

// ---------------------------------------------------------------------------
// Heartbeat building blocks
// ---------------------------------------------------------------------------

/** Apply ACC config received via downlink — deferred here from the LoRa callback to avoid I2C there. */
static void apply_pending_acc_config(void)
{
	if (!sensor_present(SENSOR_RAK1904) || !acc_config_pending)
	{
		return;
	}
	acc_config_pending = false;
	setup_rak1904();
	// Persist updated acc config so it survives a reboot
	custom_parameters.interrupt_threshold = interrupt_threshold;
	custom_parameters.interrupt_duration = interrupt_duration;
	custom_parameters.acc_frequency = acc_frequency;
	save_at_setting();
	MYLOG("UPLINK", "ACC config saved: threshold=%d duration=%d freq_idx=%d",
		  interrupt_threshold, interrupt_duration, acc_frequency);
}

static ReportedConfig current_reported_config(void)
{
	return {version_byte, interrupt_threshold, interrupt_duration, acc_frequency};
}

void append_supply_state(void)
{
	float battery_reading = platform_battery_get();
	if (battery_reading > 0.0f)
	{
		g_solution_data.addVoltage(LPP_CHANNEL_BATT_1, battery_reading);
	}
	g_solution_data.addDigitalInput(LPP_CHANNEL_DIG_7, battery_shutdown_latched ? 1 : 0);
}

/** Battery voltage, firmware version, ACC config and latch state (ch 1, 7..10, 13). */
static void append_battery_and_config(float battery_reading)
{
	if (battery_reading > 0.0f)
	{
		g_solution_data.addVoltage(LPP_CHANNEL_BATT_1, battery_reading);
	}
	// Report as DigitalInput (read-only sensor type) so the server/dashboard does
	// NOT treat these as actuator states and auto-resend them as downlinks.
	g_solution_data.addDigitalInput(LPP_CHANNEL_DIG_1, version_byte);
	g_solution_data.addDigitalInput(LPP_CHANNEL_DIG_2, interrupt_threshold);
	g_solution_data.addDigitalInput(LPP_CHANNEL_DIG_3, interrupt_duration);
	g_solution_data.addDigitalInput(LPP_CHANNEL_DIG_4, acc_frequency);
	g_solution_data.addDigitalInput(LPP_CHANNEL_DIG_7, battery_shutdown_latched ? 1 : 0);

	g_last_reported = current_reported_config();
}

/** Switch state (ch 11) and the deferred motion flag (ch 12). */
static void append_switch_and_motion(void)
{
	// Always read and report switch state in every heartbeat so the server stays current.
	// Also catches any missed FALLING-edge wakeups in LPM.
	if (gpioInterruptEnabled)
	{
		switch_state = (bool)digitalRead(SW_INT_PIN);
		g_solution_data.addDigitalOutput(LPP_CHANNEL_DIG_5, switch_state ? 0x01 : 0x00);
		previous_switch_state = switch_state;
	}

	// Fallback: a motion event still flagged here was never picked up by send_acc_alert()
	// (it clears the flag itself right before it transmits). A failed immediate send is NOT
	// retried this way: while unjoined heartbeats are skipped, and a TX failure is lost.
	if (motion_detected)
	{
		g_solution_data.addDigitalOutput(LPP_CHANNEL_DIG_6, 0x01);
		motion_detected = false;
		MYLOG("UPLINK", "Motion alert included in periodic payload (deferred from missed immediate TX)");
	}
}

// ---------------------------------------------------------------------------
// Payload budget
// ---------------------------------------------------------------------------

// The wire size of every channel payload_budget.h accounts for must match the Cayenne
// library, or the host-tested budget arithmetic silently drifts from the real frames.
static_assert(payload_budget::CH_DIGITAL == LPP_DIGITAL_INPUT_SIZE + 2 &&
				  payload_budget::CH_DIGITAL == LPP_DIGITAL_OUTPUT_SIZE + 2,
			  "payload_budget::CH_DIGITAL out of sync with CayenneLPP");
static_assert(payload_budget::CH_VOLTAGE == LPP_VOLTAGE_SIZE + 2, "payload_budget::CH_VOLTAGE out of sync");
static_assert(payload_budget::CH_TEMPERATURE == LPP_TEMPERATURE_SIZE + 2, "payload_budget::CH_TEMPERATURE out of sync");
static_assert(payload_budget::CH_HUMIDITY == LPP_RELATIVE_HUMIDITY_SIZE + 2, "payload_budget::CH_HUMIDITY out of sync");
static_assert(payload_budget::CH_PRESSURE == LPP_BAROMETRIC_PRESSURE_SIZE + 2, "payload_budget::CH_PRESSURE out of sync");
static_assert(payload_budget::CH_LUMINOSITY == LPP_LUMINOSITY_SIZE + 2, "payload_budget::CH_LUMINOSITY out of sync");
static_assert(payload_budget::CH_ACCELEROMETER == LPP_ACCELEROMETER_SIZE + 2, "payload_budget::CH_ACCELEROMETER out of sync");
static_assert(payload_budget::CH_VOC == LPP_VOC_SIZE + 2, "payload_budget::CH_VOC out of sync");

void uplink_arm_frame_budget(void)
{
	// CayenneLPP::reset() keeps the sticky error flag: clear what an earlier frame left
	// behind so the post-build overflow check only sees this frame.
	(void)g_solution_data.getError();

	// LoRaWAN: the FRMPayload budget follows the frame's DR (payload_budget.h). The
	// Cayenne layer then refuses adds beyond it (LPP_ERROR_OVERFLOW, checked after
	// the build), so a low-DR frame loses its least-important channels, not the
	// whole heartbeat. P2P has no frame header: buffer size is the only limit.
	uint8_t budget = MAX_PAYLOAD_SIZE;
	if (api.lorawan.nwm.get() == 1)
	{
		// send_packet() restores the configured link policy (DR) right before the
		// stack send; do it first so the budget is armed for the DR actually used.
		if (api.lorawan.njs.get())
		{
			lorawan_enforce_link_policy();
		}
		budget = static_cast<uint8_t>(payload_budget::frmpayload(api.lorawan.dr.get()));
	}
	g_solution_data.setMaxSize(budget);
}

// ---------------------------------------------------------------------------
// Heartbeat
// ---------------------------------------------------------------------------

static void heartbeat_run(void)
{
	MYLOG("POWER", "sensor_handler triggered");
	MYLOG("UPLINK", "Start");

	// This must be the first substantive action: do not write flash, wake
	// sensors, or key the radio while the direct supply is marginal.
	float battery_reading = platform_battery_get();
	if (battery_supervise(battery_reading))
	{
		return;
	}

	// A zero interval disables telemetry, not the protection timer.
	if (custom_parameters.send_interval == 0)
	{
		return;
	}

	if (api.lorawan.nwm.get() == 1 && !api.lorawan.njs.get())
	{
		MYLOG("UPLINK", "Not joined, skip sending");
		return;
	}

	apply_pending_acc_config();

	sensors_wakeup();

#if MY_DEBUG == 1
	digitalWrite(LED_BLUE, HIGH);
#endif

	MYLOG("UPLINK", "Read Sensors");
	g_solution_data.reset();
	// Budget first, channels after: supply/config block, motion, switch, then the
	// sensors in read-priority order (sensors.cpp read_order). If the armed budget
	// (43 usable FRMPayload bytes even at DR0-DR2) is exhausted, the Cayenne layer
	// skips further adds — the tail (pressure/light/gas) is dropped, never the
	// alarm-relevant channels or the heartbeat itself. See payload_budget.h.
	uplink_arm_frame_budget();

	// Increment the transmission counter
	transmission_count++;

	// Send battery data and digital outputs every BATTERY_SEND_INTERVAL transmissions or on first boot
	if (send_policy::should_send_battery_and_config(transmission_count, BATTERY_SEND_INTERVAL, first_boot,
													current_reported_config(), g_last_reported))
	{
		append_battery_and_config(battery_reading);

		// Reset counter and first boot flag only if this was a scheduled transmission
		if (transmission_count >= BATTERY_SEND_INTERVAL || first_boot)
		{
			transmission_count = 0;
			first_boot = false;
			MYLOG("UPLINK", "Battery and digital outputs sent: %.2f V", battery_reading);
		}
		else
		{
			MYLOG("UPLINK", "Digital outputs sent due to config change");
		}
	}

	append_switch_and_motion();

	sensors_read_all();
	sensors_shutdown();

	// Something did not fit the armed budget (only possible at a low DR with a
	// fully populated board): report what the frame carries so the loss is visible.
	if (g_solution_data.getError() == LPP_ERROR_OVERFLOW)
	{
		MYLOG("UPLINK", "Payload trimmed to %d bytes", g_solution_data.getSize());
	}

	send_packet();
	digitalWrite(LED_BLUE, LOW);
}

void sensor_handler(void *unused)
{
	// The heartbeat owns g_solution_data and the I2C bus from here to the send. Callers come
	// from different contexts (RAK_TIMER_0, loop() after a battery recovery, the join
	// callback), so a second one arriving mid-run is dropped instead of running in parallel.
	// Alerts and switch events check the same flag and retry. The check-then-set is not
	// atomic: it narrows the race, it does not close it completely.
	if (heartbeat_busy)
	{
		MYLOG("UPLINK", "Heartbeat already running, skipping");
		return;
	}
	heartbeat_busy = true;
	heartbeat_run();
	heartbeat_busy = false;
}

// ---------------------------------------------------------------------------
// Timer cadence
// ---------------------------------------------------------------------------

void uplink_timer_setup(void)
{
	MYLOG("SETUP", "Create timer for interrupt handler");
	api.system.timer.create(RAK_TIMER_0, sensor_handler, RAK_TIMER_PERIODIC);

	// Keep the voltage guard alive even while OTAA is retrying or periodic TX is
	// disabled. On join, uplink_start_after_join() replaces this with the configured interval.
	if (custom_parameters.send_interval == 0 ||
		(api.lorawan.nwm.get() == 1 && !api.lorawan.njs.get()))
	{
		api.system.timer.start(RAK_TIMER_0, BATTERY_CHECK_INTERVAL_MS, NULL);
	}
	else if (api.lorawan.nwm.get() == 0)
	{
		api.system.timer.start(RAK_TIMER_0, custom_parameters.send_interval, NULL);
		digitalWrite(LED_BLUE, LOW);
	}
}

void uplink_start_after_join(void)
{
	if (custom_parameters.send_interval != 0)
	{
		api.system.timer.start(RAK_TIMER_0, custom_parameters.send_interval, NULL);
		MYLOG("UPLINK", "Send timer started: %lu ms", custom_parameters.send_interval);
	}
	timer_started = true;
	MYLOG("UPLINK", "Triggering first transmission after join");
	sensor_handler(NULL);
}

// ---------------------------------------------------------------------------
// Transmission
// ---------------------------------------------------------------------------

bool send_packet(void)
{
	if (g_solution_data.getSize() == 0)
	{
		MYLOG("UPLINK", "No data to send");
		return false;
	}

	MYLOG("UPLINK", "Payload buffer (size: %d):", g_solution_data.getSize());
#if MY_DEBUG == 1
	for (int i = 0; i < g_solution_data.getSize(); i++)
	{
		Serial.printf("%02X ", g_solution_data.getBuffer()[i]);
	}
	Serial.println();
#endif

	// Check if it is LoRaWAN
	if (api.lorawan.nwm.get() == 1)
	{
		if (!api.lorawan.njs.get())
		{
			MYLOG("UPLINK", "Not joined to network, cannot send");
			return false;
		}

		lorawan_enforce_link_policy();
		const bool link_check = lorawan_take_link_check();
		const bool confirm = g_confirmed_mode || link_check;
		const uint8_t retry = link_check ? LINK_CHECK_RETRIES : g_confirmed_retry;
		MYLOG("UPLINK", "Sending packet over LoRaWAN (DR=%d TXP=%d ADR=%s%s)",
			  api.lorawan.dr.get(), api.lorawan.txp.get(), api.lorawan.adr.get() ? "on" : "off",
			  link_check ? ", confirmed link check" : "");

		// Note: RUI3 API may return false even when transmission succeeds
		if (api.lorawan.send(g_solution_data.getSize(), g_solution_data.getBuffer(), set_fPort, confirm, retry))
		{
			MYLOG("UPLINK", "Packet enqueued successfully, size %d", g_solution_data.getSize());
			return true;
		}
		MYLOG("UPLINK", "Enqueue returned false (check TX callback for actual result)");
		if (link_check)
		{
			lorawan_link_check_send_failed();
		}
		return false;
	}

	// It is P2P
	MYLOG("UPLINK", "Send packet with size %d over P2P", g_solution_data.getSize());
	if (api.lora.psend(g_solution_data.getSize(), g_solution_data.getBuffer(), true))
	{
		MYLOG("UPLINK", "Packet enqueued");
		return true;
	}

	MYLOG("UPLINK", "Send failed");
	// If it was motion triggered, reset the ACC interrupts
	if (sensor_present(SENSOR_RAK1904))
	{
		clear_int_rak1904();
	}
	return false;
}
