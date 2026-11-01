/**
 * @file RAK1903_light.cpp
 * @author Bernd Giesecke (bernd@giesecke.tk)
 * @brief Initialize and read data from OPT3001 sensor
 * @version 0.1
 * @date 2022-04-11
 * 
 * @copyright Copyright (c) 2022
 * 
 */
#include "app.h"
#include <ClosedCube_OPT3001.h>

/** Sensor instance */
ClosedCube_OPT3001 opt3001;
/** Sensor I2C address */
#define OPT3001_ADDRESS 0x44
/** OPT3001 configutation */
OPT3001_Config newConfig;

/**
 * @brief Initialize the Light sensor
 *
 * @return true if sensor found and configuration success
 * @return false if error occured
 */
bool init_rak1903(void)
{
	delay(100); // Give the sensor time to initialize
	
	if (opt3001.begin(OPT3001_ADDRESS) != NO_ERROR)
	{
		// Sensor not found - this is normal if RAK1903 is not populated
		return false;
	}

	newConfig.RangeNumber = B1100;
	newConfig.ConvertionTime = B0;
	newConfig.Latch = B1;
	newConfig.ModeOfConversionOperation = B11;

	// writeConfig() is the first real I2C transaction — begin() only stores the address.
	// A failure here means the sensor is not populated (normal) rather than a config error.
	OPT3001_ErrorCode errorConfig = opt3001.writeConfig(newConfig);
	if (errorConfig != NO_ERROR)
	{
		MYLOG("LIGHT", "OPT3001 not populated or not responding (writeConfig error %d)", (int)errorConfig);
		return false;
	}
	
	MYLOG("LIGHT", "OPT3001 light sensor initialized and configured successfully");
	return true;
}

/**
 * @brief Read value from light sensor
 *     Data is added to Cayenne LPP payload as channel
 *     LPP_CHANNEL_LIGHT
 *
 */
void read_rak1903()
{
	MYLOG("LIGHT", "Reading OPT3001");
	OPT3001 result = opt3001.readResult();
	if (result.error == NO_ERROR)
	{
		MYLOG("LIGHT", "L: %.2f lux", result.lux);
		g_solution_data.addLuminosity(LPP_CHANNEL_LIGHT_1, (uint32_t)result.lux);
	}
	else
	{
		MYLOG("LIGHT", "Error reading OPT3001");
	}
}

/**
 * @brief Wake up RAK1903 from sleep
 *
 */
void startup_rak1903(void)
{
	newConfig.RangeNumber = 0b1100;				// B1100;
	newConfig.ConvertionTime = 0b0;				// B0;
	newConfig.Latch = 0b1;						// B1;
	newConfig.ModeOfConversionOperation = 0b11; // B11;

	OPT3001_ErrorCode errorConfig = opt3001.writeConfig(newConfig);
	if (errorConfig != NO_ERROR)
	{
		MYLOG("LIGHT", "Could not configure OPT3001");
	}
}

/**
 * @brief Put the RAK1903 into sleep mode
 *
 */
void shutdown_rak1903(void)
{
	newConfig.RangeNumber = 0b1100;				// B1100;
	newConfig.ConvertionTime = 0b0;				// B0;
	newConfig.Latch = 0b1;						// B1;
	newConfig.ModeOfConversionOperation = 0b00; // B00;

	OPT3001_ErrorCode errorConfig = opt3001.writeConfig(newConfig);
	if (errorConfig != NO_ERROR)
	{
		MYLOG("LIGHT", "Could not configure OPT3001");
	}
}