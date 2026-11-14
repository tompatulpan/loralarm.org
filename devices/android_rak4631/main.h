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

// Battery protection. Raw 12-bit ADC, AR_INTERNAL_3_0; volts = raw * (3.0 / 4095) * BATT_DIVIDER,
// the same formula as the ChirpStack codec (payload_codec_rak4631.js). Calibrate BATT_DIVIDER
// against a meter and change the codec with it.
#define BATT_DIVIDER 1.7f
#define BATT_CRITICAL_MV 2800            // At or below: latch (alarm frames only, rare heartbeats)
#define BATT_RESUME_MARGIN_MV 50         // Hysteresis: latch clears above BATT_CRITICAL_MV + this
#define BATT_NO_BATTERY_MV 500           // At or below: no battery attached (USB only), never a latch reason
#define BATT_SAMPLES 5                   // Median of this many ADC reads per measurement
#define BATT_CONFIRM_READS 3             // A low measurement must repeat this many times in a row
#define BATT_CONFIRM_GAP_MS 100          // ... this far apart, before the latch is set
#define BATT_BOOT_RECHECK_MS 60000UL     // Boot guard: re-check interval while the supply is critical
#define BATT_LATCHED_HEARTBEAT_EVERY 12  // While latched, send every Nth periodic tick (12 x 5 min = 1 h)
#define STATUS_BATT_LATCH 0x08           // Status byte bit3: low-battery latch (bit0 switch, bit1 ACC, bit2 FUNK_ON)

extern bool battery_latched;
uint16_t read_batt_raw(void);
uint16_t batt_raw_to_mv(uint16_t raw);
void read_batt(void);

#endif // MAIN_H
