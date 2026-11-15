/**
 * @file battery_policy.h
 * @brief Pure low-battery decision rules (no hardware access, host-testable).
 *
 * battery.cpp feeds these with real ADC readings; tests/host feeds them with
 * synthetic ones. Keep this header free of Arduino/RUI3 includes.
 */
#pragma once

#include <stdint.h>

namespace battery_policy
{
	/** Readings at or below this are ADC failures / "no battery", not a voltage. */
	constexpr float INVALID_BELOW_V = 0.5f;

	inline bool valid(float volts)
	{
		return volts > INVALID_BELOW_V;
	}

	inline float critical_v(uint16_t critical_mv)
	{
		return critical_mv / 1000.0f;
	}

	/** Supply must exceed critical by this margin before telemetry resumes. */
	inline float resume_v(uint16_t critical_mv, float margin_v)
	{
		return critical_v(critical_mv) + margin_v;
	}

	/**
	 * One reading at or below the critical threshold. critical_mv == 0 means the
	 * guard is disabled (ATC+BATCRIT=0), so nothing is ever critical.
	 */
	inline bool is_critical(float volts, uint16_t critical_mv)
	{
		return critical_mv != 0 && valid(volts) && volts <= critical_v(critical_mv);
	}

	/**
	 * Supply back above the resume threshold. Invalid readings never count as
	 * recovered; a disabled guard has nothing to recover from.
	 */
	inline bool recovered(float volts, uint16_t critical_mv, float margin_v)
	{
		if (!valid(volts))
		{
			return false;
		}
		if (critical_mv == 0)
		{
			return true;
		}
		return volts > resume_v(critical_mv, margin_v);
	}
} // namespace battery_policy
