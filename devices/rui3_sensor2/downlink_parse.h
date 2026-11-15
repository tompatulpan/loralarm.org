/**
 * @file downlink_parse.h
 * @brief Pure parser for the 3-digit ASCII accelerometer downlinks (host-testable).
 *
 * Ports: 5 = interrupt threshold, 6 = ODR index, 7 = interrupt duration.
 * Payload: at least 3 ASCII digits "000".."999"; only the first three are read.
 */
#pragma once

#include <stdint.h>

/** Accelerometer settings a downlink may change. */
struct AccParams
{
	uint8_t threshold;
	uint8_t frequency;
	uint8_t duration;
};

enum DownlinkResult
{
	DL_APPLIED,		  // value accepted and written to the AccParams field
	DL_EMPTY,		  // empty payload (network acknowledgement) — nothing to do
	DL_TOO_SHORT,	  // fewer than 3 bytes
	DL_BAD_DIGIT,	  // one of the first three bytes is not '0'..'9'
	DL_OUT_OF_RANGE,  // value outside what the register/port accepts
	DL_UNKNOWN_PORT	  // port is not an accelerometer port
};

namespace downlink
{
	constexpr uint8_t PORT_THRESHOLD = 5;
	constexpr uint8_t PORT_FREQUENCY = 6;
	constexpr uint8_t PORT_DURATION = 7;

	inline bool is_acc_port(uint8_t port)
	{
		return port >= PORT_THRESHOLD && port <= PORT_DURATION;
	}

	/**
	 * Validate and apply one downlink. On anything but DL_APPLIED, @p params is
	 * left untouched.
	 */
	inline DownlinkResult apply(uint8_t port, const uint8_t *buf, uint16_t len, AccParams &params)
	{
		if (len == 0)
		{
			return DL_EMPTY;
		}
		if (len < 3)
		{
			return DL_TOO_SHORT;
		}
		for (int i = 0; i < 3; i++)
		{
			if (buf[i] < '0' || buf[i] > '9')
			{
				return DL_BAD_DIGIT;
			}
		}

		// uint16_t: "999" must not wrap (it did when this was a uint8_t)
		const uint16_t value = (buf[0] - '0') * 100 + (buf[1] - '0') * 10 + (buf[2] - '0');

		switch (port)
		{
		case PORT_THRESHOLD: // 7-bit register, valid 1-127
			if (value < 1 || value > 127)
			{
				return DL_OUT_OF_RANGE;
			}
			params.threshold = (uint8_t)value;
			return DL_APPLIED;
		case PORT_FREQUENCY: // ODR index, valid 1-7 (0 = power-down is rejected)
			if (value < 1 || value > 7)
			{
				return DL_OUT_OF_RANGE;
			}
			params.frequency = (uint8_t)value;
			return DL_APPLIED;
		case PORT_DURATION: // 7-bit register, valid 0-127
			if (value > 127)
			{
				return DL_OUT_OF_RANGE;
			}
			params.duration = (uint8_t)value;
			return DL_APPLIED;
		default:
			return DL_UNKNOWN_PORT;
		}
	}
} // namespace downlink
