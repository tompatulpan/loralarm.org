// Host tests for the pure policy headers. Build/run: make -C tests/host
#include <cassert>
#include <cstdio>

#include "../../battery_policy.h"
#include "../../downlink_parse.h"
#include "../../send_policy.h"

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

// --- battery_policy ---------------------------------------------------------

static int test_battery()
{
	using namespace battery_policy;
	const uint16_t crit = 2500;
	const float margin = 0.05f;

	// invalid readings are never critical and never "recovered"
	CHECK(!valid(0.0f));
	CHECK(!valid(0.5f));
	CHECK(valid(0.51f));
	CHECK(!is_critical(0.0f, crit));
	CHECK(!recovered(0.0f, crit, margin));
	// Mode B sense-fault voltage (BATT_SENSE_FAULT_V = 0.6 in platform_stm32.cpp) must
	// count as a valid, critical reading and never as recovered.
	CHECK(valid(0.6f) && is_critical(0.6f, crit) && !recovered(0.6f, crit, margin));

	// critical at or below the threshold only
	CHECK(is_critical(2.5f, crit));
	CHECK(is_critical(2.3f, crit));
	CHECK(!is_critical(2.51f, crit));

	// guard disabled (BATCRIT=0): nothing is critical, everything valid is recovered
	CHECK(!is_critical(1.0f, 0));
	CHECK(recovered(1.0f, 0, margin));
	CHECK(!recovered(0.0f, 0, margin));

	// hysteresis: must exceed critical + margin
	CHECK(!recovered(2.5f, crit, margin));
	CHECK(!recovered(2.55f, crit, margin));
	CHECK(recovered(2.56f, crit, margin));
	CHECK(resume_v(crit, margin) > critical_v(crit));

	// a reading between critical and resume is neither critical nor recovered
	CHECK(!is_critical(2.52f, crit) && !recovered(2.52f, crit, margin));
	return 0;
}

// --- send_policy ------------------------------------------------------------

static int test_send_policy()
{
	using namespace send_policy;
	ReportedConfig cfg = {100, 8, 1, 2};

	// nothing due: not first boot, counter below interval, config unchanged
	CHECK(!should_send_battery_and_config(0, 3, false, cfg, cfg));
	CHECK(!should_send_battery_and_config(2, 3, false, cfg, cfg));
	// scheduled
	CHECK(should_send_battery_and_config(3, 3, false, cfg, cfg));
	// interval 1 = every heartbeat
	CHECK(should_send_battery_and_config(1, 1, false, cfg, cfg));
	// first boot
	CHECK(should_send_battery_and_config(0, 3, true, cfg, cfg));
	// each reported value changing triggers a send
	ReportedConfig changed = cfg;
	changed.version = 101;
	CHECK(should_send_battery_and_config(0, 3, false, changed, cfg));
	changed = cfg;
	changed.threshold = 9;
	CHECK(should_send_battery_and_config(0, 3, false, changed, cfg));
	changed = cfg;
	changed.duration = 2;
	CHECK(should_send_battery_and_config(0, 3, false, changed, cfg));
	changed = cfg;
	changed.frequency = 3;
	CHECK(should_send_battery_and_config(0, 3, false, changed, cfg));
	return 0;
}

// --- downlink_parse ---------------------------------------------------------

static DownlinkResult dl(uint8_t port, const char *digits, AccParams &p, int len = -1)
{
	if (len < 0)
	{
		len = 0;
		while (digits[len])
		{
			len++;
		}
	}
	return downlink::apply(port, (const uint8_t *)digits, (uint16_t)len, p);
}

static int test_downlink()
{
	const AccParams start = {8, 2, 1};
	AccParams p = start;

	// valid values per port, only the addressed field changes
	CHECK(dl(5, "016", p) == DL_APPLIED && p.threshold == 16 && p.frequency == 2 && p.duration == 1);
	CHECK(dl(6, "005", p) == DL_APPLIED && p.frequency == 5 && p.threshold == 16);
	CHECK(dl(7, "000", p) == DL_APPLIED && p.duration == 0);
	// only the first three digits count
	p = start;
	CHECK(dl(5, "0129", p) == DL_APPLIED && p.threshold == 12);

	// range limits (inclusive)
	p = start;
	CHECK(dl(5, "001", p) == DL_APPLIED && p.threshold == 1);
	CHECK(dl(5, "127", p) == DL_APPLIED && p.threshold == 127);
	CHECK(dl(6, "001", p) == DL_APPLIED && p.frequency == 1);
	CHECK(dl(6, "007", p) == DL_APPLIED && p.frequency == 7);
	CHECK(dl(7, "127", p) == DL_APPLIED && p.duration == 127);

	// rejected values leave the params untouched
	p = start;
	CHECK(dl(5, "000", p) == DL_OUT_OF_RANGE);
	CHECK(dl(5, "128", p) == DL_OUT_OF_RANGE);
	CHECK(dl(6, "000", p) == DL_OUT_OF_RANGE);
	CHECK(dl(6, "008", p) == DL_OUT_OF_RANGE);
	CHECK(dl(7, "128", p) == DL_OUT_OF_RANGE);
	// regression: values above 255 used to wrap in a uint8_t ("300" -> 44, accepted)
	CHECK(dl(5, "300", p) == DL_OUT_OF_RANGE);
	CHECK(dl(7, "999", p) == DL_OUT_OF_RANGE);
	CHECK(p.threshold == start.threshold && p.frequency == start.frequency && p.duration == start.duration);

	// malformed payloads
	CHECK(dl(5, "", p) == DL_EMPTY);
	CHECK(dl(5, "12", p) == DL_TOO_SHORT);
	CHECK(dl(5, "1a2", p) == DL_BAD_DIGIT);
	CHECK(dl(5, " 12", p) == DL_BAD_DIGIT);
	CHECK(p.threshold == start.threshold);

	// non-ACC ports
	CHECK(dl(2, "016", p) == DL_UNKNOWN_PORT);
	CHECK(dl(8, "016", p) == DL_UNKNOWN_PORT);
	CHECK(!downlink::is_acc_port(4) && downlink::is_acc_port(5) && downlink::is_acc_port(7) && !downlink::is_acc_port(8));
	CHECK(p.threshold == start.threshold && p.frequency == start.frequency && p.duration == start.duration);
	return 0;
}

int main()
{
	if (test_battery() || test_send_policy() || test_downlink())
	{
		return 1;
	}
	std::printf("OK: %d checks passed\n", checks);
	return 0;
}
