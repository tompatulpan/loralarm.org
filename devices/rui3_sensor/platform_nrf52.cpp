/**
 * @file platform_nrf52.cpp
 * @brief Platform implementation for RAK4631 / RAK4631-R (nRF52840).
 *
 * Compiled only for nRF52840 targets. All MCU-specific code for this
 * platform lives here; the rest of the application is device-agnostic.
 *
 * Battery sensing — Mode B (VBAT > VDDA, LDO present on RAK19007/RAK19003):
 *   The SAADC reads the WB_A0 voltage divider pin to measure true VBAT.
 *   nRF52840 SAADC: internal reference 0.6 V, gain 1/6 -> full scale = 3.6 V.
 *   BAT_ADC_DIVIDER absorbs both the hardware resistor ratio and the SAADC
 *   reference offset (empirically calibrated; see constant comment below).
 *
 * Power:
 *   analogRead() on nRF52840 leaves SAADC ENABLE=1. Unlike STM32WLE5, this
 *   does not block sleep entry, but an enabled-idle SAADC draws ~45 uA during
 *   System ON sleep. platform_battery_get() and platform_pre_sleep() both
 *   explicitly clear ENABLE so the device sleeps at ~25 uA.
 *
 * BLE:
 *   api.ble.advertise.start(30) is required to complete the nRF52840 SoftDevice
 *   init cycle. Without it the SoftDevice stays half-initialised and draws
 *   ~440 uA continuously. api.ble.advertise.stop() is broken in the current
 *   RUI3 build (calling it causes the same ~440 uA state). The 30 s RUI3
 *   internal timeout stops advertising cleanly. Sleep current is ~45 uA during
 *   the 30 s BLE window; measure AFTER it expires.
 */

// Guard on the CMSIS device define set by the nRF52840 board package.
// NRF52840_XXAA is always defined by the nRF52840 CMSIS headers and is never
// present in the STM32WLE5 toolchain.
#if defined(NRF52840_XXAA)

#include "app.h"
#include "platform.h"

// ── Private constants ─────────────────────────────────────────────────────────
// nRF52840 SAADC internal reference: 0.6 V, gain 1/6 -> full scale 3.6 V.
static const float SAADC_VREF_V = 3.6f;

// Voltage divider calibration for RAK4631-R + RAK19003/RAK19007.
// BAT_ADC_DIVIDER absorbs both the resistor ratio and the SAADC reference offset
// (effective full-scale ~2.73 V in RUI3, not the nominal 3.6 V).
// Calibration procedure: new_divider = current_divider * (code_reading / multimeter_V)
// Last cal: multimeter=4.07 V, code=3.79 V @ 0.63 -> 0.63*(3.79/4.07) = 0.587
static const float BAT_ADC_DIVIDER = 0.587f;

// WB_A0 pin for the voltage divider input.
static const uint8_t BAT_ADC_PIN = WB_A0;

// Number of SAADC samples to average (with 2 ms inter-sample delay).
static const uint8_t BATTERY_SAMPLES = 3;

// Boot-time battery critical threshold (mV). 2.8 V = deeply discharged LiPo.
static const uint16_t BATT_CRITICAL_MV = 2800;

// NOTE: We do NOT write NRF_SAADC->ENABLE directly. The nRF52 BSP uses the
// nrfx_saadc driver internally; bypassing it with a direct register write
// corrupts nrfx's internal state. On the next SAADC use (e.g. the RUI3 stack's
// internal api.system.bat.get() for ADR battery reporting), nrfx detects the
// hardware/driver state mismatch and faults → SoftDevice error handler → 430µA
// spin. The ~50µA SAADC-idle sleep current is the unavoidable floor given
// the RUI3 stack's use of SAADC.

// ── API implementation ────────────────────────────────────────────────────────

void platform_init(void)
{
    // Serial6 is the BLE/AT UART on RAK4631-R. Must be initialised before any
    // MYLOG call because the nRF52 MYLOG macro writes to Serial6.printf().
    Serial6.begin(115200, RAK_AT_MODE);

    // Full 12-bit ADC range for accurate battery reads.
    analogReadResolution(12);

    // Configure WB_A0 as input for the voltage divider.
    pinMode(BAT_ADC_PIN, INPUT);

    // Enable WisBlock sensor power rail (RAK19007 / RAK19003).
    // Not present on RAK3172 eval board or RAK19009.
    pinMode(WB_IO2, OUTPUT);
    digitalWrite(WB_IO2, HIGH);
}

float platform_battery_get(void)
{
#if MY_DEBUG > 0
    // Debug-only comparison read. api.system.bat.get() triggers an extra SAADC
    // acquisition — skip it in production to avoid the redundant enable/disable cycle.
    float rui3_bat = api.system.bat.get();
    MYLOG("BATT", "nRF52 ModeB: SAADC_VREF=%.1fV DIV=%.3f rui3=%.3fV",
          SAADC_VREF_V, BAT_ADC_DIVIDER, rui3_bat);
#endif

    // Set 12-bit resolution immediately before reads. The RUI3 stack resets
    // analogReadResolution internally (e.g. during api.system.bat.get() for ADR
    // battery reporting), so setting it once in platform_init() does not survive
    // to subsequent calls.
    analogReadResolution(12);
    analogRead(BAT_ADC_PIN);  // warm-up: charge S&H capacitor
    float sum = 0.0f;
    for (uint8_t i = 0; i < BATTERY_SAMPLES; i++) {
        delay(2);  // allow S&H capacitor to fully recharge between samples
        uint16_t raw = (uint16_t)analogRead(BAT_ADC_PIN);
        float vbat = (raw / 4095.0f) * SAADC_VREF_V / BAT_ADC_DIVIDER;
        MYLOG("BATT", "  sample[%u]: raw=%u  VBAT=%.3fV", i, raw, vbat);
        sum += vbat;
    }

    analogReadResolution(10);  // restore RUI3 default

    float result = sum / BATTERY_SAMPLES;

#if MY_DEBUG > 0
    MYLOG("BATT", "nRF52 avg=%.3fV rui3=%.3fV  cal_check: DIV=%.3f",
          result, rui3_bat, (result / rui3_bat) * BAT_ADC_DIVIDER);
#else
    MYLOG("BATT", "nRF52 VBAT=%.3fV", result);
#endif

    return result;
}

void platform_pre_sleep(void)
{
    // No-op on nRF52840. Direct NRF_SAADC register writes are unsafe while
    // the nrfx_saadc driver is initialised — see note above.
}

void platform_ble_start(void)
{
    // Required: completing the SoftDevice init cycle. Without this call the
    // nRF52840 draws ~440 uA continuously. Do NOT call advertise.stop() —
    // it is broken in the current RUI3 build. The 30 s RUI3 internal timeout
    // stops advertising cleanly (~45 uA during the window).
    api.ble.advertise.start(30);
}

void platform_ble_rearm(void)
{
    // After each LoRaWAN TX, the RUI3 stack restarts BLE advertising without a
    // timeout, causing ~425 uA indefinitely. Re-arm with a 1 s timeout so the
    // SoftDevice stops advertising after 1 s and returns to ~35 uA sleep.
    // 1 s at ~70 uA is negligible for battery life.
    api.ble.advertise.start(1);
}

uint16_t platform_batt_critical_default_mv(void)
{
    return BATT_CRITICAL_MV;
}

#endif // NRF52840_XXAA
