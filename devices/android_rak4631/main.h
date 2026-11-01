/*
Based on LoRaWan deep sleep example DeepSleep-nRF52.ino, by Bernd Giesecke (bernd.giesecke@rakwireless.com)
Device goes into sleep after successful OTAA/ABP network join.
RAK4631 and RAK19007
Using the accelerometer LIS3DH mounted on Slot C RAK19007 i.e interupt pin WB_IO3.
*/

#ifndef MAIN_H
#define MAIN_H

#include <Arduino.h>
#include <SPI.h>
#include <Wire.h>
#include <LoRaWan-Arduino.h>

extern int16_t acc_x;
extern int16_t acc_y;
extern int16_t acc_z;
extern float temperature;
extern uint16_t raw_adc;
extern uint8_t status_byte; // Example status byte
extern uint8_t version_byte; 
extern uint8_t interrupt_threshold;
extern uint8_t interrupt_duration;
extern uint8_t acc_frequency;

// GPIO interrupt related variables
extern volatile bool gpioInterruptEnabled;
extern volatile unsigned long lastDebounceTime;
extern const unsigned long debounceDelay;

// Comment the next line out if you want DEBUG output.
// #define MAX_SAVE

/* Time the device is sleeping in milliseconds */
#define SLEEP_TIME 300000 // 5 min

// LoRaWan stuff
int8_t initLoRaWan(void);
bool sendLoRaFrame(void);
// extern SemaphoreHandle_t loraEvent;

// Main loop stuff
void periodicWakeup(TimerHandle_t unused);
extern SemaphoreHandle_t taskEvent;
extern uint8_t rcvdLoRaData[];
extern uint8_t rcvdDataLen;
extern volatile int8_t eventType;
extern SoftwareTimer taskWakeupTimer;

// RAK1904 accelerometer
#include <SparkFunLIS3DH.h>
#define INT1_PIN WB_IO3
bool init_acc(void);
bool setup_acc();
void clear_acc_int(void);
void read_acc(void);
float read_temp();
void setInterruptThreshold(uint8_t value);
void setInterruptDuration(uint8_t value);
void setAccFrequency(uint8_t value);

// GPIO 
#define GPIO_PIN WB_IO1 // GPIO pin for interupt and open-circuit detection
// ADC
// #define NUM_SAMPLES 5   // Number of ADC readings to be averaged

// Battery protection thresholds (raw 12-bit ADC, AR_INTERNAL_3_0, 1:1 divider)
#define BATT_CRITICAL_RAW 1707  // ~2.5V: hard shutdown
void enterSystemOff(void);

#endif // MAIN_H
