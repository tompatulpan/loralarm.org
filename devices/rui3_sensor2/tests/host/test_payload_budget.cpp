// Host tests for the LoRaWAN payload budget and the heartbeat channel accounting.
// Locks the fix for the DR0 gap: a fully populated board builds a 52-byte heartbeat,
// which exceeds the 43 usable FRMPayload bytes at DR0-DR2 (EU868), so the frame must
// be built most-important-first and drop only its tail at a low DR — never the
// heartbeat, and never a channel the alarm rules read.
// Build/run: make -C tests/host
#include <cassert>
#include <cstdio>

#include "../../payload_budget.h"

static int checks = 0;
#define CHECK(cond)                                                    \
	do                                                                 \
	{                                                                  \
		checks++;                                                      \
		if (!(cond))                                                   \
		{                                                              \
			std::printf("FAIL %s:%d  %s\n", __FILE__, __LINE__, #cond); \
			return 1;                                                  \
		}                                                              \
	} while (0)

using payload_budget::CH_ACCELEROMETER;
using payload_budget::CH_DIGITAL;
using payload_budget::CH_HUMIDITY;
using payload_budget::CH_LUMINOSITY;
using payload_budget::CH_PRESSURE;
using payload_budget::CH_TEMPERATURE;
using payload_budget::CH_VOLTAGE;
using payload_budget::CH_VOC;
using payload_budget::eu868_max_mac_payload;
using payload_budget::frmpayload;

// Minimal model of the Cayenne "add" behaviour with the budget armed: each add
// either fits and advances the cursor, or is skipped (LPP_ERROR_OVERFLOW) exactly
// like WisCayenne with setMaxSize(budget) — and like the driver code, a single
// sensor's channels are added in a fixed internal order.
struct Budget
{
	uint16_t budget;
	uint16_t cursor = 0;
	bool overflow = false;

	Budget(uint16_t b) : budget(b) {}

	uint16_t add(uint8_t size)
	{
		if (cursor + size > budget)
		{
			overflow = true;
			return 0;
		}
		cursor += size;
		return cursor;
	}
};

// --- EU868 table ------------------------------------------------------------------

static int test_eu868()
{
	CHECK(eu868_max_mac_payload(0) == 51);
	CHECK(eu868_max_mac_payload(1) == 51);
	CHECK(eu868_max_mac_payload(2) == 51);
	CHECK(eu868_max_mac_payload(3) == 115);
	CHECK(eu868_max_mac_payload(4) == 115);
	CHECK(eu868_max_mac_payload(5) == 115);
	CHECK(eu868_max_mac_payload(6) == 242);
	CHECK(eu868_max_mac_payload(7) == 250);

	CHECK(frmpayload(0) == 43); // 51 - 8 (FHDR 7 + FPort 1)
	CHECK(frmpayload(1) == 43);
	CHECK(frmpayload(2) == 43);
	CHECK(frmpayload(3) == 107);
	CHECK(frmpayload(4) == 107);
	CHECK(frmpayload(5) == 107);
	CHECK(frmpayload(6) == 234);
	CHECK(frmpayload(7) == 242);

	// An out-of-range DR falls back to the DR0 worst case, never a permissive value
	CHECK(eu868_max_mac_payload(8) == 51 && frmpayload(8) == 43);
	CHECK(frmpayload(255) == 43);
	return 0;
}

// --- heartbeat accounting ----------------------------------------------------------

// Fully populated board, worst-case heartbeat: battery/config block (voltage ch 1 +
// version/threshold/duration/frequency/latch on ch 7-10, 13), switch ch 11, deferred
// motion ch 12, acc ch 6, and a RAK1906 (RAK1901/RAK1902 are suppressed when it is
// present) writing ch 2, 3, 4 and 14, plus a RAK1903 on ch 5.
//
// Wire sizes must match payload_budget.h: dig 3, voltage/temp/pressure/light/VOC 4,
// humidity 3, accelerometer 8.
static const unsigned FULL_HEARTBEAT =
	CH_VOLTAGE + 5 * CH_DIGITAL		// battery + config block (ch 1, 7-10, 13)
	+ CH_DIGITAL					// switch (ch 11)
	+ CH_DIGITAL					// deferred motion (ch 12)
	+ CH_ACCELEROMETER				// ch 6
	+ CH_TEMPERATURE + CH_HUMIDITY	// ch 2, 3
	+ CH_PRESSURE					// ch 4
	+ CH_LUMINOSITY					// ch 5
	+ CH_VOC;						// ch 14

static int test_heartbeat_accounting()
{
	// The known gap this file guards: 52 bytes does not fit DR0-DR2 (43), so the build
	// must be priority-ordered and degrade — and it never fits a smaller region's DR0
	// (US915 M=11) either, which is why payload_budget.h refuses other regions.
	CHECK(FULL_HEARTBEAT == 52);
	CHECK(FULL_HEARTBEAT > frmpayload(0));
	CHECK(FULL_HEARTBEAT <= frmpayload(3)); // the shipped fixed-DR3 policy fits untouched

	// Priority build at the DR0 budget, in the exact order the firmware assembles the
	// heartbeat (uplink.cpp) and reads the sensors (read_order in sensors.cpp):
	const uint16_t b = frmpayload(0);
	Budget t{b};
	CHECK(t.add(CH_VOLTAGE) > 0); // ch 1 battery voltage
	CHECK(t.add(CH_DIGITAL) > 0); // ch 7 version
	CHECK(t.add(CH_DIGITAL) > 0); // ch 8 threshold
	CHECK(t.add(CH_DIGITAL) > 0); // ch 9 duration
	CHECK(t.add(CH_DIGITAL) > 0); // ch 10 frequency
	CHECK(t.add(CH_DIGITAL) > 0); // ch 13 battery latch
	CHECK(t.add(CH_DIGITAL) > 0); // ch 11 switch
	CHECK(t.add(CH_DIGITAL) > 0); // ch 12 deferred motion
	CHECK(t.add(CH_ACCELEROMETER) > 0); // ch 6 XYZ (RAK1904 first in read_order)
	CHECK(t.add(CH_TEMPERATURE) > 0); // ch 2 (RAK1906 adds T then H)
	CHECK(t.add(CH_HUMIDITY) > 0); // ch 3
	// 43-byte budget is now exhausted: the telemetry tail must be skipped, not the frame
	CHECK(t.cursor == 40);
	CHECK(t.add(CH_PRESSURE) == 0); // ch 4
	CHECK(t.add(CH_LUMINOSITY) == 0); // ch 5
	CHECK(t.add(CH_VOC) == 0); // ch 14
	CHECK(t.overflow);
	CHECK(t.cursor == b - 3); // 40 bytes on the wire at DR0

	// Every channel the alarm rules read survives a DR0 heartbeat:
	// battery voltage (battery rules), config trio (settings read-back),
	// battery latch, switch (circuit), motion alert, accelerometer (tilt),
	// temperature and humidity (temp rules). Only pressure/light/gas are lost.
	return 0;
}

// --- event frames ------------------------------------------------------------------

static int test_event_frames()
{
	// Motion alert (send_acc_alert): motion bit, supply state, XYZ, fast T/H.
	const uint16_t b = frmpayload(0);
	Budget m{b};
	CHECK(m.add(CH_DIGITAL) > 0); // ch 12 motion
	CHECK(m.add(CH_VOLTAGE) > 0); // ch 1 battery voltage
	CHECK(m.add(CH_DIGITAL) > 0); // ch 13 battery latch
	CHECK(m.add(CH_ACCELEROMETER) > 0); // ch 6 XYZ
	CHECK(m.add(CH_TEMPERATURE) > 0);
	CHECK(m.add(CH_HUMIDITY) > 0);
	CHECK(!m.overflow);
	CHECK(m.cursor == 25);

	// Switch event (handle_gpio): switch bit + supply state. Tiny at any DR.
	Budget s{b};
	CHECK(s.add(CH_DIGITAL) > 0 && s.add(CH_VOLTAGE) > 0 && s.add(CH_DIGITAL) > 0);
	CHECK(!s.overflow && s.cursor == 10);

	// A minimal deployment (acc + battery/config, like the field nodes) always fits
	// the DR0 budget even with the switch and a deferred motion pulse.
	Budget min{b};
	CHECK(min.add(CH_VOLTAGE) > 0); // ch 1
	for (int i = 0; i < 5; i++)	   // ch 7-10, 13 config block
		CHECK(min.add(CH_DIGITAL) > 0);
	CHECK(min.add(CH_DIGITAL) > 0); // ch 11 switch
	CHECK(min.add(CH_DIGITAL) > 0); // ch 12 deferred motion
	CHECK(min.add(CH_ACCELEROMETER) > 0); // ch 6
	CHECK(min.add(CH_TEMPERATURE) > 0); // ch 2
	CHECK(min.add(CH_HUMIDITY) > 0); // ch 3
	CHECK(!min.overflow && min.cursor == 40);
	return 0;
}

int main()
{
	if (test_eu868() || test_heartbeat_accounting() || test_event_frames())
	{
		return 1;
	}
	std::printf("OK: %d checks passed\n", checks);
	return 0;
}
