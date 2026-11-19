/**
 * @file payload_budget.h
 * @brief Pure LoRaWAN FRMPayload budget arithmetic (EU868) for the uplink frames.
 *
 * The limit the payload code must obey is not a constant: it depends on the
 * data rate of the frame being built. In EU868 (LoRaWAN 1.0.3 Regional
 * Parameters) the maximum MACPayload is 51 bytes at DR0-DR2 (SF12/SF11/SF10)
 * and 115 at DR3-DR5 — of which FHDR (7) + FPort (1) belong to the frame, so
 * the application (Cayenne FRMPayload) gets 43 resp. 107 usable bytes.
 *
 *   DR0-2: MaxMACPayload  51 -> 43 FRMPayload
 *   DR3-5: MaxMACPayload 115 -> 107 FRMPayload
 *   DR6:   MaxMACPayload 242 -> 234 FRMPayload
 *   DR7:   MaxMACPayload 250 -> 242 FRMPayload (FSK)
 *
 * MAC commands the network piggy-backs in FOpts can shrink the budget by up to
 * 15 bytes more. No reserve is held back for them: the stack then refuses the
 * send (send_packet() logs "Enqueue returned false"), and holding back would
 * cost every low-DR heartbeat data it could have carried.
 *
 * Frames are built most-important-channel-first (see uplink.cpp and the
 * read_order in sensors.cpp) with the budget armed via WisCayenne::setMaxSize(),
 * so a DR0 heartbeat drops its tail (pressure/light/gas) instead of the whole
 * frame. tests/host/test_payload_budget.cpp locks this accounting.
 *
 * Only EU868 is implemented. The static assert below makes a different region
 * fail the build instead of silently reusing these numbers (US915 DR0 = 11!).
 * The check only runs when config.h (which defines LORA_REGION) is included first.
 *
 * The CH_* sizes below are cross-checked against the CayenneLPP *_SIZE constants by
 * static_asserts in uplink.cpp.
 */
#pragma once

#include <stdint.h>

#ifdef LORA_REGION
static_assert(LORA_REGION == 4, "payload_budget.h only has the EU868 MaxMACPayload table; "
								"add the region's table before building with another LORA_REGION");
#endif

namespace payload_budget
{

	/**
	 * @brief Maximum MACPayload size in bytes for EU868 by DR.
	 * @param dr LoRaWAN data rate index as reported by api.lorawan.dr.get()
	 */
	static inline uint16_t eu868_max_mac_payload(uint8_t dr)
	{
		if (dr <= 2)
		{
			return 51; // SF12/SF11/SF10 @ 125 kHz
		}
		if (dr <= 5)
		{
			return 115; // SF9/SF8/SF7 @ 125 kHz
		}
		if (dr == 6)
		{
			return 242; // SF7 @ 250 kHz
		}
		return dr == 7 ? 250 : 51; // FSK; an out-of-range dr gets the DR0 worst case
	}

	/**
	 * @brief FRMPayload bytes available to the application at this DR.
	 *
	 * FHDR (7) + FPort (1) belong to the frame, the rest is FRMPayload.
	 * This is the value passed to WisCayenne::setMaxSize() before building a frame.
	 */
	static inline uint16_t frmpayload(uint8_t dr)
	{
		return eu868_max_mac_payload(dr) - 8;
	}

	// --- Channel accounting (Cayenne LPP, 1 channel byte + 1 type byte + data) ------
	// The wire sizes of every channel this firmware sends. Keep in sync with the
	// LPP_CHANNEL_* map in config.h and the add* calls in uplink.cpp / the drivers.
	static const uint8_t CH_DIGITAL = 3;	  // addDigitalInput / addDigitalOutput (1 data byte)
	static const uint8_t CH_VOLTAGE = 4;	  // addVoltage (2)
	static const uint8_t CH_TEMPERATURE = 4;  // addTemperature (2)
	static const uint8_t CH_HUMIDITY = 3;	  // addRelativeHumidity (1)
	static const uint8_t CH_PRESSURE = 4;	  // addBarometricPressure (2)
	static const uint8_t CH_LUMINOSITY = 4;  // addLuminosity (2)
	static const uint8_t CH_ACCELEROMETER = 8; // addAccelerometer (6)
	static const uint8_t CH_VOC = 4;		  // addVoc_index (2)

} // namespace payload_budget
