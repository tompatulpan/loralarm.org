/**
 * @file wisblock_cayenne.h
 * @author Bernd Giesecke (bernd.giesecke@rakwireless.com)
 * @brief Extend CayenneLPP class with custom channels
 * @version 0.1
 * @date 2022-01-29
 *
 * @copyright Copyright (c) 2022
 *
 */
#ifndef WISBLOCK_CAYENNE_H
#define WISBLOCK_CAYENNE_H

#include <Arduino.h>
// #include <ArduinoJson.h>
#include <CayenneLPP.h>

#define LPP_GPS4 136 // 3 byte lon/lat 0.0001 °, 3 bytes alt 0.01 meter (Cayenne LPP default)
#define LPP_GPS6 137 // 4 byte lon/lat 0.000001 °, 3 bytes alt 0.01 meter (Customized Cayenne LPP)
#define LPP_VOC 138	 // 2 byte VOC index

// Only Data Size
#define LPP_GPS4_SIZE 9
#define LPP_GPS6_SIZE 11
#define LPP_GPSH_SIZE 14
#define LPP_GPST_SIZE 10
#define LPP_VOC_SIZE 2

class WisCayenne : public CayenneLPP
{
public:
	WisCayenne(uint8_t size) : CayenneLPP(size), _buffer_size(size) {}

	/**
	 * Shrink the usable payload budget for the frame being built (never above the
	 * allocated buffer). uplink_arm_frame_budget() calls this with the LoRaWAN
	 * FRMPayload budget of the current DR before every frame; add* calls that do
	 * not fit raise LPP_ERROR_OVERFLOW and are skipped — that is how a low-DR
	 * frame drops its least-important channels instead of the whole heartbeat.
	 */
	void setMaxSize(uint8_t size)
	{
		if (size <= _buffer_size)
		{
			_maxsize = size;
		}
	}

	uint8_t addGNSS_4(uint8_t channel, int32_t latitude, int32_t longitude, int32_t altitude);
	uint8_t addGNSS_6(uint8_t channel, int32_t latitude, int32_t longitude, int32_t altitude);
	uint8_t addGNSS_H(int32_t latitude, int32_t longitude, int16_t altitude, int16_t accuracy, int16_t battery);
	uint8_t addGNSS_T(int32_t latitude, int32_t longitude, int16_t altitude, float accuracy, int8_t sats);
	uint8_t addVoc_index(uint8_t channel, uint32_t voc_index);

private:
	/** Bytes actually allocated in the constructor; setMaxSize() never exceeds it. */
	uint8_t _buffer_size;
};
#endif