/**
 * @file platform_stm32.cpp
 * @brief Platform implementation for RAK3172 / RAK3172-SiP (STM32WLE5).
 *
 * Compiled only for STM32WLE5 targets. All MCU-specific code for this
 * platform lives here; the rest of the application is device-agnostic.
 *
 * Battery sensing — Mode A (VBAT == VDDA, no LDO between cell and MCU):
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
 *   Power: the Vrefint path is the ONLY analogRead() that clears ADC ADEN on
 *   exit. Any GPIO pin analogRead() leaves ADEN=1, blocking Stop 2 entry
 *   (~45 uA). Do NOT add GPIO ADC reads here.
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

// Boot-time battery critical threshold (mV). Keep margin above 2.5 V so a
// radio/sensor current pulse cannot pull a marginal direct supply into BOR.
static const uint16_t BATT_CRITICAL_MV = 2800;

// ── API implementation ────────────────────────────────────────────────────────

void platform_init(void)
{
    // analogReadResolution is set to 12 inside platform_battery_get() before
    // each read and restored to 10 after. No need to touch it here; calling
    // it without a subsequent read could leave the ADC in an unexpected state.
    // No Serial6 (no BLE/AT UART on STM32WLE5).
    // No WB_IO2 (sensor power rail not present on RAK3172 eval / RAK19009).
}

float platform_battery_get(void)
{
    uint16_t vrefint_cal = *VREFINT_CAL_ADDR_PTR;
    analogReadResolution(12);
    analogRead(254);                              // warm-up: enable VREFEN, let Vrefint settle
    uint16_t vref_raw = (uint16_t)analogRead(254); // UDRV_ADC_CHANNEL_VREFINT
    analogReadResolution(10);                     // restore RUI3 default

    MYLOG("BATT", "STM32 Vrefint: raw=%u (cal=%u)", vref_raw, vrefint_cal);

    if (vref_raw == 0 || vref_raw > 4095) {
        MYLOG("BATT", "Vrefint OOB — monitoring disabled");
        return 0.0f;  // 0.0 V skips boot guard (>0.5f) and runtime check
    }

    float vdda_raw  = (VREFINT_CAL_VDDA_MV * (float)vrefint_cal) / (vref_raw * 1000.0f);
    float vdda_corr = (vdda_raw * BATT_CAL_GAIN) + BATT_CAL_OFFSET;

    MYLOG("BATT", "STM32: raw=%.3fV gain=%.3f off=%.3f -> %.3fV",
          vdda_raw, BATT_CAL_GAIN, BATT_CAL_OFFSET, vdda_corr);

    return vdda_corr;
}

void platform_pre_sleep(void)
{
    // Force ADC off before Stop 2 entry. The Vrefint read path clears ADEN,
    // but the RUI3 LoRaWAN stack may call api.system.bat.get() for ADR
    // battery level reporting, re-enabling ADC (ADEN=1) after our read.
    // Stop 2 is blocked while ADEN=1 (RM0461 §21.3.1).
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
