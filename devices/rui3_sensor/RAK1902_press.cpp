/**
 * @file RAK1902_press.cpp
 * @author Bernd Giesecke (bernd@giesecke.tk)
 * @brief Initialize and read values from the LPS22HB sensor
 * @version 0.1
 * @date 2022-04-11
 *
 * @copyright Copyright (c) 2022
 *
 */
#include "app.h"
#include <LPS35HW.h>

/** Sensor instance */
LPS35HW lps;

/**
 * @brief Initialize barometric pressure sensor
 *
 * @return true if sensor was found
 * @return false if initialization failed
 */
bool init_rak1902(void)
{
	// Try to initialize the sensor with a small delay first
	delay(100);
	
	if (!lps.begin(&Wire))
	{
		// Sensor not found - this is normal if RAK1902 is not populated
		return false;
	}

	MYLOG("PRESS", "LPS35HW pressure sensor initialized successfully");
	
	lps.setLowPower(true);
	lps.setOutputRate(LPS35HW::OutputRate_75Hz);	   // 75 Hz sample rate
	lps.setLowPassFilter(LPS35HW::LowPassFilter_ODR9); // default is off

	shutdown_rak1902();
	return true;
}

/**
 * @brief Read the barometric pressure
 *     Data is added to Cayenne LPP payload as channel
 *     LPP_CHANNEL_PRESS
 *
 */
void read_rak1902(void)
{
	MYLOG("PRESS", "Reading LPS22HB");

	// OneShot mode triggers a single conversion. Poll the status register until
	// the pressure-data-ready bit (bit 1) is set, with a 40 ms safety timeout.
	// LPS35HW one-shot conversion completes in ~12 ms at the first ODR cycle.
	uint32_t deadline = millis() + 40;
	while (millis() < deadline) {
		// STATUS register (0x27): bit 1 = P_DA (pressure data available)
		// Read via Wire directly since the LPS35HW library has no status-poll API.
		Wire.beginTransmission(0x5C);  // LPS35HW default address
		Wire.write(0x27);
		if (Wire.endTransmission(false) == 0) {
			Wire.requestFrom((uint8_t)0x5C, (uint8_t)1);
			if (Wire.available() && (Wire.read() & 0x02)) {
				break;  // P_DA set — data ready
			}
		}
		delay(2);
	}
	float pressure = lps.readPressure(); // hPa

	// Switch back to low power mode
	lps.setLowPower(true);
	lps.setOutputRate(LPS35HW::OutputRate_OneShot); // 75 Hz sample rate

	MYLOG("PRESS", "P: %.2f", pressure);

	g_solution_data.addBarometricPressure(LPP_CHANNEL_PRESS_1, pressure);

#if HAS_EPD > 0
	set_baro_rak14000(pressure);
#endif
}

/**
 * @brief Wake up RAK1902 from sleep
 *
 */
void startup_rak1902(void)
{
	lps.setLowPower(false);						 // Disable low power mode
	lps.setOutputRate(LPS35HW::OutputRate_75Hz); // 75 Hz sample rate
}

/**
 * @brief Put the RAK1902 into sleep mode
 *
 */
void shutdown_rak1902(void)
{
	lps.setOutputRate(LPS35HW::OutputRate_OneShot); // Oneshot
	lps.setLowPower(true);
}
