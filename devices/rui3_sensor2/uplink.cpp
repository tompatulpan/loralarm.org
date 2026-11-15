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

	// Fallback: include motion alert if a previous immediate send failed (radio busy, not joined, etc.)
	if (motion_detected)
	{
		g_solution_data.addDigitalOutput(LPP_CHANNEL_DIG_6, 0x01);
		motion_detected = false;
		MYLOG("UPLINK", "Motion alert included in periodic payload (deferred from missed immediate TX)");
	}
}

// ---------------------------------------------------------------------------
// Heartbeat
// ---------------------------------------------------------------------------

void sensor_handler(void *unused)
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

	// From here to the send, the heartbeat owns g_solution_data and the I2C bus
	heartbeat_busy = true;

	sensors_wakeup();

#if MY_DEBUG == 1
	digitalWrite(LED_BLUE, HIGH);
#endif

	MYLOG("UPLINK", "Read Sensors");
	g_solution_data.reset();
	sensors_read_all();
	sensors_shutdown();

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

	// Validate payload size before sending
	if (g_solution_data.getSize() > MAX_PAYLOAD_SIZE)
	{
		MYLOG("UPLINK", "Payload too large: %d bytes", g_solution_data.getSize());
		heartbeat_busy = false;
		return;
	}

	send_packet();
	heartbeat_busy = false;
	digitalWrite(LED_BLUE, LOW);
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

		MYLOG("UPLINK", "Sending packet over LoRaWAN");

		// Note: RUI3 API may return false even when transmission succeeds
		if (api.lorawan.send(g_solution_data.getSize(), g_solution_data.getBuffer(), set_fPort, g_confirmed_mode, g_confirmed_retry))
		{
			MYLOG("UPLINK", "Packet enqueued successfully, size %d", g_solution_data.getSize());
			return true;
		}
		MYLOG("UPLINK", "Enqueue returned false (check TX callback for actual result)");
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
