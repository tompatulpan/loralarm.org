// LoRaLarm RUI3 sensor firmware (modular) for RAK3172 and RAK4631-R.
// Based on https://github.com/RAKWireless/RUI3-Best-Practice, by Bernd Giesecke (bernd.giesecke@rakwireless.com)
// Device goes into sleep after successful OTAA/ABP network join.
// Tested hardware: RAK3172, RAK4631, RAK1901, RAK1902, RAK1903, RAK1904, RAK1906, RAK19003, RAK19007.
// See README.md for the module map and CHANGELOG.md for version history.
//
// This file only sequences startup; each concern lives in its own module:
//   battery.*   low-battery guard and latch      lorawan.*  join, callbacks, downlinks
//   sensors.*   sensor registry and drivers      uplink.*   heartbeat and TX
//   state.*     shared runtime state             custom_at.* settings and AT commands

#include "app.h"

/**
 * @brief Arduino setup, called once after reboot/power-up
 */
void setup()
{
	// Start Serial first — must be before any MYLOG or LoRaWAN calls so
	// debug output is not garbled and join events during setup are visible.
	Serial.begin(115200);

	// Platform init: Serial6 (nRF52 only), analogReadResolution, BAT_ADC_PIN,
	// WB_IO2 sensor power rail. Must run before any MYLOG call.
	platform_init();

	// No flash, radio or AT window while the supply is critical
	battery_boot_guard();

	// 5-second window to send AT+BOOT before the stack initialises
	delay(5000);

	lorawan_setup();

	pinMode(LED_GREEN, OUTPUT);
	digitalWrite(LED_GREEN, HIGH);
	pinMode(LED_BLUE, OUTPUT);
	digitalWrite(LED_BLUE, HIGH);

	api.system.firmwareVersion.set("RUI3-Kit1-V1.0.1");

	Serial.println("LoraLarm.org RAKwireless RUI3 Node");
	Serial.println("------------------------------------------------------");
	Serial.println("Setup the device with WisToolBox or AT commands before using it");
	Serial.printf("Version %s\n", api.system.firmwareVersion.get().c_str());
	Serial.println("------------------------------------------------------");

	// Initialize module
	Wire.begin();

	// Add I2C device scanning for diagnostics (debug builds only)
#if MY_DEBUG > 0
	sensors_scan_i2c();
#endif

	at_register_all();

	// Get saved settings from flash
	get_at_setting();

	digitalWrite(LED_GREEN, LOW);

	if (gpioInterruptEnabled)
	{
		MYLOG("SETUP", "Initializing GPIO interrupt system");
		if (init_gpio())
		{
			MYLOG("SETUP", "GPIO interrupt system initialized successfully");
		}
		else
		{
			MYLOG("SETUP", "Failed to initialize GPIO interrupt system");
		}
	}

	uplink_timer_setup();
	lorawan_log_tx_settings();

	// Enable low power mode
	api.system.lpm.set(1);

	// Start BLE advertising (nRF52840: required for SoftDevice init; no-op on STM32WLE5).
	// See platform_nrf52.cpp for power implications and the advertise.stop() caveat.
	platform_ble_start();

	sensors_init_all();

	// Set last trigger time to boot time
	last_trigger = millis();

	digitalWrite(LED_BLUE, LOW);

	// Signal that sensor init is complete. joinCallback checks this before firing sensor_handler
	// to avoid TX/RX events overlapping with sensor initialization.
	setup_complete = true;
	MYLOG("SETUP", "Setup complete");

	lorawan_resume_cached_join();
}

/**
 * @brief The firmware is timer driven; loop() only sleeps.
 *
 * While a low-battery shutdown is latched it re-checks the supply on every wake
 * (RAK1904 INT1 motion, RUI3 timers) and resumes telemetry — with an immediate
 * heartbeat — once the supply recovers above the resume threshold.
 */
void loop()
{
	battery_loop_check();
	lorawan_loop_check();

	// Disable ADC/peripherals before sleep (no-op on STM32WLE5).
	// See platform_pre_sleep() in platform_nrf52.cpp for details.
	platform_pre_sleep();

	api.system.sleep.all();
}
