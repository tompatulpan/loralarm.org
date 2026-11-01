// Based on https://github.com/RAKWireless/RUI3-Best-Practice, by Bernd Giesecke (bernd.giesecke@rakwireless.com)
// Device goes into sleep after successful OTAA/ABP network join.
// Tested hardware: RAK3172, RAK4631, RAK1901, RAK1902, RAK1903, RAK1904, RAK1906, RAK19003, RAK19007.
// See CHANGELOG.md for version history.
 
#include "app.h"

// Constants to replace magic numbers
const uint8_t BATTERY_SAMPLES = 3;
const uint16_t SENSOR_WARMUP_TIME = 200;  // 200ms: OPT3001 needs 100ms conversion + margin
const uint8_t MAX_PAYLOAD_SIZE = 255;
const uint8_t ASCII_TO_NUM_OFFSET = '0';
const uint8_t DOWNLINK_MAX_VALUE = 255;

// float previous_battery_reading = 0.0;
uint8_t previous_version_byte = 0;
uint8_t previous_interrupt_threshold = 0;
uint8_t previous_interrupt_duration = 0;
uint8_t previous_acc_frequency = 0;
uint8_t previous_switch_state = 0;

// POWER NOTE: Enabling the switch feature adds ~75uA in sleep when the switch is closed (GND),
// because INPUT_PULLUP uses the internal ~35kohm pull-up (3.3V/35k ≈ 94uA).
// To keep sleep current near ~25uA regardless of switch state, add an external 1Mohm
// pull-up resistor between VCC and WB_IO1, then change INPUT_PULLUP to INPUT in init_gpio().
bool gpioInterruptEnabled = false; // set true if switch sensor is going to be used
bool acc_config_pending = false;  // Set in receiveCallback, applied safely in sensor_handler
bool setup_complete = false;      // Set true at end of setup(); guards joinCallback TX
bool timer_started = false;       // Set true when timer+first TX have been armed; prevents double-start

/** version byte */
uint8_t version_byte = 30;

/** Packet is confirmed/unconfirmed (Set with AT commands) */
bool g_confirmed_mode = false;
/** If confirmed packet, number or retries (Set with AT commands) */
uint8_t g_confirmed_retry = 0;
/** Data rate  (Set with AT commands) */
uint8_t g_data_rate = 3;

/** fPort to send packages */
uint8_t set_fPort = 2;

/** Payload buffer */
WisCayenne g_solution_data(255);

/** Flag if RAK1901/Temperature and Humidity Sensor was found */
bool has_rak1901 = false;

/** Flag if RAK1902/Barometric Pressure Sensor was found */
bool has_rak1902 = false;

/** Flag if RAK1903/Light Sensor was found */
bool has_rak1903 = false;

/** Flag if RAK1904/Acceleration Sensor was found */
bool has_rak1904 = false;

/** Flag if RAK1906/Environmental Sensor was found */
bool has_rak1906 = false;

/** Variables for battery reporting */
// unsigned long last_battery_report = 0;
// const unsigned long BATTERY_REPORT_INTERVAL = 3600000; // 1 hour in milliseconds

/** Transmission counter */
uint8_t transmission_count = 0;

bool first_boot = true;

/** Battery send interval */
const uint8_t BATTERY_SEND_INTERVAL = 1;  // Send battery status and digital outputs every hour (if TX interval is 5 min)

/**
 * @brief Callback after join request cycle
 *
 * @param status Join result
 */
void joinCallback(int32_t status)
{
    if (status != 0)
    {
        MYLOG("JOIN-CB", "LoRaWan OTAA - join fail! Next retry in %d s (auto-rejoin active)\r\n", JOIN_RETRY_SEC);
    }
    else
    {
        MYLOG("JOIN-CB", "LoRaWan OTAA - joined!\r\n");

        // Enable ADR — network will now manage DR and TX power adaptively
        api.lorawan.adr.set(true);
        // Step down from join TX power to POST_JOIN_TXP. ADR will adjust from this
        // baseline — it can go up or down. Staying at TXP=0 saturates a nearby
        // gateway LNA → wild RSSI swings and COLLISION_PACKET on RX1 downlink.
        api.lorawan.txp.set(POST_JOIN_TXP);
        MYLOG("JOIN-CB", "ADR enabled. TXP set to %d (~%d dBm EIRP). DR=%d",
              POST_JOIN_TXP, 16 - POST_JOIN_TXP * 2, api.lorawan.dr.get());

        digitalWrite(LED_BLUE, LOW);

        // Start normal application send timer now that we are joined.
        // Guard: if setup() is still running (join completed from flash before sensor init
        // finished), skip — setup()'s "already joined" block will start the timer and trigger
        // the first TX. Starting it here AND there would double-start the timer.
        if (setup_complete)
        {
            if (custom_parameters.send_interval != 0)
            {
                api.system.timer.start(RAK_TIMER_0, custom_parameters.send_interval, NULL);
                MYLOG("JOIN-CB", "Send timer started: %lu ms", custom_parameters.send_interval);
            }
            timer_started = true;
            MYLOG("JOIN-CB", "Triggering first transmission after join");
            sensor_handler(NULL);
        }
        else
        {
            MYLOG("JOIN-CB", "Setup not complete — timer and first TX deferred to setup()");
        }
    }
}

/**
 * @brief LoRaWAN callback after packet was received
 *
 * @param data pointer to structure with the received data
 */
void receiveCallback(SERVICE_LORA_RECEIVE_T *data)
{
	MYLOG("RX-CB", "RX, port %d, DR %d, RSSI %d, SNR %d", data->Port, data->RxDatarate, data->Rssi, data->Snr);
	
	// Handle empty payloads (network acknowledgments, etc.)
	if (data->BufferSize == 0) {
        MYLOG("RX-CB", "Empty payload received (network acknowledgment)");
        return;
    }
    
	if (data->BufferSize < 3) {
        MYLOG("RX-CB", "Invalid payload size: %d (minimum 3 bytes required)", data->BufferSize);
        return;
    }
    
    // Validate ASCII digits
    for (int i = 0; i < 3; i++) {
        if (data->Buffer[i] < '0' || data->Buffer[i] > '9') {
            MYLOG("RX-CB", "Invalid ASCII digit at position %d: %02X", i, data->Buffer[i]);
            return;
        }
    }
    
	#if MY_DEBUG == 1
	{
		Serial.printf("[RX-CB] Payload (%d bytes):", data->BufferSize);
		for (int i = 0; i < data->BufferSize; i++)
			Serial.printf(" %02X", data->Buffer[i]);
		Serial.println();
	}
	#endif

	// Combine the received downlink data to a single number
	uint8_t combined_data = (data->Buffer[0] - ASCII_TO_NUM_OFFSET) * 100 + 
	                       (data->Buffer[1] - ASCII_TO_NUM_OFFSET) * 10 + 
	                       (data->Buffer[2] - ASCII_TO_NUM_OFFSET);

	if (combined_data > DOWNLINK_MAX_VALUE) {
        MYLOG("RX-CB", "Value out of range: %d", combined_data);
        return;
    }

	switch (data->Port)
	{
	case 5:		// port5 — threshold (7-bit register, valid 1-127)
		if (combined_data < 1 || combined_data > 127) {
			MYLOG("RX-CB", "threshold %d out of range [1-127], ignored", combined_data);
			return;
		}
		interrupt_threshold = combined_data;
		MYLOG("RX-CB", "Updated interrupt_threshold to %d", combined_data);
		break;
	case 6:		// port6 — ODR index (valid 1-7; 0=power-down rejected)
		if (combined_data < 1 || combined_data > 7) {
			MYLOG("RX-CB", "acc_frequency %d out of range [1-7], ignored", combined_data);
			return;
		}
		acc_frequency = combined_data;
		MYLOG("RX-CB", "Updated acc_frequency to %d", combined_data);
		break;
	case 7:		// port7 — duration (7-bit register, valid 0-127)
		if (combined_data > 127) {
			MYLOG("RX-CB", "duration %d out of range [0-127], ignored", combined_data);
			return;
		}
		interrupt_duration = combined_data;
		MYLOG("RX-CB", "Updated interrupt_duration to %d", combined_data);
		break;
	default:
		MYLOG("RX-CB", "Unknown port: %d", data->Port);
		break;
	}
	// Defer setup_rak1904() to sensor_handler — never call I2C from inside the LoRa stack callback.
	// Do NOT guard on has_rak1904 here: a downlink can arrive during init_rak1904() (before
	// has_rak1904 is set). sensor_handler already guards on has_rak1904 before applying.
	if (data->Port >= 5 && data->Port <= 7)
	{
		acc_config_pending = true;
	}
}

/**
 * @brief Callback for LinkCheck result
 *
 * @param data pointer to structure with the linkcheck result
 */
void linkcheckCallback(SERVICE_LORA_LINKCHECK_T *data)
{
	MYLOG("LC_CB", "%s Margin %d GW# %d RSSI%d SNR %d", data->State == 0 ? "Success" : "Failed",
		  data->DemodMargin, data->NbGateways,
		  data->Rssi, data->Snr);
}

/**
 * @brief Sleep indefinitely when the supply reaches the critical threshold.
 * Called before any further flash, sensor, or radio activity.
 *
 * RUI3/RAK3172 has no hardware power-off; untimed deep sleep minimises current
 * but is not a substitute for a hardware undervoltage disconnect.
 */
void enterLowBatteryShutdown(float voltage) {
	MYLOG("POWER", "Battery critical (%.2fV <= %.2fV); radio TX suppressed", voltage, custom_parameters.batt_critical_mv / 1000.0f);

	MYLOG("POWER", "Entering indefinite deep sleep to protect battery");
	Serial.flush();
	while (1) {
		api.system.sleep.all(); // untimed — no LPTIM wakeup needed, minimises sleep current
	}
}

/**
 * @brief Check if battery and config data should be sent
 *
 * @return bool True if should send
 */
bool shouldSendBatteryAndConfig() {
    return (transmission_count >= BATTERY_SEND_INTERVAL || 
            first_boot ||
            version_byte != previous_version_byte ||
            interrupt_threshold != previous_interrupt_threshold ||
            interrupt_duration != previous_interrupt_duration ||
            acc_frequency != previous_acc_frequency);
}

/**
 * @brief Check if switch state should be sent
 *
 * @return bool True if should send
 */
bool shouldSendSwitchState() {
    return switch_state != previous_switch_state;
}

/**
 * @brief Manage sensor startup and shutdown
 *
 * @param wakeup True to wake up sensors, false to shut down
 */
void manageSensors(bool wakeup) {
    if (wakeup) {
        MYLOG("SENSORS", "Waking up active sensors...");
        if (has_rak1901) {
            MYLOG("SENSORS", "Starting RAK1901 (Temp/Humidity)");
            startup_rak1901();
        }
        if (has_rak1902) {
            MYLOG("SENSORS", "Starting RAK1902 (Pressure)");
            startup_rak1902();
        }
        if (has_rak1903) {
            MYLOG("SENSORS", "Starting RAK1903 (Light)");
            startup_rak1903();
        }
        if (has_rak1904) {
            MYLOG("SENSORS", "Starting RAK1904 (Accelerometer)");
            startup_rak1904();
            last_trigger = millis();
        }
        if (has_rak1906) {
            MYLOG("SENSORS", "Starting RAK1906 (Environmental)");
            startup_rak1906();
        }
        // Warmup time for active sensors
        MYLOG("SENSORS", "Sensor warmup delay: %d ms", SENSOR_WARMUP_TIME);
        delay(SENSOR_WARMUP_TIME);
        MYLOG("SENSORS", "Active sensors ready");
    } else {
        MYLOG("SENSORS", "Shutting down active sensors...");
        if (has_rak1901) {
            MYLOG("SENSORS", "Shutting down RAK1901 (Temp/Humidity)");
            shutdown_rak1901();
        }
        if (has_rak1902) {
            MYLOG("SENSORS", "Shutting down RAK1902 (Pressure)");
            shutdown_rak1902();
        }
        if (has_rak1903) {
            MYLOG("SENSORS", "Shutting down RAK1903 (Light)");
            shutdown_rak1903();
        }
        if (has_rak1904) {
            MYLOG("SENSORS", "Shutting down RAK1904 (Accelerometer)");
            shutdown_rak1904();
        }
        if (has_rak1906) {
            MYLOG("SENSORS", "Shutting down RAK1906 (Environmental)");
            shutdown_rak1906();
        }
        MYLOG("SENSORS", "Sensor shutdown complete");
    }
}

/**
 * @brief LoRaWAN callback after TX is finished
 *
 * @param status TX status
 */
void sendCallback(int32_t status)
{
	if (status == 0) {
		MYLOG("TX-CB", "Transmission successful - ready for sleep");
	} else {
		MYLOG("TX-CB", "Transmission failed with status %d", status);
	}
	digitalWrite(LED_BLUE, LOW);
	// RUI3 nRF52840 restarts BLE advertising without a timeout after each TX.
	// Re-arm with a 1 s auto-stop to prevent the ~425 uA stuck-advertise state.
	// No-op on STM32WLE5.
	platform_ble_rearm();
}

/**
 * @brief LoRa P2P callback if a packet was received
 *
 * @param data pointer to the data with the received data
 */
void recv_cb(rui_lora_p2p_recv_t data)
{
	MYLOG("RX-P2P-CB", "P2P RX, RSSI %d, SNR %d", data.Rssi, data.Snr);
	for (int i = 0; i < data.BufferSize; i++)
	{
		Serial.printf("%02X", data.Buffer[i]);
	}
	Serial.print("\r\n");
}

/**
 * @brief LoRa P2P callback if a packet was sent
 *
 */
void send_cb(void)
{
	MYLOG("TX-P2P-CB", "P2P TX finished");
	digitalWrite(LED_BLUE, LOW);
}

/**
 * @brief LoRa P2P callback for CAD result
 *
 * @param result true if activity was detected, false if no activity was detected
 */
void cad_cb(bool result)
{
	MYLOG("CAD-P2P-CB", "P2P CAD reports %s", result ? "activity" : "no activity");
}

/**
 * @brief Scan I2C bus for connected devices
 *
 */
void scanI2CDevices() {
    MYLOG("I2C", "Scanning I2C bus for connected sensors...");
    
    uint8_t deviceCount = 0;
    bool rak1901_found = false;
    bool rak1902_found = false;
    bool rak1903_found = false;
    bool rak1904_found = false;
    bool rak1906_found = false;
    
    for (uint8_t address = 1; address < 127; address++) {
        Wire.beginTransmission(address);
        uint8_t error = Wire.endTransmission();
        
        if (error == 0) {
            deviceCount++;
            MYLOG("I2C", "Device found at address 0x%02X", address);
            
            // Identify sensors by address
            switch (address) {
                case 0x70:
                    MYLOG("I2C", "  -> SHTC3 (RAK1901) Temp/Humidity Sensor");
                    rak1901_found = true;
                    break;
                case 0x5C:
                    MYLOG("I2C", "  -> LPS35HW (RAK1902) Pressure Sensor");
                    rak1902_found = true;
                    break;
                case 0x5D:
                    MYLOG("I2C", "  -> LPS22HB (RAK1902) Pressure Sensor");
                    rak1902_found = true;
                    break;
                case 0x44:
                    MYLOG("I2C", "  -> OPT3001 (RAK1903) Light Sensor");
                    rak1903_found = true;
                    break;
                case 0x18:
                    MYLOG("I2C", "  -> LIS3DH (RAK1904) Accelerometer (SDO=GND)");
                    rak1904_found = true;
                    break;
                case 0x19:
                    MYLOG("I2C", "  -> LIS3DH (RAK1904) Accelerometer (SDO=VCC)");
                    rak1904_found = true;
                    break;
                case 0x76:
                    MYLOG("I2C", "  -> BME680 (RAK1906) Environmental Sensor (SDO=GND)");
                    rak1906_found = true;
                    break;
                case 0x77:
                    MYLOG("I2C", "  -> BME680 (RAK1906) Environmental Sensor (SDO=VCC)");
                    rak1906_found = true;
                    break;
                default:
                    MYLOG("I2C", "  -> Unknown device at 0x%02X", address);
                    break;
            }
        }
        else if (error == 4) {
            MYLOG("I2C", "Unknown error at address 0x%02X", address);
        }
    }
    
    MYLOG("I2C", "Detected sensors: RAK1901=%s, RAK1902=%s, RAK1903=%s, RAK1904=%s, RAK1906=%s",
          rak1901_found ? "YES" : "NO", rak1902_found ? "YES" : "NO",
          rak1903_found ? "YES" : "NO", rak1904_found ? "YES" : "NO",
          rak1906_found ? "YES" : "NO");
    
    if (deviceCount == 0) {
        MYLOG("I2C", "No I2C devices found! Check connections.");
    } else {
        MYLOG("I2C", "Scan complete. Found %d device(s)", deviceCount);
    }
}

/**
 * @brief Arduino setup, called once after reboot/power-up
 *
 */
void setup()
{
	// Start Serial first — must be before any MYLOG or LoRaWAN calls so
	// debug output is not garbled and join events during setup are visible.
	Serial.begin(115200);

	// Platform init: Serial6 (nRF52 only), analogReadResolution, BAT_ADC_PIN,
	// WB_IO2 sensor power rail. Must run before any MYLOG call.
	platform_init();

	// Check the supply before the AT window, LoRa stack configuration, join TX,
	// or any flash access. At low voltage those operations can brown out and
	// leave persistent RUI3/application state corrupt.
	{
		float boot_voltage = platform_battery_get();
		MYLOG("SETUP", "Boot battery: %.2fV", boot_voltage);
		if (boot_voltage > 0.5f && boot_voltage <= (platform_batt_critical_default_mv() / 1000.0f)) {
			MYLOG("SETUP", "Battery too low at boot (%.2fV), entering indefinite sleep", boot_voltage);
			Serial.flush();
			while (1) { api.system.sleep.all(); }
		}
	}

	// 5-second window to send AT+BOOT before the stack initialises
	delay(5000);

	// Setup for LoRaWAN
	if (api.lorawan.nwm.get() == 1)
	{
		// Read AT-configurable TX settings (CFM/RETY remain user-settable via AT commands)
		g_confirmed_mode = api.lorawan.cfm.get();
		g_confirmed_retry = api.lorawan.rety.get();
		g_data_rate = api.lorawan.dr.get();

		// Setup the callbacks for joined and send finished
		api.lorawan.registerRecvCallback(receiveCallback);
		api.lorawan.registerSendCallback(sendCallback);
		api.lorawan.registerJoinCallback(joinCallback);
		api.lorawan.registerLinkCheckCallback(linkcheckCallback);

		// --- Join configuration: enforced in code each boot ---

		// OTAA join mode (overrides any ABP set via AT+NJM)
		api.lorawan.njm.set(1);
		MYLOG("SETUP", "Join mode: OTAA");

		// Region EU868 (overrides AT+BAND; change LORA_REGION define in app.h)
		api.lorawan.band.set(LORA_REGION);
		MYLOG("SETUP", "Region set to %d (EU868)", LORA_REGION);

		// Enforce Class A (overrides any Class B/C set via AT+CLASS)
		api.lorawan.deviceClass.set(RAK_LORA_CLASS_A);
		MYLOG("SETUP", "Class A enforced");

		// Verify OTAA credentials are provisioned (AppEUI + AppKey must be non-zero)
		{
			uint8_t key_buf[16] = {0};
			bool credentials_ok = true;

			api.lorawan.appeui.get(key_buf, 8);
			bool appeui_ok = false;
			for (int i = 0; i < 8; i++) { if (key_buf[i] != 0) { appeui_ok = true; break; } }
			if (!appeui_ok)
			{
				MYLOG("SETUP", "ERROR: AppEUI is all-zeros — not provisioned! Use AT+APPEUI");
				credentials_ok = false;
			}

			memset(key_buf, 0, sizeof(key_buf));
			api.lorawan.appkey.get(key_buf, 16);
			bool appkey_ok = false;
			for (int i = 0; i < 16; i++) { if (key_buf[i] != 0) { appkey_ok = true; break; } }
			if (!appkey_ok)
			{
				MYLOG("SETUP", "ERROR: AppKey is all-zeros — not provisioned! Use AT+APPKEY");
				credentials_ok = false;
			}

			if (credentials_ok)
			{
				MYLOG("SETUP", "OTAA credentials verified");

				if (api.lorawan.njs.get())
				{
					// Already joined via cached session (joinCallback fired before this point).
					// Do NOT call join() again — it would overwrite the ADR/TXP set by
					// joinCallback, kick off a redundant OTAA exchange, and if that fails
					// (JOIN_FAILED_RX_TIMEOUT) the cached session is lost.
					MYLOG("SETUP", "Already joined (cached session) — skipping join setup");
				}
				else
				{
					// Join profile: ADR off, max TX power, robust DR — best chance of first join
					api.lorawan.adr.set(false);
					MYLOG("SETUP", "ADR=off for join");

					api.lorawan.txp.set(JOIN_MAX_TXP);
					MYLOG("SETUP", "TXP=%d (max EIRP)", JOIN_MAX_TXP);

					api.lorawan.dr.set(JOIN_DR);
					MYLOG("SETUP", "Join DR=%d", JOIN_DR);

					// Start join; RUI3 valid interval range: 7-255 s. Count 255 = retry ~35 h before stopping.
					// api.lorawan.join() returns false when a cached OTAA session is already being
					// restored in the background — joinCallback will still fire with status=0.
					// Check njs to distinguish "already joining" from a genuine failure.
					if (!api.lorawan.join(1, 1, JOIN_RETRY_SEC, 255)) {
						MYLOG("SETUP", "join() returned false — cached session restore in progress or stack busy. Waiting for joinCallback...");
					} else {
						MYLOG("SETUP", "Join started — retry every %d s, up to 255 attempts", JOIN_RETRY_SEC);
					}
				}
			}
			else
			{
				MYLOG("SETUP", "OTAA credentials missing — join skipped. Provision with AT+APPEUI / AT+APPKEY");
			}
		}
	}
	else // Setup for LoRa P2P
	{
		api.lora.registerPRecvCallback(recv_cb);
		api.lora.registerPSendCallback(send_cb);
		api.lora.registerPSendCADCallback(cad_cb);
	}

	pinMode(LED_GREEN, OUTPUT);
	digitalWrite(LED_GREEN, HIGH);
	pinMode(LED_BLUE, OUTPUT);
	digitalWrite(LED_BLUE, HIGH);

 // pinMode(GPIO_PIN, INPUT_PULLUP); // Requires externall pull-up

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
	scanI2CDevices();
#endif

	// Register the custom AT command to get device status
	if (!init_status_at())
	{
		MYLOG("SETUP", "Add custom AT command STATUS fail");
	}

	// Register the custom AT command to set the send interval
	if (!init_interval_at())
	{
		MYLOG("SETUP", "Add custom AT command Send Interval fail");
	}

	// Register the custom AT command to set the minimal interval
	if (!init_min_interval_at())
	{
		MYLOG("SETUP", "Add custom AT command Minimal Interval fail");
	}

	// Register the custom AT command to set the battery critical threshold
	if (!init_batt_crit_at())
	{
		MYLOG("SETUP", "Add custom AT command Batt Critical fail");
	}

	// Get saved settings from flash
	get_at_setting();

	digitalWrite(LED_GREEN, LOW);


if (gpioInterruptEnabled) {
    MYLOG("SETUP", "Initializing GPIO interrupt system");
    if (init_gpio()) {
        MYLOG("SETUP", "GPIO interrupt system initialized successfully");
    } else {
        MYLOG("SETUP", "Failed to initialize GPIO interrupt system");
    }
}

  MYLOG("SETUP", "Create timer for interrupt handler");
	// Create a timer.
	api.system.timer.create(RAK_TIMER_0, sensor_handler, RAK_TIMER_PERIODIC);

	// Keep the voltage guard alive even while OTAA is retrying or periodic TX is
	// disabled. On join, joinCallback replaces this with the configured interval.
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

	if (api.lorawan.nwm.get() == 1)
	{
		MYLOG("SETUP", "CFM=%s, RETY=%d (AT-configurable)",
		      g_confirmed_mode ? "confirmed" : "unconfirmed", g_confirmed_retry);
	}

	// Enable low power mode
	api.system.lpm.set(1);

	// Start BLE advertising (nRF52840: required for SoftDevice init; no-op on STM32WLE5).
	// See platform_nrf52.cpp for power implications and the advertise.stop() caveat.
	platform_ble_start();

	// Check if sensors are connected and initialize them
	MYLOG("SETUP", "Auto-detecting and initializing sensors...");
	
	has_rak1901 = init_rak1901();
	if (has_rak1901)
	{
		Serial.println("+EVT:RAK1901");
		shutdown_rak1901();
		MYLOG("SETUP", "RAK1901 (Temp/Humidity) initialized successfully");
	}
	else
	{
		MYLOG("SETUP", "RAK1901 (Temp/Humidity) not detected or initialization failed");
	}
	
	has_rak1902 = init_rak1902();
	if (has_rak1902)
	{
		Serial.println("+EVT:RAK1902");
		shutdown_rak1902();
		MYLOG("SETUP", "RAK1902 (Pressure) initialized successfully");
	}
	else
	{
		MYLOG("SETUP", "RAK1902 (Pressure) not detected or initialization failed");
	}
	
	has_rak1903 = init_rak1903();
	if (has_rak1903)
	{
		Serial.println("+EVT:RAK1903");
		shutdown_rak1903();
		MYLOG("SETUP", "RAK1903 (Light) initialized successfully");
	}
	else
	{
		MYLOG("SETUP", "RAK1903 (Light) not detected or initialization failed");
	}
	
	has_rak1904 = init_rak1904();
	if (has_rak1904)
	{
		Serial.println("+EVT:RAK1904");
		MYLOG("SETUP", "RAK1904 (Accelerometer) initialized successfully");
	}
	else
	{
		MYLOG("SETUP", "RAK1904 (Accelerometer) not detected or initialization failed");
	}

	has_rak1906 = init_rak1906();
	if (has_rak1906)
	{
		Serial.println("+EVT:RAK1906");
		MYLOG("SETUP", "RAK1906 (Environmental) initialized successfully");
	}
	else
	{
		MYLOG("SETUP", "RAK1906 (Environmental) not detected or initialization failed");
	}

	// Count active sensors
	uint8_t active_sensors = (has_rak1901 ? 1 : 0) + (has_rak1902 ? 1 : 0) +
	                        (has_rak1903 ? 1 : 0) + (has_rak1904 ? 1 : 0) +
	                        (has_rak1906 ? 1 : 0);

	MYLOG("SETUP", "Sensor initialization complete. Active sensors: %d/5", active_sensors);
	MYLOG("SETUP", "  RAK1901(T/H)=%s, RAK1902(Press)=%s, RAK1903(Light)=%s, RAK1904(Acc)=%s, RAK1906(Env)=%s",
	      has_rak1901 ? "ON" : "OFF", has_rak1902 ? "ON" : "OFF",
	      has_rak1903 ? "ON" : "OFF", has_rak1904 ? "ON" : "OFF",
	      has_rak1906 ? "ON" : "OFF");

	// Set last trigger time to boot time
	last_trigger = millis();

	digitalWrite(LED_BLUE, LOW);

	// Signal that sensor init is complete. joinCallback checks this before firing sensor_handler
	// to avoid TX/RX events overlapping with sensor initialization.
	setup_complete = true;
	MYLOG("SETUP", "Setup complete");

	// If join already completed while setup() was running (cached credentials), joinCallback
	// would have seen setup_complete=false and deferred. Start timer and trigger first TX now.
	// Guard on timer_started: if joinCallback fired in the narrow window after setup_complete=true
	// but before this check, it already armed the timer — skip to avoid double-start.
	if (api.lorawan.nwm.get() == 1 && api.lorawan.njs.get() && !timer_started)
	{
		MYLOG("SETUP", "Already joined — starting send timer and triggering first transmission");
		if (custom_parameters.send_interval != 0)
		{
			api.lorawan.adr.set(true);
			api.lorawan.txp.set(POST_JOIN_TXP); // same moderate baseline as joinCallback path
			api.system.timer.start(RAK_TIMER_0, custom_parameters.send_interval, NULL);
		}
		sensor_handler(NULL);
	}
}

/**
 * @brief sensor_handler is a timer function called every
 * custom_parameters.send_interval milliseconds. Default is 120000. Can be
 * changed with ATC+SENDINT command
 *
 */
void sensor_handler(void* unused)
{
	MYLOG("POWER", "sensor_handler triggered");
	MYLOG("UPLINK", "Start");

	// This must be the first substantive action: do not write flash, wake
	// sensors, or key the radio while the direct supply is marginal.
	float battery_reading = platform_battery_get();
	if (battery_reading > 0.5f &&
	    custom_parameters.batt_critical_mv != 0 &&
	    battery_reading <= (custom_parameters.batt_critical_mv / 1000.0f)) {
		enterLowBatteryShutdown(battery_reading);
		return; // never reached
	}

	// A zero interval disables telemetry, not the protection timer.
	if (custom_parameters.send_interval == 0) {
		return;
	}

	if (api.lorawan.nwm.get() == 1)
	{
		// Check if the node has joined the network
		if (!api.lorawan.njs.get())
		{
			MYLOG("UPLINK", "Not joined, skip sending");
			return;
		}
	}

	// Apply any ACC config received via downlink — deferred here from receiveCallback
	// to avoid I2C calls inside the LoRa stack callback context.
	if (has_rak1904 && acc_config_pending) {
		acc_config_pending = false;
		setup_rak1904();
		// Persist updated acc config so it survives a reboot
		custom_parameters.interrupt_threshold = interrupt_threshold;
		custom_parameters.interrupt_duration  = interrupt_duration;
		custom_parameters.acc_frequency       = acc_frequency;
		save_at_setting();
		MYLOG("UPLINK", "ACC config saved: threshold=%d duration=%d freq_idx=%d",
			  interrupt_threshold, interrupt_duration, acc_frequency);
	}

	// Wakeup sensors
	manageSensors(true);

#if MY_DEBUG == 1
	digitalWrite(LED_BLUE, HIGH);
#endif

	MYLOG("UPLINK", "Read Sensors");

	// Clear payload
	g_solution_data.reset();

	// Get sensor values
	// RAK1906 writes to the same LPP channels as RAK1901 (T/H) and RAK1902 (pressure).
	// Only read RAK1901/RAK1902 when RAK1906 is absent to prevent payload overwrite.
	if (has_rak1901 && !has_rak1906)
	{
		read_rak1901();
	}
	if (has_rak1902 && !has_rak1906)
	{
		read_rak1902();
	}
	if (has_rak1903)
	{
		read_rak1903();
	}
  if (has_rak1904)
	{
		read_rak1904();
	}
	if (has_rak1906)
	{
		read_rak1906();
	}

	// Shutdown sensors after reading
	manageSensors(false);

	// Increment the transmission counter
	transmission_count++;

	// Send battery data and digital outputs every BATTERY_SEND_INTERVAL transmissions or on first boot
	if (shouldSendBatteryAndConfig())
	{
		if (battery_reading > 0.0f) {
			g_solution_data.addVoltage(LPP_CHANNEL_BATT_1, battery_reading);
		}
		// Report as DigitalInput (read-only sensor type) so the server/dashboard does
		// NOT treat these as actuator states and auto-resend them as downlinks.
		g_solution_data.addDigitalInput(LPP_CHANNEL_DIG_1, version_byte);
		g_solution_data.addDigitalInput(LPP_CHANNEL_DIG_2, interrupt_threshold);
		g_solution_data.addDigitalInput(LPP_CHANNEL_DIG_3, interrupt_duration);
		g_solution_data.addDigitalInput(LPP_CHANNEL_DIG_4, acc_frequency);
		
		// Update previous values
		previous_version_byte = version_byte;
		previous_interrupt_threshold = interrupt_threshold;
		previous_interrupt_duration = interrupt_duration;
		previous_acc_frequency = acc_frequency;
		
		// Reset counter and first boot flag only if this was a scheduled transmission
		if (transmission_count >= BATTERY_SEND_INTERVAL || first_boot)
		{
			transmission_count = 0; // Reset the counter
			first_boot = false;
			MYLOG("UPLINK", "Battery and digital outputs sent: %.2f V", battery_reading);
		}
		else
		{
			MYLOG("UPLINK", "Digital outputs sent due to config change");
		}
	}
	
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

	// Validate payload size before sending
	if (g_solution_data.getSize() > MAX_PAYLOAD_SIZE) {
		MYLOG("UPLINK", "Payload too large: %d bytes", g_solution_data.getSize());
		return;
	}

	// Send the packet
	send_packet();
	digitalWrite(LED_BLUE, LOW);
}

/**
 * @brief This example is complete timer driven.
 * The loop() does nothing than sleep.
 *
 */
void loop()
{
	// Disable ADC/peripherals before sleep (no-op on STM32WLE5).
	// See platform_pre_sleep() in platform_nrf52.cpp for details.
	platform_pre_sleep();

	api.system.sleep.all();
}

/**
 * @brief Send the data packet that was prepared in
 * Cayenne LPP format by the different sensor and location
 * aqcuision functions
 *
 */
bool send_packet(void)
{
	if (g_solution_data.getSize() == 0) {
        MYLOG("UPLINK", "No data to send");
        return false;
    }
    
    // Print the payload buffer
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
		if (!api.lorawan.njs.get()) {
            MYLOG("UPLINK", "Not joined to network, cannot send");
            return false;
        }
        
		MYLOG("UPLINK", "Sending packet over LoRaWAN");
		
		// Send the packet - Note: RUI3 API may return false even when transmission succeeds
		if (api.lorawan.send(g_solution_data.getSize(), g_solution_data.getBuffer(), set_fPort, g_confirmed_mode, g_confirmed_retry))
		{
			MYLOG("UPLINK", "Packet enqueued successfully, size %d", g_solution_data.getSize());
			return true;
		}
		else
		{
			MYLOG("UPLINK", "Enqueue returned false (check TX callback for actual result)");
			return false;
		}
	}
	// It is P2P
	else
	{
		MYLOG("UPLINK", "Send packet with size %d over P2P", g_solution_data.getSize());

		if (api.lora.psend(g_solution_data.getSize(), g_solution_data.getBuffer(), true))
		{
			MYLOG("UPLINK", "Packet enqueued");
			return true;
		}
		else
		{
			MYLOG("UPLINK", "Send failed");
      // tx_active = false;
      
			// If it was motion triggered, reset the ACC interrupts
			if (has_rak1904)
			{
				clear_int_rak1904();
			}
			return false;
		}
	}
}

