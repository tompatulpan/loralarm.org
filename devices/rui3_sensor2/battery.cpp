/**
 * @file battery.cpp
 * @brief Low-battery supervision (see battery.h, battery_policy.h).
 */
#include "app.h"
#include "battery_policy.h"

void battery_boot_guard(void)
{
	// Check the supply before the AT window, LoRa stack configuration, join TX,
	// or any flash access. At low voltage those operations can brown out and
	// leave persistent RUI3/application state corrupt.
	float boot_voltage = platform_battery_get();
	MYLOG("SETUP", "Boot battery: %.2fV", boot_voltage);

	// custom_parameters is not loaded from flash yet — use the platform default threshold.
	const uint16_t default_mv = platform_batt_critical_default_mv();
	if (!battery_policy::is_critical(boot_voltage, default_mv))
	{
		return;
	}

	const float boot_resume_v = battery_policy::resume_v(default_mv, BATT_RESUME_MARGIN_V);
	MYLOG("SETUP", "Battery too low at boot (%.2fV); sleeping until supply recovers above %.2fV",
		  boot_voltage, boot_resume_v);
	Serial.flush();
	// Timed deep sleep: at this point no GPIO interrupts, RUI3 timers, or
	// LoRa events exist yet, so an untimed sleep could never wake (e.g. the
	// device is simply plugged into a charger). The timeout guarantees a
	// re-check every BOOT_GUARD_RECHECK_MS; external events still wake earlier.
	while (1)
	{
		api.system.sleep.all(BOOT_GUARD_RECHECK_MS);
		float v = platform_battery_get();
		if (v > boot_resume_v)
		{
			MYLOG("SETUP", "Battery recovered (%.2fV) — continuing startup", v);
			break;
		}
	}
}

bool battery_recovered(float voltage)
{
	return battery_policy::recovered(voltage, custom_parameters.batt_critical_mv, BATT_RESUME_MARGIN_V);
}

/**
 * @return true when first_reading and BATT_LATCH_CONFIRM_READS-1 further readings
 *         are all at or below the critical threshold. A single outlier right
 *         after wake-up returns false, so it can never latch the device.
 */
bool battery_confirmed_critical(float first_reading)
{
	const uint16_t crit_mv = custom_parameters.batt_critical_mv;
	if (!battery_policy::is_critical(first_reading, crit_mv))
	{
		return false;
	}
	for (uint8_t i = 1; i < BATT_LATCH_CONFIRM_READS; i++)
	{
		delay(BATT_LATCH_CONFIRM_GAP_MS);
		float v = platform_battery_get();
		if (!battery_policy::is_critical(v, crit_mv))
		{
			MYLOG("POWER", "Low reading %.2fV not confirmed (%.2fV) — not latching", first_reading, v);
			return false;
		}
	}
	return true;
}

/**
 * Latches a flag instead of parking in a never-returning untimed sleep loop:
 * RUI3 callback dispatch stays alive, motion events (RAK_TIMER_1) keep
 * sending, and the latch clears — without a power cycle — as soon as the
 * supply recovers above critical + BATT_RESUME_MARGIN_V. Recovery is
 * re-checked on every wake in loop() and before every heartbeat in
 * sensor_handler().
 *
 * RUI3/RAK3172 has no hardware power-off; the latch suppresses radio TX but
 * is not a substitute for a hardware undervoltage disconnect.
 */
void enterLowBatteryShutdown(float voltage)
{
	// Log only on the latch transition — this is called on every tick while
	// the outage persists, and the recovery path logs its own line.
	if (!battery_shutdown_latched)
	{
		const uint16_t crit_mv = custom_parameters.batt_critical_mv;
		MYLOG("POWER", "Battery critical (%.2fV <= %.2fV); radio TX suppressed", voltage, battery_policy::critical_v(crit_mv));
		MYLOG("POWER", "Latching low-battery shutdown until supply recovers above %.2fV",
			  battery_policy::resume_v(crit_mv, BATT_RESUME_MARGIN_V));
		Serial.flush();
	}
	battery_shutdown_latched = true;
}

bool battery_supervise(float voltage)
{
	// Clear the latch once the supply recovers above the resume threshold
	// (hysteresis) — this tick continues as a normal heartbeat, so the server
	// sees the recovered device without waiting for the next wake.
	if (battery_shutdown_latched && battery_recovered(voltage))
	{
		battery_shutdown_latched = false;
		MYLOG("POWER", "Supply recovered (%.2fV); telemetry resumed", voltage);
	}

	// Stay quiet while latched until the supply is back above the resume threshold
	// (hysteresis), not merely above the critical threshold.
	if (battery_shutdown_latched || battery_confirmed_critical(voltage))
	{
		enterLowBatteryShutdown(voltage);
		return true;
	}
	return false;
}

void battery_loop_check(void)
{
	if (!battery_shutdown_latched)
	{
		return;
	}
	float v = platform_battery_get();
	if (battery_recovered(v))
	{
		battery_shutdown_latched = false;
		MYLOG("POWER", "Supply recovered (%.2fV) — resuming telemetry", v);
		// Immediate heartbeat so the server sees the device without
		// waiting for the next periodic tick.
		sensor_handler(NULL);
	}
}
