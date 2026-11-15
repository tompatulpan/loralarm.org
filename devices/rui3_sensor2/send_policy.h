/**
 * @file send_policy.h
 * @brief Pure heartbeat content decisions (no hardware access, host-testable).
 */
#pragma once

#include <stdint.h>

/** The configuration values a heartbeat reports (ch 7..10). */
struct ReportedConfig
{
	uint8_t version;
	uint8_t threshold;
	uint8_t duration;
	uint8_t frequency;
};

namespace send_policy
{
	/**
	 * True when the heartbeat must carry battery voltage, config and latch state:
	 * on the scheduled interval, on first boot, or whenever a reported value
	 * changed since it was last sent (so the server sees config updates at once).
	 */
	inline bool should_send_battery_and_config(uint8_t transmission_count,
											   uint8_t battery_send_interval,
											   bool first_boot,
											   const ReportedConfig &now,
											   const ReportedConfig &last_sent)
	{
		return transmission_count >= battery_send_interval ||
			   first_boot ||
			   now.version != last_sent.version ||
			   now.threshold != last_sent.threshold ||
			   now.duration != last_sent.duration ||
			   now.frequency != last_sent.frequency;
	}
} // namespace send_policy
