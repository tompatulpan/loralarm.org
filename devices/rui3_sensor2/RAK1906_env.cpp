/**
 * @file RAK1906_env.cpp
 * @brief Initialize and read data from BME680 environmental sensor
 * @version 0.1
 * @date 2026-06-02
 *
 * @copyright Copyright (c) 2026
 *
 * RAK1906 = Bosch BME680: temperature, humidity, barometric pressure, gas resistance.
 * I2C address: 0x76 (SDO = GND, default) or 0x77 (SDO = VCC).
 * Library: Adafruit BME680 (http://librarymanager/All#Adafruit_BME680)
 *
 * The BME680 uses forced mode in the Adafruit library: it wakes on performReading()
 * and returns to sleep automatically when the measurement cycle completes.
 * No explicit startup/shutdown is needed between readings.
 */
#include "app.h"
#include <Adafruit_BME680.h>

/** Sensor instance */
Adafruit_BME680 bme;

/** BME680 gas heater profile used for the full (heartbeat) reading */
static const uint16_t GAS_HEATER_TEMP_C = 320;
static const uint16_t GAS_HEATER_TIME_MS = 150;

/**
 * @brief Initialize the BME680 environmental sensor
 *
 * @return true if initialization succeeded
 * @return false if sensor could not be found
 */
bool init_rak1906(void)
{
	// Try default I2C address (SDO = GND), then alternate (SDO = VCC)
	if (!bme.begin(0x76) && !bme.begin(0x77))
	{
		// Sensor not found — normal if RAK1906 is not populated
		return false;
	}

	bme.setTemperatureOversampling(BME680_OS_8X);
	bme.setHumidityOversampling(BME680_OS_2X);
	bme.setPressureOversampling(BME680_OS_4X);
	// IIR filter off: with one forced reading every few minutes the filter gains nothing
	// but makes each reading only partly follow the real value (seen as a slow decay of
	// T over several heartbeats), which delays temperature alarms.
	bme.setIIRFilterSize(BME680_FILTER_SIZE_0);
	bme.setGasHeater(GAS_HEATER_TEMP_C, GAS_HEATER_TIME_MS);

	MYLOG("ENV", "BME680 environmental sensor initialized successfully");
	return true;
}

/**
 * @brief Read temperature, humidity, pressure, and gas resistance from BME680.
 *     Data is added to Cayenne LPP payload on channels:
 *     LPP_CHANNEL_TEMP_1  (2)  — temperature (°C)
 *     LPP_CHANNEL_HUMID_1 (3)  — relative humidity (%RH)
 *     LPP_CHANNEL_PRESS_1 (4)  — barometric pressure (hPa)
 *     LPP_CHANNEL_GAS_1   (14) — gas resistance (kΩ, LPP type 0x8A)
 */
void read_rak1906(void)
{
	MYLOG("ENV", "Reading BME680");

	if (!bme.performReading())
	{
		MYLOG("ENV", "BME680 reading failed");
		return;
	}

	float temp_f       = bme.temperature;           // °C
	float humid_f      = bme.humidity;              // %RH
	float press_f      = bme.pressure / 100.0f;     // Pa → hPa
	// Round to nearest kΩ (+ 500 avoids floor truncation: 1234 Ω → 1 kΩ becomes 1 kΩ, 1500 Ω → 2 kΩ).
	// addVoc_index packs into a uint16_t, so the max representable value is 65535 kΩ = 65 MΩ,
	// which covers the full BME680 gas resistance range (~1 kΩ – 1 MΩ).
	uint32_t gas_kohm  = (bme.gas_resistance + 500) / 1000;

	MYLOG("ENV", "T: %.2f H: %.2f P: %.2f Gas: %lu kOhm",
	      temp_f, humid_f, press_f, gas_kohm);

	g_solution_data.addTemperature(LPP_CHANNEL_TEMP_1, temp_f);
	g_solution_data.addRelativeHumidity(LPP_CHANNEL_HUMID_1, humid_f);
	g_solution_data.addBarometricPressure(LPP_CHANNEL_PRESS_1, press_f);
	g_solution_data.addVoc_index(LPP_CHANNEL_GAS_1, gas_kohm);
}

/**
 * @brief Quick temperature + humidity reading for motion alerts (ch 2/3 only).
 *
 * The full reading in read_rak1906() spends ~390 ms, mostly in the gas heater.
 * Here the heater is switched off for one forced measurement, then restored, so
 * the alert is delayed by tens of ms instead.
 */
void read_rak1906_fast(void)
{
	bme.setGasHeater(0, 0);
	bool ok = bme.performReading();
	bme.setGasHeater(GAS_HEATER_TEMP_C, GAS_HEATER_TIME_MS);
	if (!ok)
	{
		MYLOG("ENV", "BME680 fast reading failed");
		return;
	}

	MYLOG("ENV", "Fast T: %.2f H: %.2f", bme.temperature, bme.humidity);
	g_solution_data.addTemperature(LPP_CHANNEL_TEMP_1, bme.temperature);
	g_solution_data.addRelativeHumidity(LPP_CHANNEL_HUMID_1, bme.humidity);
}

/**
 * @brief Wake up BME680 (no-op — forced mode wakes on performReading)
 */
void startup_rak1906(void)
{
	// BME680 in forced mode wakes automatically when performReading() is called
}

/**
 * @brief Put BME680 into sleep (no-op — forced mode sleeps after each reading)
 */
void shutdown_rak1906(void)
{
	// BME680 in forced mode returns to sleep automatically after performReading()
}
