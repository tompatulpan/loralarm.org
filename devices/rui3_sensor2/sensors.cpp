/**
 * @file sensors.cpp
 * @brief Sensor registry: one table drives init, wake-up, read and shutdown.
 */
#include "app.h"

struct SensorDesc
{
	const char *name;		 // for logs
	const char *evt;		 // "+EVT:" line announced on the serial port when found
	bool (*init)(void);
	void (*startup)(void);
	void (*read)(void);
	void (*shutdown)(void);
	bool sleep_after_init;	 // call shutdown() straight after a successful init
	void (*read_alert)(void); // quick temp/humidity-only reading for motion alerts, nullptr = none
	int8_t suppressed_by;	 // SensorId whose presence makes read() redundant (same LPP channels), -1 = never
	bool present;
};

// Table order = init, wake-up, read and shutdown order (matches rui3_sensor v33).
// RAK1906 writes the same LPP channels as RAK1901 (T/H) and RAK1902 (pressure),
// so those two are not read while a RAK1906 is present: a second value on the
// same channel would overwrite/duplicate it in the payload. The same rule applies
// to the alert readings: the RAK1906 supplies T/H there (heater-off quick read).
static SensorDesc sensors[SENSOR_COUNT] = {
	// SENSOR_RAK1901
	{"RAK1901 (Temp/Humidity)", "+EVT:RAK1901", init_rak1901, startup_rak1901, read_rak1901, shutdown_rak1901, true, read_rak1901, SENSOR_RAK1906, false},
	// SENSOR_RAK1902
	{"RAK1902 (Pressure)", "+EVT:RAK1902", init_rak1902, startup_rak1902, read_rak1902, shutdown_rak1902, true, nullptr, SENSOR_RAK1906, false},
	// SENSOR_RAK1903
	{"RAK1903 (Light)", "+EVT:RAK1903", init_rak1903, startup_rak1903, read_rak1903, shutdown_rak1903, true, nullptr, -1, false},
	// SENSOR_RAK1904: keeps its configured ODR running during sleep, so no sleep after init
	// (its X/Y/Z is added by send_acc_alert() itself)
	{"RAK1904 (Accelerometer)", "+EVT:RAK1904", init_rak1904, startup_rak1904, read_rak1904, shutdown_rak1904, false, nullptr, -1, false},
	// SENSOR_RAK1906: forced mode, sleeps on its own after each reading
	{"RAK1906 (Environmental)", "+EVT:RAK1906", init_rak1906, startup_rak1906, read_rak1906, shutdown_rak1906, false, read_rak1906_fast, -1, false},
};

bool sensor_present(SensorId id)
{
	return sensors[id].present;
}

/** True if this sensor's reading would duplicate another present sensor's channels. */
static bool is_suppressed(const SensorDesc &s)
{
	return s.suppressed_by >= 0 && sensors[s.suppressed_by].present;
}

void sensors_init_all(void)
{
	MYLOG("SETUP", "Auto-detecting and initializing sensors...");

	uint8_t active = 0;
	for (uint8_t i = 0; i < SENSOR_COUNT; i++)
	{
		SensorDesc &s = sensors[i];
		s.present = s.init();
		if (s.present)
		{
			active++;
			Serial.println(s.evt);
			if (s.sleep_after_init)
			{
				s.shutdown();
			}
			MYLOG("SETUP", "%s initialized successfully", s.name);
		}
		else
		{
			MYLOG("SETUP", "%s not detected or initialization failed", s.name);
		}
	}

	MYLOG("SETUP", "Sensor initialization complete. Active sensors: %d/%d", active, SENSOR_COUNT);
}

void sensors_wakeup(void)
{
	MYLOG("SENSORS", "Waking up active sensors...");
	for (uint8_t i = 0; i < SENSOR_COUNT; i++)
	{
		if (sensors[i].present)
		{
			MYLOG("SENSORS", "Starting %s", sensors[i].name);
			sensors[i].startup();
		}
	}
	// Warmup time for active sensors
	MYLOG("SENSORS", "Sensor warmup delay: %d ms", SENSOR_WARMUP_TIME);
	delay(SENSOR_WARMUP_TIME);
	MYLOG("SENSORS", "Active sensors ready");
}

void sensors_shutdown(void)
{
	MYLOG("SENSORS", "Shutting down active sensors...");
	for (uint8_t i = 0; i < SENSOR_COUNT; i++)
	{
		if (sensors[i].present)
		{
			MYLOG("SENSORS", "Shutting down %s", sensors[i].name);
			sensors[i].shutdown();
		}
	}
	MYLOG("SENSORS", "Sensor shutdown complete");
}

// Heartbeat read order = payload priority (payload_budget.h): the frame budget is
// armed by uplink_arm_frame_budget() and the Cayenne layer fills most-important-first
// for the current DR, so a low-DR frame drops the channels read last instead of the
// heartbeat. The accelerometer (tilt baseline, motion context) and the environmental
// temperature/humidity come first; light is pure telemetry and reads last. The table
// order above still drives init, wake-up and shutdown.
static const SensorId read_order[SENSOR_COUNT] = {
	SENSOR_RAK1904, // ch 6 XYZ — 8 bytes, tilt detector input
	SENSOR_RAK1906, // ch 2/3 T/H, ch 4 pressure, ch 14 VOC (T/H added first inside the driver)
	SENSOR_RAK1901, // ch 2/3 T/H only
	SENSOR_RAK1902, // ch 4 pressure only
	SENSOR_RAK1903, // ch 5 light — telemetry, least important
};

void sensors_read_all(void)
{
	for (uint8_t prio = 0; prio < SENSOR_COUNT; prio++)
	{
		const SensorDesc &s = sensors[read_order[prio]];
		if (s.present && !is_suppressed(s))
		{
			s.read();
		}
	}
}

void sensors_read_for_alert(void)
{
	uint8_t read_count = 0;
	for (uint8_t i = 0; i < SENSOR_COUNT; i++)
	{
		const SensorDesc &s = sensors[i];
		if (s.read_alert == nullptr || !s.present || is_suppressed(s))
		{
			continue;
		}
		s.read_alert();
		read_count++;
	}
	MYLOG("ALERT", "%d sensor(s) added to the alert", read_count);
}

/** Known I2C addresses -> human label (diagnostics only). */
struct I2cKnown
{
	uint8_t address;
	const char *label;
	SensorId id;
};

static const I2cKnown i2c_known[] = {
	{0x70, "SHTC3 (RAK1901) Temp/Humidity Sensor", SENSOR_RAK1901},
	{0x5C, "LPS35HW (RAK1902) Pressure Sensor", SENSOR_RAK1902},
	{0x5D, "LPS22HB (RAK1902) Pressure Sensor", SENSOR_RAK1902},
	{0x44, "OPT3001 (RAK1903) Light Sensor", SENSOR_RAK1903},
	{0x18, "LIS3DH (RAK1904) Accelerometer (SDO=GND)", SENSOR_RAK1904},
	{0x19, "LIS3DH (RAK1904) Accelerometer (SDO=VCC)", SENSOR_RAK1904},
	{0x76, "BME680 (RAK1906) Environmental Sensor (SDO=GND)", SENSOR_RAK1906},
	{0x77, "BME680 (RAK1906) Environmental Sensor (SDO=VCC)", SENSOR_RAK1906},
};

void sensors_scan_i2c(void)
{
	MYLOG("I2C", "Scanning I2C bus for connected sensors...");

	uint8_t device_count = 0;
	bool found[SENSOR_COUNT] = {false};

	for (uint8_t address = 1; address < 127; address++)
	{
		Wire.beginTransmission(address);
		uint8_t error = Wire.endTransmission();

		if (error == 0)
		{
			device_count++;
			MYLOG("I2C", "Device found at address 0x%02X", address);

			bool known = false;
			for (const I2cKnown &k : i2c_known)
			{
				if (k.address == address)
				{
					MYLOG("I2C", "  -> %s", k.label);
					found[k.id] = true;
					known = true;
					break;
				}
			}
			if (!known)
			{
				MYLOG("I2C", "  -> Unknown device at 0x%02X", address);
			}
		}
		else if (error == 4)
		{
			MYLOG("I2C", "Unknown error at address 0x%02X", address);
		}
	}

	MYLOG("I2C", "Detected sensors: RAK1901=%s, RAK1902=%s, RAK1903=%s, RAK1904=%s, RAK1906=%s",
		  found[SENSOR_RAK1901] ? "YES" : "NO", found[SENSOR_RAK1902] ? "YES" : "NO",
		  found[SENSOR_RAK1903] ? "YES" : "NO", found[SENSOR_RAK1904] ? "YES" : "NO",
		  found[SENSOR_RAK1906] ? "YES" : "NO");

	if (device_count == 0)
	{
		MYLOG("I2C", "No I2C devices found! Check connections.");
	}
	else
	{
		MYLOG("I2C", "Scan complete. Found %d device(s)", device_count);
	}
}
