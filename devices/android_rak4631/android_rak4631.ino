/*
Based on LoRaWan deep sleep example DeepSleep-nRF52.ino, by Bernd Giesecke (bernd.giesecke@rakwireless.com)
Device goes into sleep after successful OTAA/ABP network join.
RAK4631 and RAK19007
Using the accelerometer LIS3DH mounted on Slot C RAK19007 i.e interupt pin WB_IO3.

 01 - added temp_read
 02 - Send data
 03 - everything in place expect battery
 04 - plus battery and clear_acc_int on all readings
 05- Changed sleep time to 5 min
 ver20:06 - added max number of retryies
 ver21:07 - Send th and duration
 ver22:08 - Send frequency
 ver23:09 - Report Ths, duration and acc_freq.
 ver24:10 - added GPIO interrup, 
 ver25:11 - Check GPIO state for alarm (== high), read_bat() improved, 
 ver26 - minor changes 
 ver27 - Bug fixes and power improvements
 ver28 - LoRa callback (ports 5/6/7): updates the variable + minor improvments
 ver29 - Send immediately - read_acc(); read_temp(); read_batt(); 	sendLoRaDataWithRetry();
 ver30 - Battery protection: boot guard + runtime shutdown below 2.5V (raw 1707)
 ver31 - LoRa join retry: 3x every 2 min, then every 30 min until joined
 ver32 - Battery supervision: median ADC, 2.8 V recoverable latch (no System OFF), checked on every wake,
         latch reported in status byte bit3 (0x08), boot guard waits instead of shutting down
 
 */
#include "main.h"

int16_t acc_x = 0; // accelerometer X value
int16_t acc_y = 0; // accelerometer Y value
int16_t acc_z = 0; // accelerometer Z value
float temperature = 0;
uint16_t raw_adc = 0;
uint8_t status_byte = 0x00; // status byte
uint8_t version_byte = 32; // version byte

bool battery_latched = false;      // Low-battery latch; clears when the supply recovers (see read_batt)
uint8_t latched_tick_count = 0;    // Periodic ticks seen while latched (heartbeat thinning)

// TODO: Consider replacing with a #define compile-time flag for cleaner configuration,
// since this value is never changed at runtime.
volatile bool gpioInterruptEnabled = false;  // Enable or disable the GPIO interrupt/functionality. External pull-up of 1M ohm between Vcc and IO1 required.
  
volatile unsigned long lastDebounceTime = 0; // the last time the output pin was toggled
const unsigned long debounceDelay = 200; // the debounce time; increase if the output flickers

/** Semaphore used by events to wake up loop task */
SemaphoreHandle_t taskEvent = NULL;

/** Timer to wakeup task frequently and send message */
SoftwareTimer taskWakeupTimer;

/** Buffer for received LoRaWan data */
uint8_t rcvdLoRaData[256];
/** Length of received data */
uint8_t rcvdDataLen = 0;

// #define NUM_SAMPLES 5   // Number of ADC readings to be averaged

/**
 * @brief Flag for the event type
 * -1 => no event
 * 0 => LoRaWan data received
 * 1 => Timer wakeup
 * 2 => ACC interrupt
 * 3 => Network joined
 * 4 => GPIO interrupt
 * 5 => ACC config update received (ports 5/6/7)
 * ...
 */
volatile int8_t eventType = -1;

/**
 * @brief Timer event that wakes up the loop task frequently
 *
 * @param unused
 */
void periodicWakeup(TimerHandle_t unused)
{
	// NOTE: No Serial or digitalWrite here — timer callbacks run in ISR/task context
	// where those calls are unsafe. Debug output is handled in loop() case 1.
	eventType = 1;
	// Give the semaphore, so the loop task will wake up
	xSemaphoreGiveFromISR(taskEvent, pdFALSE);
}


void gpioInterruptHandler() {
    // Check if GPIO interrupt handling is enabled
	// Debounce mechanism
	unsigned long currentTime = millis();
	if ((currentTime - lastDebounceTime) < debounceDelay) {
		return; // Ignore the interrupt if within debounce delay
	}
	lastDebounceTime = currentTime;
  
	eventType = 4;
	#ifndef MAX_SAVE
	  // Switch on green LED to show we are awake
	  digitalWrite(LED_BUILTIN, HIGH);
	#endif
    
	if (taskEvent != NULL)
	{
#ifndef MAX_SAVE
		Serial.println("Waking up loop task GPIO");
#endif
		xSemaphoreGiveFromISR(taskEvent, pdFALSE);
  }
}

void setGpioInterruptEnabled(bool enabled) {
	gpioInterruptEnabled = enabled;
	if (enabled) {
		attachInterrupt(digitalPinToInterrupt(GPIO_PIN), gpioInterruptHandler, CHANGE);
	} else {
		detachInterrupt(digitalPinToInterrupt(GPIO_PIN));
	}
}

/**
 * @brief Arduino setup function. Called once after power-up or reset
 *
 */
void setup(void)
{
	// Create the LoRaWan event semaphore
	taskEvent = xSemaphoreCreateBinary();
	// Initialize semaphore
	xSemaphoreGive(taskEvent);

	// Initialize the built in LED
	pinMode(LED_BUILTIN, OUTPUT);
	digitalWrite(LED_BUILTIN, LOW);

	// Initialize the connection status LED
	pinMode(LED_CONN, OUTPUT);
	digitalWrite(LED_CONN, LOW);

  // ADC
  analogReference(AR_INTERNAL_3_0);
  analogReadResolution(12);

  // Boot-time battery guard: while the supply is critical, wait (timed re-check) instead of
  // joining/transmitting, so a marginal supply is not pushed into a brown-out loop.
  // No System OFF: that needed a power cycle to recover. Near-zero readings mean no battery
  // (USB only) and never block startup.
  delay(10); // allow ADC to stabilize
  {
    const uint16_t resume_mv = BATT_CRITICAL_MV + BATT_RESUME_MARGIN_MV;
    uint16_t mv = batt_raw_to_mv(read_batt_raw());
    if (mv > BATT_NO_BATTERY_MV && mv <= BATT_CRITICAL_MV) {
      while (true) {
        delay(BATT_BOOT_RECHECK_MS);
        mv = batt_raw_to_mv(read_batt_raw());
        if (mv <= BATT_NO_BATTERY_MV || mv > resume_mv) {
          break;
        }
      }
    }
  }

	// Setup GPIO interrupt pin only if gpioInterruptEnabled is true
	if (gpioInterruptEnabled) {
		pinMode(GPIO_PIN, INPUT); // Requires externall pull-up
	}

#ifndef MAX_SAVE
	// Initialize Serial for debug output
	Serial.begin(115200);

	time_t timeout = millis();
	// On nRF52840 the USB serial is not available immediately
	while (!Serial)
	{
		if ((millis() - timeout) < 5000)
		{
			delay(100);
			digitalWrite(LED_BUILTIN, !digitalRead(LED_BUILTIN));
		}
		else
		{
			break;
		}
	}
#endif

	digitalWrite(LED_BUILTIN, LOW);

#ifndef MAX_SAVE
	Serial.println("=====================================");
	Serial.println("     Welcome to LoraLarm.org !!");
	Serial.println("=====================================");
#endif

	// Initialize LoRaWan and start join request
	int8_t loraInitResult = initLoRaWan();

#ifndef MAX_SAVE
	if (loraInitResult != 0)
	{
		switch (loraInitResult)
		{
		case -1:
			Serial.println("SX126x init failed");
			break;
		case -2:
			Serial.println("LoRaWan init failed");
			break;
		case -3:
			Serial.println("Subband init error");
			break;
		case -4:
			Serial.println("LoRa Task init error");
			break;
		default:
			Serial.println("LoRa init unknown error");
			break;
		}

		// Without working LoRa we just stop here
		while (1)
		{
			Serial.println("Nothing we can do, STOPPED !!");
			delay(5000);
		}
	}
	Serial.println("LoRaWan init success");
#endif

	// Initialize ACC
	init_acc();
  // setup_acc();

  // Set GPIO interrupt enabled state
  setGpioInterruptEnabled(gpioInterruptEnabled);

	// Take the semaphore so the loop will go to sleep until an event happens
	xSemaphoreTake(taskEvent, 10);
}

/**
 * @brief Arduino loop task. Called in a loop from the FreeRTOS task handler
 *
 */
void loop(void)
{
	// Switch off green LED to show we go to sleep
	digitalWrite(LED_BUILTIN, LOW);
#ifndef MAX_SAVE
  Serial.println("Going to sleep.......");
#endif

	// Sleep until we are woken up by an event
	if (xSemaphoreTake(taskEvent, portMAX_DELAY) == pdTRUE)
	{
		// Switch on green LED to show we are awake
		#ifndef MAX_SAVE
		digitalWrite(LED_BUILTIN, HIGH);
		#endif
		
#ifndef MAX_SAVE
		// delay(500); // Only so we can see the LED
    Serial.printf("Wakeup event %d\n", eventType);
#endif

		// Check the wake up reason
		switch (eventType)
		{
		case 0: // Wakeup reason is package downlink arrived
#ifndef MAX_SAVE
			Serial.println("Received package over LoRaWan");
#endif
			if (rcvdLoRaData[0] > 0x1F)
			{
#ifndef MAX_SAVE
				Serial.printf("%s\n", (char *)rcvdLoRaData);
#endif
			}
			else
			{
#ifndef MAX_SAVE
				for (int idx = 0; idx < rcvdDataLen; idx++)
				{
					Serial.printf("%X ", rcvdLoRaData[idx]);
				}
				Serial.println("");
#endif
			}

			break;

		case 1: // Wakeup reason is timer
#ifndef MAX_SAVE
			Serial.println("Timer wakeup");
#endif
			read_acc();
      read_temp();
      read_batt();
			if (battery_latched && (++latched_tick_count % BATT_LATCHED_HEARTBEAT_EVERY) != 0) {
#ifndef MAX_SAVE
				Serial.println("Battery latched: periodic frame skipped");
#endif
				clear_acc_int();
				break;
			}
			sendLoRaDataWithRetry();
			clear_acc_int();
		  break;

		case 2:
#ifndef MAX_SAVE
			Serial.println("ACC triggered");
#endif
		read_acc();
		read_temp();
		read_batt();
	    status_byte = 0x02; // set status byte for acc alarm
	    sendLoRaDataWithRetry(); // send status byte
	    status_byte = 0x00; // reset status byte
	    clear_acc_int();
		  break;

		case 3:
#ifndef MAX_SAVE
			Serial.println("Joined network");
#endif
			read_acc();
			read_temp();
			read_batt();
			sendLoRaDataWithRetry();
			clear_acc_int();
			break;

    case 4:
#ifndef MAX_SAVE
			Serial.println("GPIO triggered");
#endif
	    if (digitalRead(GPIO_PIN) == HIGH) {
		read_batt();
		status_byte = 0x01; // set status byte for the GPIO alarm
		// read_acc();
		sendLoRaDataWithRetry(); // send status byte
		status_byte = 0x00; // reset status byte
	  } else {
		  status_byte = 0x00; // reset status byte
	  }
		  break;

		case 5:
#ifndef MAX_SAVE
			Serial.println("ACC config update");
#endif
			setup_acc();
			break;

		default:
#ifndef MAX_SAVE
			Serial.println("This should never happen ;-)");
#endif
			break;
		}
		// Go back to sleep
		xSemaphoreTake(taskEvent, 10);
	}
}

/**
 * @brief Converts a raw 12-bit ADC count on A0 to battery millivolts (same formula as the codec).
 */
uint16_t batt_raw_to_mv(uint16_t raw) {
	return (uint16_t)(raw * (3000.0f / 4095.0f) * BATT_DIVIDER);
}

/**
 * @brief Raw ADC count on A0: median of BATT_SAMPLES reads after one discarded warm-up read.
 *
 * The median rejects a single outlier (wake-up glitch, wrong resolution), which a mean of
 * three samples did not: one bad sample pulled the mean close to the shutdown threshold.
 */
uint16_t read_batt_raw(void) {
	uint16_t samples[BATT_SAMPLES];

	analogRead(WB_A0); // warm-up: charge the sample-and-hold capacitor
	for (int i = 0; i < BATT_SAMPLES; i++) {
		delay(5);
		uint16_t v = (uint16_t)analogRead(WB_A0);
		int j = i;
		while (j > 0 && samples[j - 1] > v) { // insertion sort
			samples[j] = samples[j - 1];
			j--;
		}
		samples[j] = v;
	}
	return samples[BATT_SAMPLES / 2];
}

/**
 * @brief Measures the battery into `raw_adc` and updates the low-battery latch.
 *
 * Latch sets only when the low measurement repeats BATT_CONFIRM_READS times in a row.
 * It clears above BATT_CRITICAL_MV + BATT_RESUME_MARGIN_MV (hysteresis), or when the
 * reading drops to "no battery attached" (USB power). Replaces the old one-sample
 * System OFF, which needed a power cycle to recover.
 */
void read_batt() {
	raw_adc = read_batt_raw();
	uint16_t mv = batt_raw_to_mv(raw_adc);
#ifndef MAX_SAVE
	Serial.printf("raw ADC %d (%u mV)\n", raw_adc, mv);  // raw data is converted on the application side
#endif

	if (battery_latched) {
		if (mv <= BATT_NO_BATTERY_MV || mv > BATT_CRITICAL_MV + BATT_RESUME_MARGIN_MV) {
			battery_latched = false;
			latched_tick_count = 0;
#ifndef MAX_SAVE
			Serial.printf("Battery recovered (%u mV), latch cleared\n", mv);
#endif
		}
		return;
	}

	if (mv <= BATT_NO_BATTERY_MV || mv > BATT_CRITICAL_MV) {
		return;
	}
	for (int i = 1; i < BATT_CONFIRM_READS; i++) {
		delay(BATT_CONFIRM_GAP_MS);
		uint16_t again = batt_raw_to_mv(read_batt_raw());
		if (again <= BATT_NO_BATTERY_MV || again > BATT_CRITICAL_MV) {
#ifndef MAX_SAVE
			Serial.printf("Low reading %u mV not confirmed (%u mV), not latching\n", mv, again);
#endif
			return;
		}
	}
	battery_latched = true;
	latched_tick_count = 0;
#ifndef MAX_SAVE
	Serial.printf("Battery critical (%u mV <= %u mV): latched, alarm frames only\n", mv, BATT_CRITICAL_MV);
#endif
}

/**
 * @brief Sends a LoRaWan data package with a retry mechanism in case of failure.
 */
void sendLoRaDataWithRetry() {
	const int maxRetries = 3; // Maximum number of retries
	int attempt = 0;
	bool success = false;

	while (attempt < maxRetries && !success) {
		// Send the data package
		if (sendLoRaFrame()) {
			success = true;
			#ifndef MAX_SAVE
			Serial.println("LoRaWan package sent successfully");
			#endif
		} else {
			attempt++;
			// Only delay if there are retries remaining
			if (attempt < maxRetries) {
				#ifndef MAX_SAVE
				Serial.println("LoRaWan package send failed, trying again...");
				#endif
				vTaskDelay(pdMS_TO_TICKS(5000));
			}
		}
	}

	if (!success) {
		#ifndef MAX_SAVE
		Serial.println("LoRaWan package send failed after maximum retries");
		#endif
	}
}
