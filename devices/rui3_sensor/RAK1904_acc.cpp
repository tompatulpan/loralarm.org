/*
Based on https://github.com/RAKWireless/RUI3-Best-Practice, by Bernd Giesecke (bernd.giesecke@rakwireless.com)
Device goes into sleep after successful OTAA/ABP network join.
RAK3372, RAK1901, RAK1904 and RAK19003 tested so far.
Using the accelerometer LIS3DH mounted on Slot D RAK19003 i.e interupt pin WB_IO5.
*/

#include "app.h"
#include <Adafruit_LIS3DH.h>
// #include <Adafruit_Sensor.h>

// void send_lpp_payload(uint8_t *buffer, uint8_t size);

// float previous_x = 0.0;
// float previous_y = 0.0;
// float previous_z = 0.0;

uint8_t rx_data_0;
uint8_t rx_data_1;
uint8_t rx_data_2;

uint8_t interrupt_threshold = 0x08; // bitwise set 1/32 of full-scale range for ±2g
uint8_t acc_frequency = 2;           // ODR index: 1=1Hz, 2=10Hz, 3=25Hz, 4=50Hz, 5=100Hz, 6=200Hz, 7=400Hz
uint8_t interrupt_duration = 0x01;  // bitwise set at at 10Hz (0.1 sec) the duration = interrupt_duration*0.1 sec (0-127)

// Forward declarations
void int_callback_rak1904(void);
void send_acc_alert(void* unused);

/** Sensor instance using Wire */
Adafruit_LIS3DH acc_sensor(&Wire);

/** For internal usage */
TwoWire *usedWire;

/** Interrupt pin — board-conditional default set via ACC_INT_PIN in app.h. */
uint8_t acc_int_pin = ACC_INT_PIN;

/** Last time a motion was detected */
volatile time_t last_trigger = 0;     // not used at the moment

/** Flag if motion was detected */
volatile bool motion_detected = false;

/**
 * @brief Read RAK1904 register
 *     Added here because Adafruit made that function private :-(
 *
 * @param chip_reg register address
 * @param dataToWrite data to write
 * @return true write success
 * @return false write failed
 */
bool rak1904_writeRegister(uint8_t chip_reg, uint8_t dataToWrite)
{
	// Write the byte
	usedWire->beginTransmission(LIS3DH_DEFAULT_ADDRESS);
	usedWire->write(chip_reg);
	usedWire->write(dataToWrite);
	if (usedWire->endTransmission() != 0)
	{
		return false;
	}

	return true;
}

/**
 * @brief Write RAK1904 register
 *     Added here because Adafruit made that function private :-(
 *
 * @param outputPointer
 * @param chip_reg
 * @return true read success
 * @return false read failed
 */
bool rak1904_readRegister(uint8_t *outputPointer, uint8_t chip_reg)
{
	// Return value
	uint8_t result = 0;
	uint8_t numBytes = 1;

	usedWire->beginTransmission(LIS3DH_DEFAULT_ADDRESS);
	usedWire->write(chip_reg);
	if (usedWire->endTransmission() != 0)
	{
		return false;
	}
	usedWire->requestFrom(LIS3DH_DEFAULT_ADDRESS, numBytes);
	if (!usedWire->available())
	{
		return false;
	}
	while (usedWire->available()) // slave may send less than requested
	{
		result = usedWire->read(); // receive a byte as a proper uint8_t
	}

	*outputPointer = result;
	return true;
}

/**
 * @brief Initialize LIS3DH 3-axis
 * acceleration sensor
 *
 * @return true If sensor was found and is initialized
 * @return false If sensor initialization failed
 */
bool init_rak1904(void)
{
	MYLOG("ACC", "ACC sensor initialization begin...");
  
  // Setup interrupt pin
	pinMode(acc_int_pin, INPUT);
  MYLOG("ACC", "Interrupt pin setup complete");
	
	usedWire = &Wire;
  MYLOG("ACC", "I2C communication initialized");

	if (!acc_sensor.begin())
	{
		MYLOG("ACC", "ACC sensor initialization failed");
		return false;
	}

	acc_sensor.setDataRate(LIS3DH_DATARATE_10_HZ);
	acc_sensor.setRange(LIS3DH_RANGE_2_G);
  MYLOG("ACC", "Accelerometer data rate and range set");


	// Note: enableDRDY() calls are omitted — they modify CTRL3 which is overwritten
	// below by the direct register write. Leaving them in caused DRDY on INT1 briefly
	// before being overridden, which could trigger spurious wakeups.
	uint8_t data_to_write = 0;
	data_to_write |= 0x20;									  // Z high
	data_to_write |= 0x08;									  // Y high
	data_to_write |= 0x02;									  // X high
	rak1904_writeRegister(LIS3DH_REG_INT1CFG, data_to_write); // Enable interrupts on high thresholds for X, Y, and Z axes

	// Set interrupt trigger range
	data_to_write = 0;
	data_to_write |= 0x08;									  // threshold 1/16 range
	rak1904_writeRegister(LIS3DH_REG_INT1THS, data_to_write); // 1/8th range

	// Set interrupt signal length
	data_to_write = 0;
	data_to_write |= 0x01; // Set interrupt duration to 20ms (1 * 1/50 s)
	rak1904_writeRegister(LIS3DH_REG_INT1DUR, data_to_write);

	rak1904_readRegister(&data_to_write, LIS3DH_REG_CTRL5);
	data_to_write &= 0xF3;									// Clear bits of interest
	data_to_write |= 0x08;									// Latch interrupt (Cleared by reading int1_src)
	rak1904_writeRegister(LIS3DH_REG_CTRL5, data_to_write); // Set interrupt to latching

	// Route only INT generator 1 (AOI1/motion) to INT1 pin.
	// I1_IA2 (0x20) was previously also set here, routing INT2 generator to INT1.
	// clear_int_rak1904() only reads INT1_SRC — it cannot clear an INT2 latch, so
	// a latched INT2 event would hold INT1 asserted and cause repeated MCU wakeups.
	data_to_write = 0;
	data_to_write |= 0x40; // I1_IA1: AOI1 motion interrupt on INT1 pin (only)
	rak1904_writeRegister(LIS3DH_REG_CTRL3, data_to_write);

	// No interrupt on pin 2
	rak1904_writeRegister(LIS3DH_REG_CTRL6, 0x00);

	// Enable high pass filter
	rak1904_writeRegister(LIS3DH_REG_CTRL2, 0x01);

	// Set low power mode
	data_to_write = 0;
	rak1904_readRegister(&data_to_write, LIS3DH_REG_CTRL1);
	data_to_write |= 0x08;
	rak1904_writeRegister(LIS3DH_REG_CTRL1, data_to_write);
	delay(100);
	data_to_write = 0;
	rak1904_readRegister(&data_to_write, 0x1E);
	data_to_write |= 0x90;
	rak1904_writeRegister(0x1E, data_to_write);
	delay(100);

	// Apply configurable settings (threshold, duration, frequency) — also clears pending interrupt
	MYLOG("ACC", "Before setup_rak1904: acc_freq=0x%02X threshold=0x%02X duration=0x%02X",
		  acc_frequency, interrupt_threshold, interrupt_duration);
	setup_rak1904();

	// Create timer for deferred motion alert (keeps ISR minimal and avoids LoRa API in ISR context)
	api.system.timer.create(RAK_TIMER_1, send_acc_alert, RAK_TIMER_ONESHOT);
	MYLOG("ACC", "RAK1904 shall be mounted on %s", acc_int_pin == WB_IO3 ? "SlotC" : "SlotD");
	attachInterrupt(acc_int_pin, int_callback_rak1904, RISING);

	last_trigger = millis();

	MYLOG("ACC", "RAK1904 accelerometer initialization completed successfully");
	return true;
}

/**
 * @brief ACC interrupt handler — minimal ISR, defers work to timer
 *
 */
void int_callback_rak1904(void)
{
	// Keep ISR minimal: only start a one-shot timer.
	// send_acc_alert() will do the actual work outside ISR context.
	motion_detected = true;
	api.system.timer.start(RAK_TIMER_1, 10, NULL);
}

/**
 * @brief Clear ACC interrupt register to enable next wakeup
 *
 */
void clear_int_rak1904(void)
{
	acc_sensor.readAndClearInterrupt();
	MYLOG("ACC", "Interrupt cleared...");
}

/**
 * @brief Deferred motion alert handler — called by RAK_TIMER_1 after ISR
 *
 */
void send_acc_alert(void* unused)
{
#if MY_DEBUG == 1
	digitalWrite(LED_BLUE, HIGH);
#endif
	MYLOG("ACC", "Motion detected! Sending immediate alert...");
	g_solution_data.reset();
	g_solution_data.addDigitalOutput(LPP_CHANNEL_DIG_6, 0x01);
	// Reset flag unconditionally so ISR can arm the next event regardless of TX outcome.
	// A missed alert due to TX failure is recoverable; a stuck flag silences all future alerts.
	motion_detected = false;
	send_packet();
	clear_int_rak1904();
}

/**
 * @brief Read accelerometer data from the RAK1904 sensor and add it to the solution data
 * 
  // Get the latest sensor event
  acc_sensor.getEvent(&event);
 * logs the values, adds them to the Cayenne LPP formatted solution data, and clears the interrupt register.
 */
void read_rak1904(void)
{
  sensors_event_t event;
  acc_sensor.getEvent(&event);

  // Adafruit getEvent() returns m/s²; addAccelerometer() expects G — divide by standard gravity
  float x = event.acceleration.x / SENSORS_GRAVITY_STANDARD;
  float y = event.acceleration.y / SENSORS_GRAVITY_STANDARD;
  float z = event.acceleration.z / SENSORS_GRAVITY_STANDARD;

  MYLOG("ACC", "X: %f, Y: %f, Z: %f", x, y, z);

  // Add data to Cayenne LPP format
  //	if (x != previous_x || y != previous_y || z != previous_z) {
	//	g_solution_data.addAccelerometer(LPP_CHANNEL_ACC_1, x, y, z);
	//	previous_x = x;
  //  previous_x = y;
  //  previous_x = z;
	// } 
  g_solution_data.addAccelerometer(LPP_CHANNEL_ACC_1, x, y, z);

  // Read temperature
  // uint8_t temp_l = rak1904_readRegister(LIS3DH_REG_TEMP_OUT_L);
  // uint8_t temp_h = rak1904_readRegister(LIS3DH_REG_TEMP_OUT_H);
  // int16_t temp_raw = (temp_h << 8) | temp_l;
  // float temp = temp_raw / 256.0; // Convert to a rough temperature value
  // MYLOG("ACC", "Temperature: %f", temp);
  // g_solution_data.addTemperature(LPP_CHANNEL_ACCTEMP, temp);

  // Send the payload
  // send_lpp_payload(lpp.getBuffer(), lpp.getSize());

  clear_int_rak1904();
}

void startup_rak1904(void)
{
	// No ODR change needed — shutdown_rak1904() keeps the configured ODR active
	// so the interrupt timing is preserved during sleep. This is a no-op.
}

/**
 * @brief Keep LIS3DH running at its configured ODR during MCU deep sleep.
 *
 * Rationale: the interrupt duration counter ticks at the ODR rate. Dropping to
 * 1 Hz would multiply the effective interrupt_duration by 10 (at default 10 Hz),
 * requiring a full second of sustained motion instead of 100 ms — making the
 * wakeup effectively unresponsive. The power difference between LP-mode 1 Hz
 * and LP-mode 10 Hz is only ~1 µA, not worth the reliability trade-off.
 * The sensor remains in low-power mode (LPen=1) as set by setup_rak1904().
 */
void shutdown_rak1904(void)
{
	// ODR intentionally left unchanged — do not alter CTRL1 here.
}

/* Set during runtime if needed */
/**
 * @brief Configure and set up the accelerometer with specified settings
 * 
 * @return true If the setup was successful
 * @return false If the setup failed
 */
bool setup_rak1904() {

  // Use a local variable for readback — never overwrite the config variables,
  // otherwise a wrong readback silently corrupts them and the next call
  // writes garbage (e.g. CTRL1=0x0A = power-down) into the sensor.
  uint8_t readback = 0;

  // acc_frequency is an ODR index: 1=1Hz, 2=10Hz, 3=25Hz, 4=50Hz, 5=100Hz, 6=200Hz, 7=400Hz
  // Clamp to valid range; 0 (power-down) is rejected and falls back to 10Hz.
  static const uint8_t odr_nibble[8] = {0x00, 0x10, 0x20, 0x30, 0x40, 0x50, 0x60, 0x70};
  if (acc_frequency == 0 || acc_frequency > 7) {
    MYLOG("ACC", "WARNING: acc_frequency=%d out of range [1-7], resetting to 2 (10Hz)", acc_frequency);
    acc_frequency = 2;
  }
  // Build CTRL1: ODR nibble | LPen=1 | Xen=Yen=Zen=1 (0x0F)
  uint8_t ctrl1 = odr_nibble[acc_frequency] | 0x0F;
  rak1904_writeRegister(LIS3DH_REG_CTRL1, ctrl1);
  rak1904_readRegister(&readback, LIS3DH_REG_CTRL1);
  MYLOG("ACC", "LIS3DH_CTRL_REG1: ODR_idx=%d wrote 0x%02X, read 0x%02X", acc_frequency, ctrl1, readback);
  if (readback != ctrl1) {
    MYLOG("ACC", "ERROR: CTRL1 write mismatch (wrote 0x%02X, read 0x%02X) — retrying", ctrl1, readback);
    rak1904_writeRegister(LIS3DH_REG_CTRL1, ctrl1);
    rak1904_readRegister(&readback, LIS3DH_REG_CTRL1);
    if (readback != ctrl1) {
      MYLOG("ACC", "ERROR: CTRL1 write failed after retry — sensor may be misconfigured");
      return false;
    }
  }

  rak1904_writeRegister(LIS3DH_REG_INT1THS, interrupt_threshold);
  rak1904_readRegister(&readback, LIS3DH_REG_INT1THS);
  MYLOG("ACC", "LIS3DH_INT1_THS: wrote 0x%02X, read 0x%02X", interrupt_threshold, readback);

  // Warn if duration is unreasonably long (at 10Hz each count = 100ms)
  if (interrupt_duration > 10) {
    MYLOG("ACC", "WARNING: interrupt_duration=%d → %.1fs at 10Hz — send 001 on port 6 to reset",
          interrupt_duration, interrupt_duration * 0.1f);
  }
  rak1904_writeRegister(LIS3DH_REG_INT1DUR, interrupt_duration);
  rak1904_readRegister(&readback, LIS3DH_REG_INT1DUR);
  MYLOG("ACC", "LIS3DH_REG_INT1DUR: wrote 0x%02X, read 0x%02X (%.1fs at 10Hz)", interrupt_duration, readback, interrupt_duration * 0.1f);

  MYLOG("ACC", "Finished ACC setup");
  clear_int_rak1904();

  return true;
}


