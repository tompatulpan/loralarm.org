/**
 * @file platform_stm32.cpp
 * @brief Platform implementation for RAK3172 / RAK3172-SiP (STM32WLE5).
 *
 * Compiled only for STM32WLE5 targets. All MCU-specific code for this
 * platform lives here; the rest of the application is device-agnostic.
 *
 * Battery sensing — RAK3172 ships in two supply layouts; the mode is a
 * per-device setting (ATC+BATMODE, flash-backed, default = Mode A):
 *
 *   Mode A (0, default) — VBAT == VDDA, no LDO between cell and MCU:
 *   The STM32WLE5 internal Vrefint bandgap (~1.212 V) measured at pin 254
 *   (UDRV_ADC_CHANNEL_VREFINT) against the factory calibration word at
 *   0x1FFF75AA gives true VDDA. VDDA equals the battery voltage directly.
 *
 *   WHY pin 254 and not AVREF:
 *     On WisDuoRAK3172EvaluationBoard AVREF = PNUM_ANALOG_BASE(0xC0)
 *     + NUM_ANALOG_INPUTS(2) + 1 = 195. uhal_adc_read has no handler for 195
 *     -> UDRV_NACK, *value is uninitialised stack memory (=8192).
 *     Pin 254 = UDRV_ADC_CHANNEL_VREFINT IS handled; HAL_ADC_ConfigChannel
 *     auto-enables ADC_CCR_VREFEN. Factory cal is 12-bit, so we read at 12-bit.
 *
 *   Mode B (1) — battery behind an LDO. VDDA is then the regulator output and
 *   says nothing about the cell, so Mode A cannot see a dying battery. An
 *   external high-impedance divider from the cell (>= 10 MOhm total, 100 nF
 *   at the pin for S&H settling; ~0.4 uA drain at 4 V) feeds BATT_SENSE_PIN
 *   (config.h). The ADC full scale is VDDA, so Mode B measures VDDA via
 *   Vrefint first, then the divider pin:
 *     VBAT = raw/4095 * VDDA * BATT_SENSE_DIVIDER
 *   BATT_SENSE_DIVIDER lumps the resistor ratio and pin tolerances; calibrate
 *   as for BATT_CAL_GAIN: new = current * (multimeter_V / code_V). An
 *   uncalibrated 1.0 reads LOW with a real divider -> the low-battery latch
 *   trips and TX stays suppressed until a calibrated value is compiled in
 *   (no alarm uplink is sent). An unwired/shorted/saturated pin reports
 *   BATT_SENSE_FAULT_V so it latches too, never silently disabling supervision.
 *
 *   Power: the Vrefint path is the only analogRead() that clears ADC ADEN on
 *   exit. The Mode B GPIO read leaves ADEN=1; platform_pre_sleep() clears it
 *   with ADC_CR_ADDIS before Stop 2 entry. The ~45 uA penalty applies only
 *   between a Mode B read and the next sleep, not during sleep.
 */

// Guard on the CMSIS device define set by the RUI3 STM32 board package.
// The build.extra_flags in boards.txt define -DSTM32WLE5xx for RAK3172.
#if defined(STM32WLE5xx)

#include "app.h"
#include "platform.h"

// ── Private constants ─────────────────────────────────────────────────────────
// Vrefint factory calibration: 12-bit ADC reading of Vrefint at VDDA=3.0 V,
// T=30 °C (STM32WLE5 RM0461 §21.4.4).
// NOTE: VREFINT_CAL_ADDR is already defined as a macro in stm32wlxx_ll_adc.h
// so we use a different name here to avoid a preprocessor collision.
static const uint16_t* const VREFINT_CAL_ADDR_PTR = (const uint16_t*)0x1FFF75AAU;
static const uint32_t        VREFINT_CAL_VDDA_MV  = 3000UL;

// Board-level gain/offset correction applied after the factory formula.
// Calibrate by comparing platform_battery_get() against a multimeter.
// new_gain = current_gain * (multimeter_V / code_V)
static const float BATT_CAL_GAIN   = 1.102f;
static const float BATT_CAL_OFFSET = 0.022f;

// Boot-time battery critical threshold (mV). 2.5 V keeps ~0.2 V above the
// STM32WLE5 brown-out reset while staying clear of the +/-0.1 V error of the
// Vrefint measurement on a healthy 3 V cell (v30's 2.8 V latched healthy cells).
static const uint16_t BATT_CRITICAL_MV = 2500;

// Vrefint samples per reading; the median rejects a single wake-up outlier.
static const uint8_t BATT_SAMPLES = 5;

// Mode B: lumped divider ratio for the external cell divider on BATT_SENSE_PIN
// (LDO boards, ATC+BATMODE=1). Absorbs the resistor ratio and pin tolerances.
// Calibration: new_divider = current_divider * (multimeter_V / code_V).
// Default 1.0 = UNCALIBRATED: with a real divider it reads LOW, so the low-battery
// latch trips and TX is suppressed (device goes quiet; no alarm uplink is sent)
// until a calibrated value is compiled in. Never a missed low battery.
static const float BATT_SENSE_DIVIDER = 1.0f;

// Mode B: voltage reported when the divider pin gives no valid sample (unwired,
// shorted or saturated). Must be above the policy's invalid threshold (0.5 V) and
// below any allowed critical threshold so the latch trips instead of silently
// disabling supervision.
static const float BATT_SENSE_FAULT_V = 0.6f;

// ── API implementation ────────────────────────────────────────────────────────

void platform_init(void)
{
    // analogReadResolution is set to 12 inside platform_battery_get() before
    // each read and restored to 10 after. No need to touch it here; calling
    // it without a subsequent read could leave the ADC in an unexpected state.
    // No Serial6 (no BLE/AT UART on STM32WLE5).
    // No WB_IO2 (sensor power rail not present on RAK3172 eval / RAK19009).

    // Park interrupt-capable WisBlock lines that no module drives. An
    // unpopulated slot leaves them floating: a floating STM32WLE5 input buffer
    // near mid-rail draws current and can oscillate (bench: 350 uA bursts at
    // ~2 Hz with the RAK1904 absent, clean ~25 uA with it present).
    // A present module overrides the parking: init_rak1904() reconfigures
    // ACC_INT_PIN and init_gpio() reconfigures SW_INT_PIN after this runs.
    pinMode(ACC_INT_PIN, INPUT_PULLDOWN); // WB_IO5: RAK1904 INT1 (Slot D)
    pinMode(SW_INT_PIN, INPUT_PULLDOWN);  // WB_IO1: switch input (SWITCH_ENABLED=0)
}

/**
 * @brief Median VDDA in volts from the internal Vrefint reference.
 *
 * Raw factory-formula value, no board gain/offset correction. Mode A treats the
 * median as the battery; Mode B uses it as the ADC full-scale voltage.
 *
 * @return Median VDDA in V, or 0.0 if no valid sample was taken.
 */
static float vdda_read_v(void)
{
    uint16_t vrefint_cal = *VREFINT_CAL_ADDR_PTR;
    analogReadResolution(12);
    analogRead(254);                              // warm-up: enable VREFEN, let Vrefint settle

    // Collect valid VDDA samples (insertion-sorted) and take the median.
    float vdda[BATT_SAMPLES];
    uint8_t n = 0;
    for (uint8_t i = 0; i < BATT_SAMPLES; i++) {
        if (i > 0) {
            delay(2);
        }
        uint16_t vref_raw = (uint16_t)analogRead(254); // UDRV_ADC_CHANNEL_VREFINT
        if (vref_raw == 0 || vref_raw > 4095) {
            continue;
        }
        float v = (VREFINT_CAL_VDDA_MV * (float)vrefint_cal) / (vref_raw * 1000.0f);
        uint8_t j = n++;
        while (j > 0 && vdda[j - 1] > v) {
            vdda[j] = vdda[j - 1];
            j--;
        }
        vdda[j] = v;
    }
    analogReadResolution(10);                     // restore RUI3 default

    if (n == 0) {
        MYLOG("BATT", "Vrefint OOB — monitoring disabled");
        return 0.0f;  // 0.0 V skips boot guard (>0.5f) and runtime check
    }

    MYLOG("BATT", "Vrefint %u/%u samples, median raw=%.3fV (min %.3f max %.3f)",
          n, BATT_SAMPLES, vdda[n / 2], vdda[0], vdda[n - 1]);
    return vdda[n / 2];
}

/**
 * @brief Mode B battery read: external divider on BATT_SENSE_PIN (ATC+BATMODE=1).
 *
 * The ADC full scale is VDDA (the regulator output on an LDO board), not a
 * fixed reference, so the scale comes from the same Vrefint method Mode A uses.
 *
 * @return Battery voltage in V; 0.0 if the Vrefint scale could not be read, or
 *         BATT_SENSE_FAULT_V if the divider pin gave no valid sample.
 */
static float battery_read_divider_v(void)
{
    float vdda = vdda_read_v();
    if (vdda <= 0.0f) {
        return 0.0f;  // reason logged by vdda_read_v
    }

    analogReadResolution(12);
    analogRead(BATT_SENSE_PIN);  // warm-up: charge S&H capacitor

    float samples[BATT_SAMPLES];
    uint8_t n = 0;
    for (uint8_t i = 0; i < BATT_SAMPLES; i++) {
        delay(2);  // allow S&H capacitor to fully recharge between samples
        uint16_t raw = (uint16_t)analogRead(BATT_SENSE_PIN);
        // 0 / full-scale = floating pin (no divider wired) or shorted divider
        if (raw == 0 || raw >= 4095) {
            continue;
        }
        float v = (raw / 4095.0f) * vdda * BATT_SENSE_DIVIDER;
        uint8_t j = n++;
        while (j > 0 && samples[j - 1] > v) {
            samples[j] = samples[j - 1];
            j--;
        }
        samples[j] = v;
    }
    analogReadResolution(10);                     // restore RUI3 default

    if (n == 0) {
        // Unlike Mode A's Vrefint failure, do NOT return 0.0 here: that would silently disable
        // supervision on a board with no/broken divider. Report a valid-but-critical voltage
        // so the latch trips and the fault is visible.
        MYLOG("BATT", "Divider OOB — reporting %.2fV (sense fault)", BATT_SENSE_FAULT_V);
        return BATT_SENSE_FAULT_V;
    }

    float median = samples[n / 2];
    MYLOG("BATT", "STM32 Mode B: %u/%u samples, VDDA=%.3fV div=%.3f -> %.3fV",
          n, BATT_SAMPLES, vdda, BATT_SENSE_DIVIDER, median);
    return median;
}

float platform_battery_get(void)
{
    // Mode is a per-device setting (ATC+BATMODE); the Mode B divider ratio is not.
    // battery_boot_guard() may run before get_at_setting() loads the mode from
    // flash; it then measures Mode A, which is harmless — behind an LDO the
    // regulated VDDA passes the guard, and the runtime latch uses the saved mode.
    if (custom_parameters.batt_sense_mode == 1)
    {
        return battery_read_divider_v();
    }

    float vdda_raw  = vdda_read_v();
    if (vdda_raw <= 0.0f)
    {
        return 0.0f;
    }

    float vdda_corr = (vdda_raw * BATT_CAL_GAIN) + BATT_CAL_OFFSET;
    MYLOG("BATT", "STM32 Mode A: gain=%.3f off=%.3f -> %.3fV",
          BATT_CAL_GAIN, BATT_CAL_OFFSET, vdda_corr);
    return vdda_corr;
}

void platform_pre_sleep(void)
{
    // Force ADC off before Stop 2 entry. The Vrefint read path clears ADEN,
    // but the RUI3 LoRaWAN stack may call api.system.bat.get() for ADR
    // battery level reporting, re-enabling ADC (ADEN=1) after our read — and
    // the Mode B divider read does the same. Stop 2 is blocked while ADEN=1
    // (RM0461 §21.3.1).
    // ADC_CR_ADDIS (bit 1) requests disable; hardware clears ADEN (bit 0).
    if (ADC->CR & ADC_CR_ADEN) {
        ADC->CR |= ADC_CR_ADDIS;
        while (ADC->CR & ADC_CR_ADEN) {}  // wait for hardware acknowledgment
    }
}

void platform_ble_start(void)
{
    // No-op: RAK3172 has no BLE hardware.
}

void platform_ble_rearm(void)
{
    // No-op: RAK3172 has no BLE hardware.
}

uint16_t platform_batt_critical_default_mv(void)
{
    return BATT_CRITICAL_MV;
}

#endif // STM32WLE5xx
