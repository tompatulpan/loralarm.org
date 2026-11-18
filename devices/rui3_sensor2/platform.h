/**
 * @file platform.h
 * @brief Hardware abstraction API for RAK3172 (STM32WLE5) and RAK4631 (nRF52840).
 *
 * All MCU-specific code lives in platform_stm32.cpp and platform_nrf52.cpp.
 * The rest of the application uses only these functions — no #ifdef for MCU
 * variant anywhere outside the platform files.
 */
#pragma once

#include <Arduino.h>

/**
 * @brief Initialize platform-specific peripherals.
 *
 * Call at the top of setup(), immediately after Serial.begin().
 * Must precede Wire.begin() and any MYLOG calls.
 *
 * nRF52840:  Serial6.begin (AT/BLE UART), analogReadResolution(12),
 *            pinMode(WB_A0, INPUT), WB_IO2 sensor power rail HIGH.
 * STM32WLE5: analogReadResolution(12) only.
 */
void platform_init(void);

/**
 * @brief Read battery voltage in volts.
 *
 * STM32WLE5: layout is a per-device setting (ATC+BATMODE, stored in flash).
 *   Mode A (default): Vrefint factory calibration at pin 254 -> true VDDA/VBAT;
 *   correct when the cell feeds VDD directly. The Vrefint path is the only
 *   analogRead() that leaves the ADC disabled on exit.
 *   Mode B: external divider on BATT_SENSE_PIN, ADC scale from Vrefint; for
 *   boards where an LDO sits between the cell and the module. The GPIO read
 *   leaves ADC ADEN set; platform_pre_sleep() clears it before sleep entry.
 * nRF52840 (always divider): SAADC + WB_A0 voltage divider -> true VBAT.
 *   The SAADC is left enabled-idle after the read; see platform_nrf52.cpp for why it is
 *   not disabled by direct register writes (nrfx driver state corruption).
 *
 * @return Battery voltage in V, or 0.0 if ADC read failed / monitoring disabled.
 */
float platform_battery_get(void);

/**
 * @brief Prepare for MCU sleep — call from loop() before api.system.sleep.all().
 *
 * Currently a no-op on both MCUs. On nRF52840 it deliberately does NOT write
 * NRF_SAADC->ENABLE: direct register writes are unsafe while the nrfx_saadc driver is
 * initialised (see platform_nrf52.cpp). Kept as the single hook for per-MCU sleep prep.
 */
void platform_pre_sleep(void);

/**
 * @brief Start BLE advertising — call from setup() after api.system.lpm.set(1).
 *
 * nRF52840: calls api.ble.advertise.start(30). Without this the nRF52840 SoftDevice
 *           stays half-initialised and draws ~440 uA continuously. The RUI3 build
 *           stops advertising internally after 30 s; sleep current drops to ~25 uA
 *           after the window expires. Measure sleep current AFTER 30 s.
 * STM32WLE5: no-op (no BLE hardware).
 */
void platform_ble_start(void);
void platform_ble_rearm(void);

/**
 * @brief Boot-time battery critical threshold in mV.
 *
 * Used before flash settings are loaded (get_at_setting has not run yet).
 * At runtime the value from flash (ATC+BATCRIT) is used instead.
 *
 * STM32WLE5: 2500 mV
 * nRF52840:  2800 mV
 */
uint16_t platform_batt_critical_default_mv(void);
