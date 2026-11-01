/*
Based on LoRaWan deep sleep example DeepSleep-nRF52.ino, by Bernd Giesecke (bernd.giesecke@rakwireless.com)
Device goes into sleep after successful OTAA/ABP network join.
RAK4631 and RAK19007
Using the accelerometer LIS3DH mounted on Slot C RAK19007 i.e interupt pin WB_IO3.
*/

#include "main.h"

// uint8_t interrupt_threshold = 0x7F; // Maximum threshold value (127)
// uint8_t interrupt_threshold = 0x10; // 1/16 of full-scale range for ±2g
uint8_t acc_frequency = 0x2F;       // 10 Hz !! this sets the whole register, so be carefull !!
uint8_t interrupt_threshold = 0x08; // bitwise set 1/32 of full-scale range for ±2g
uint8_t interrupt_duration = 0x01;  // bitwise set at at 10Hz (0.1 sec) the duration = interrupt_duration*0.1 sec (0-127)

void acc_int_callback(void);


void setInterruptThreshold(uint8_t value) {
    interrupt_threshold = value;
}

void setInterruptDuration(uint8_t value) {
    interrupt_duration = value;
}

void setAccFrequency(uint8_t value) {
	// acc_frequency &= 0x0F; // Clear the 4 MSB
	// acc_frequency |= (value & 0xF0); // Set the 4 MSB
    acc_frequency = value; // did not work to restart from power down if not the whole register was set, todo?
}


/** The LIS3DH sensor */
LIS3DH acc_sensor(I2C_MODE, 0x18);

/**
 * @brief Initialize LIS3DH 3-axis 
 * acceleration sensor
 * 
 * @return true If sensor was found and is initialized
 * @return false If sensor initialization failed
 */
bool init_acc(void)
{
	// Setup interrupt pin
	pinMode(INT1_PIN, INPUT);

	Wire.begin();

	acc_sensor.settings.accelSampleRate = 10; //Hz.  Can be: 0,1,10,25,50,100,200,400,1600,5000 Hz
	acc_sensor.settings.accelRange = 2;		  //Max G force readable.  Can be: 2, 4, 8, 16

	acc_sensor.settings.adcEnabled = 1;
	acc_sensor.settings.tempEnabled = 1;
	acc_sensor.settings.xAccelEnabled = 1;
	acc_sensor.settings.yAccelEnabled = 1;
	acc_sensor.settings.zAccelEnabled = 1;

	if (acc_sensor.begin() != 0)
	{
	#ifndef MAX_SAVE
    Serial.println("ACC sensor initialization failed");
  #endif
		return false;
	}

	uint8_t data_to_write = 0;
	// Enable interrupts
	data_to_write |= 0x20;									  //Z high
	data_to_write |= 0x08;									  //Y high
	data_to_write |= 0x02;									  //X high
	acc_sensor.writeRegister(LIS3DH_INT1_CFG, data_to_write); // Enable interrupts on high tresholds for x, y and z

	// Set interrupt trigger range
	data_to_write = 0;
	data_to_write |= 0x08;									  // 1/16 range
	acc_sensor.writeRegister(LIS3DH_INT1_THS, data_to_write); // 1/16th range

	// Set interrupt signal length
	data_to_write = 0;
	data_to_write |= 0x01;
	acc_sensor.writeRegister(LIS3DH_INT1_DURATION, data_to_write);

	acc_sensor.readRegister(&data_to_write, LIS3DH_CTRL_REG5);
	data_to_write &= 0xF3;									   //Clear bits of interest
	data_to_write |= 0x08;									   //Latch interrupt (Cleared by reading int1_src)
	acc_sensor.writeRegister(LIS3DH_CTRL_REG5, data_to_write); // Set interrupt to latching

	// Select interrupt pin 1
	data_to_write = 0;
	data_to_write |= 0x40; //AOI1 event (Generator 1 interrupt on pin 1)
	data_to_write |= 0x20; //AOI2 event ()
	acc_sensor.writeRegister(LIS3DH_CTRL_REG3, data_to_write);

	// No interrupt on pin 2
	acc_sensor.writeRegister(LIS3DH_CTRL_REG6, 0x00); 

	// Enable high pass filter
	acc_sensor.writeRegister(LIS3DH_CTRL_REG2, 0x01); 

	// Set low power mode
	data_to_write = 0;
	acc_sensor.readRegister(&data_to_write, LIS3DH_CTRL_REG1);
	data_to_write |= 0x08;
	acc_sensor.writeRegister(LIS3DH_CTRL_REG1, data_to_write);
	vTaskDelay(pdMS_TO_TICKS(100));

	data_to_write = 0;
	acc_sensor.readRegister(&data_to_write, 0x1E);
	data_to_write |= 0x90;
	acc_sensor.writeRegister(0x1E, data_to_write);
	vTaskDelay(pdMS_TO_TICKS(100));

	// clear_acc_int();

	// // Set the interrupt callback function
	attachInterrupt(INT1_PIN, acc_int_callback, RISING);

	read_acc();

#ifndef MAX_SAVE
	Serial.println("Finished ACC init");
#endif

	return true;
}

/* Set during runtime if needed */
/**
 * @brief Configure and set up the accelerometer with specified settings
 * 
 * @return true If the setup was successful
 * @return false If the setup failed
 */
bool setup_acc() {
    
  // Set the frequency
  uint8_t data_to_write = 0;
  data_to_write = acc_frequency;
	acc_sensor.writeRegister(LIS3DH_CTRL_REG1, data_to_write);  // Set the 4 most significant bits (MSB) in the register.
	// acc_sensor.settings.accelSampleRate = acc_frequency;

	// Read back LIS3DH_CTRL_REG1
 	acc_sensor.readRegister(&acc_frequency, LIS3DH_CTRL_REG1);
 #ifndef MAX_SAVE
 	Serial.printf("LIS3DH_CTRL_REG1: 0x%02X\n", acc_frequency);
  #endif

	data_to_write = 0;
	data_to_write = interrupt_threshold;
	acc_sensor.writeRegister(LIS3DH_INT1_THS, data_to_write);

	// Read back LIS3DH_INT1_THS
 	acc_sensor.readRegister(&interrupt_threshold, LIS3DH_INT1_THS);
 #ifndef MAX_SAVE
 	Serial.printf("LIS3DH_INT1_THS: 0x%02X\n", interrupt_threshold);
  #endif

	// Set interrupt signal length
	data_to_write = 0;
	data_to_write = interrupt_duration;
	acc_sensor.writeRegister(LIS3DH_INT1_DURATION, data_to_write);

	// Read back LIS3DH_INT1_DURATION
 	acc_sensor.readRegister(&interrupt_duration, LIS3DH_INT1_DURATION);
 #ifndef MAX_SAVE
 	Serial.printf("LIS3DH_INT1_DURATION: 0x%02X\n", interrupt_duration);
  #endif
    
    // clear_acc_int();

    // // Set the interrupt callback function
    detachInterrupt(INT1_PIN);
    attachInterrupt(INT1_PIN, acc_int_callback, RISING);

    read_acc();

   // #ifndef MAX_SAVE
   // uint8_t regValue = 0x00;
   // acc_sensor.readRegister(&regValue, LIS3DH_CTRL_REG1);
   // Serial.printf("LIS3DH_CTRL_REG1: 0x%02X\n", regValue);
// #endif

#ifndef MAX_SAVE
    Serial.println("Finished ACC setup");
#endif

    return true;
}

void read_acc(void)
{
	 acc_x = (int16_t)(acc_sensor.readFloatAccelX() * 1000.0);
	 acc_y = (int16_t)(acc_sensor.readFloatAccelY() * 1000.0);
	 acc_z = (int16_t)(acc_sensor.readFloatAccelZ() * 1000.0);

#ifndef MAX_SAVE
	Serial.printf("X %d\r\n", acc_x);
	Serial.printf("Y %d\r\n", acc_y);
	Serial.printf("Z %d\r\n", acc_z);
#endif

}

/**
 * @brief ACC interrupt handler
 * @note gives semaphore to wake up main loop
 * 
 */
void acc_int_callback(void)
{
	eventType = 2;

	if (taskEvent != NULL)
	{
#ifndef MAX_SAVE
		Serial.println("Waking up loop task ACC");
#endif
		xSemaphoreGiveFromISR(taskEvent, pdFALSE);
	}
}

/**
 * @brief Clear ACC interrupt register to enable next wakeup
 * 
 * This function reads the interrupt source register of the LIS3DH accelerometer
 * to clear any active interrupts. This is necessary to enable the sensor to
 * trigger the next interrupt event. The function also prints the status of the
 * interrupt source register for debugging purposes if MAX_SAVE is not defined.
 */
void clear_acc_int(void)
{
	uint8_t data_read;
	acc_sensor.readRegister(&data_read, LIS3DH_INT1_SRC);
	#ifndef MAX_SAVE
	if (data_read & 0x40)
		Serial.printf("Interrupt Active 0x%X\n", data_read);
	if (data_read & 0x20)
		Serial.println("Z high");
	if (data_read & 0x10)
		Serial.println("Z low");
	if (data_read & 0x08)
		Serial.println("Y high");
	if (data_read & 0x04)
		Serial.println("Y low");
	if (data_read & 0x02)
		Serial.println("X high");
	if (data_read & 0x01)
		Serial.println("X low");
#endif

  #ifndef MAX_SAVE
  // Read back LIS3DH_CTRL_REG1
  uint8_t data_to_write = 0;
	acc_sensor.readRegister(&data_to_write, LIS3DH_CTRL_REG1);
	Serial.printf("LIS3DH_CTRL_REG1: 0x%02X\n", data_to_write);
  // Read back LIS3DH_INT1_THS
  acc_sensor.readRegister(&data_to_write, LIS3DH_INT1_THS);
	Serial.printf("LIS3DH_INT1_THS: 0x%02X\n", data_to_write);
  // Read back LIS3DH_INT1_DURATION
  acc_sensor.readRegister(&data_to_write, LIS3DH_INT1_DURATION);
	Serial.printf("LIS3DH_INT1_DURATION: 0x%02X\n", data_to_write);
  #endif 
}


// Function to read temperature from LIS3DH
float read_temp()
{
  uint8_t tempLow, tempHigh;
  acc_sensor.readRegister(&tempLow, 0x0C); // Read low byte of temperature register
  acc_sensor.readRegister(&tempHigh, 0x0D); // Read high byte of temperature register
  int16_t tempRaw = (tempHigh << 8) | tempLow; // Combine high and low bytes
  temperature = (tempRaw / 256.0) + 25.0; // Convert raw temperature to Celsius

    #ifndef MAX_SAVE
      Serial.print("Temperature is: ");
      Serial.print(temperature);
      Serial.println(" C");
    #endif

  return temperature;
}