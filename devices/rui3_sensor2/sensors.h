/**
 * @file sensors.h
 * @brief Sensor registry and driver entry points for the WisBlock modules.
 *
 * Adding a sensor = write its driver (init/read/startup/shutdown), add one row
 * to the table in sensors.cpp. Nothing else in the firmware needs to change.
 */
#pragma once

#include <Arduino.h>

enum SensorId : uint8_t
{
	SENSOR_RAK1901, // SHTC3 temperature / humidity
	SENSOR_RAK1902, // LPS22HB/LPS35HW pressure
	SENSOR_RAK1903, // OPT3001 light
	SENSOR_RAK1904, // LIS3DH accelerometer (also the motion interrupt source)
	SENSOR_RAK1906, // BME680 environmental (temp/humidity/pressure/gas)
	SENSOR_COUNT
};

/** True if the sensor was found and initialised at boot. */
bool sensor_present(SensorId id);

/** Log every device on the I2C bus (debug builds). */
void sensors_scan_i2c(void);

/** Probe + initialise all sensors, emit +EVT:RAKxxxx lines, put idle ones to sleep. */
void sensors_init_all(void);

/** Wake present sensors and wait SENSOR_WARMUP_TIME. */
void sensors_wakeup(void);

/** Put present sensors back to sleep. */
void sensors_shutdown(void);

/** Append every present sensor's reading to g_solution_data (heartbeat). */
void sensors_read_all(void);

/**
 * Append only the fast sensors to g_solution_data (motion alert extras:
 * temp/humidity). Slow sensors (BME680 gas heater) are left out on purpose.
 * The accelerometer is added by send_acc_alert() itself.
 */
void sensors_read_for_alert(void);

// ---------------------------------------------------------------------------
// Driver entry points (one file per module)
// ---------------------------------------------------------------------------
bool init_rak1901(void);
void read_rak1901(void);
void startup_rak1901(void);
void shutdown_rak1901(void);

bool init_rak1902(void);
void read_rak1902(void);
void startup_rak1902(void);
void shutdown_rak1902(void);

bool init_rak1903(void);
void read_rak1903(void);
void startup_rak1903(void);
void shutdown_rak1903(void);

bool init_rak1906(void);
void read_rak1906(void);
void startup_rak1906(void);
void shutdown_rak1906(void);

// RAK1904 accelerometer
bool rak1904_writeRegister(uint8_t chip_reg, uint8_t dataToWrite);
bool rak1904_readRegister(uint8_t *outputPointer, uint8_t chip_reg);
bool init_rak1904(void);
bool setup_rak1904(void);
void int_callback_rak1904(void);
void send_acc_alert(void *unused);
void clear_int_rak1904(void);
void read_rak1904(void);
void startup_rak1904(void);
void shutdown_rak1904(void);
