/*
Based on LoRaWan deep sleep example DeepSleep-nRF52.ino, by Bernd Giesecke (bernd.giesecke@rakwireless.com)
Device goes into sleep after successful OTAA/ABP network join.
RAK4631 and RAK19007
Using the accelerometer LIS3DH mounted on Slot C RAK19007 i.e interupt pin WB_IO3.
*/

#include "main.h"
#include "secrets.h"

// uint16_t sense = 0; // for testing

/** HW definition structure */
hw_config hwConfig;

// // nRF52 - SX126x pin configuration
// int PIN_LORA_RESET = 31; // 4;  // LORA RESET
// int PIN_LORA_NSS = 20;	 // 28;   // LORA SPI CS
// int PIN_LORA_SCLK = 21;	 // 12;  // LORA SPI CLK
// int PIN_LORA_MISO = 23;	 // 14;  // LORA SPI MISO
// int PIN_LORA_DIO_1 = 29; // 11; // LORA DIO_1
// int PIN_LORA_BUSY = 28;	 // 29;  // LORA SPI BUSY
// int PIN_LORA_MOSI = 22;	 // 13;  // LORA SPI MOSI
// int RADIO_TXEN = -1;	 // LORA ANTENNA TX ENABLE
// int RADIO_RXEN = -1;	 // LORA ANTENNA RX ENABLE
// // Replace PIN_SPI_MISO, PIN_LORA_SCLK, PIN_SPI_MOSI with your
// SPIClass SPI_LORA(NRF_SPIM2, PIN_LORA_MISO, PIN_LORA_SCLK, PIN_LORA_MOSI);

/** Max size of the data to be transmitted. */
#define LORAWAN_APP_DATA_BUFF_SIZE 64
/** Number of trials for the join request. */
#define JOINREQ_NBTRIALS 8

/** Join retry intervals and limits */
#define JOIN_RETRY_SHORT_MS    120000   // 2 min — first few retries
#define JOIN_RETRY_LONG_MS     1800000  // 30 min — after initial attempts
#define JOIN_RETRY_SHORT_COUNT 3        // retries before switching to long interval

static SoftwareTimer joinRetryTimer;
static uint8_t joinRetryCount = 0;

/** Forward declaration for join retry handler */
static void joinRetryHandler(TimerHandle_t unused);

/** Lora application data buffer. */
static uint8_t m_lora_app_data_buffer[LORAWAN_APP_DATA_BUFF_SIZE];
/** Lora application data structure. */
static lmh_app_data_t m_lora_app_data = {m_lora_app_data_buffer, 0, 0, 0, 0};

// LoRaWan event handlers
/** LoRaWan callback when join network finished */
static void lorawan_has_joined_handler(void);
/** LoRaWan callback when data arrived */
static void lorawan_rx_handler(lmh_app_data_t *app_data);
/** LoRaWan callback after class change request finished */
static void lorawan_confirm_class_handler(DeviceClass_t Class);
/** LoRaWan Function to send a package */
bool sendLoRaFrame(void);

/**@brief Structure containing LoRaWan parameters, needed for lmh_init()

   Set structure members to
   LORAWAN_ADR_ON or LORAWAN_ADR_OFF to enable or disable adaptive data rate
   LORAWAN_DEFAULT_DATARATE OR DR_0 ... DR_5 for default data rate or specific data rate selection
   LORAWAN_PUBLIC_NETWORK or LORAWAN_PRIVATE_NETWORK to select the use of a public or private network
   JOINREQ_NBTRIALS or a specific number to set the number of trials to join the network
   LORAWAN_DEFAULT_TX_POWER or a specific number to set the TX power used
   LORAWAN_DUTYCYCLE_ON or LORAWAN_DUTYCYCLE_OFF to enable or disable duty cycles
					 Please note that ETSI mandates duty cycled transmissions.
*/

// #define LORAWAN_TX_POWER TX_POWER_5

static lmh_param_t lora_param_init = {LORAWAN_ADR_ON, DR_3, LORAWAN_PUBLIC_NETWORK, JOINREQ_NBTRIALS, LORAWAN_DEFAULT_TX_POWER, LORAWAN_DUTYCYCLE_ON};

/** Structure containing LoRaWan callback functions, needed for lmh_init() */
static lmh_callback_t lora_callbacks = {BoardGetBatteryLevel, BoardGetUniqueId, BoardGetRandomSeed,
										lorawan_rx_handler, lorawan_has_joined_handler, lorawan_confirm_class_handler};

/** Device EUI required for OTAA network join */
uint8_t nodeDeviceEUI[8] = NODE_DEVICE_EUI;
/** Application EUI required for network join */
uint8_t nodeAppEUI[8] = NODE_APP_EUI;
/** Application key required for network join */
uint8_t nodeAppKey[16] = NODE_APP_KEY;
/** Device address required for ABP network join */
uint32_t nodeDevAddr = NODE_DEV_ADDR;
/** Network session key required for ABP network join */
uint8_t nodeNwsKey[16] = NODE_NWS_KEY;
/** Application session key required for ABP network join */
uint8_t nodeAppsKey[16] = NODE_APPS_KEY;
/** Flag whether to use OTAA or ABP network join method */
bool doOTAA = DO_OTAA;

/**
   @brief Initialize LoRa HW and LoRaWan MAC layer

   @return int8_t result
	0 => OK
   -1 => SX126x HW init failure
   -2 => LoRaWan MAC initialization failure
   -3 => Subband selection failure
   -4 => LoRaWan handler task start failure
*/
int8_t initLoRaWan(void)
{
	//   // Create the LoRaWan event semaphore
	//   loraEvent = xSemaphoreCreateBinary();
	//   // Initialize semaphore
	//   xSemaphoreGive(loraEvent);

	// Define the HW configuration between MCU and SX126x
	// hwConfig.CHIP_TYPE = SX1262_CHIP;		  // Example uses an eByte E22 module with an SX1262
	// hwConfig.PIN_LORA_RESET = PIN_LORA_RESET; // LORA RESET
	// hwConfig.PIN_LORA_NSS = PIN_LORA_NSS;	  // LORA SPI CS
	// hwConfig.PIN_LORA_SCLK = PIN_LORA_SCLK;	  // LORA SPI CLK
	// hwConfig.PIN_LORA_MISO = PIN_LORA_MISO;	  // LORA SPI MISO
	// hwConfig.PIN_LORA_DIO_1 = PIN_LORA_DIO_1; // LORA DIO_1
	// hwConfig.PIN_LORA_BUSY = PIN_LORA_BUSY;	  // LORA SPI BUSY
	// hwConfig.PIN_LORA_MOSI = PIN_LORA_MOSI;	  // LORA SPI MOSI
	// hwConfig.RADIO_TXEN = RADIO_TXEN;		  // LORA ANTENNA TX ENABLE
	// hwConfig.RADIO_RXEN = RADIO_RXEN;		  // LORA ANTENNA RX ENABLE
	// hwConfig.USE_DIO2_ANT_SWITCH = true;	  // Example uses an CircuitRocks Alora RFM1262 which uses DIO2 pins as antenna control
	// hwConfig.USE_DIO3_TCXO = true;			  // Example uses an CircuitRocks Alora RFM1262 which uses DIO3 to control oscillator voltage
	// hwConfig.USE_DIO3_ANT_SWITCH = false;	  // Only Insight ISP4520 module uses DIO3 as antenna control

	// Initialize LoRa chip.
	if (lora_rak4630_init() != 0)
	{
		return -1;
	}

	// Setup the EUIs and Keys
	lmh_setDevEui(nodeDeviceEUI);
	lmh_setAppEui(nodeAppEUI);
	lmh_setAppKey(nodeAppKey);
	lmh_setNwkSKey(nodeNwsKey);
	lmh_setAppSKey(nodeAppsKey);
	lmh_setDevAddr(nodeDevAddr);

	// Initialize LoRaWan
	if (lmh_init(&lora_callbacks, lora_param_init, doOTAA, CLASS_A, LORAMAC_REGION_EU868) != 0)
	{
		return -2;
	}

	// For some regions we might need to define the sub band the gateway is listening to
	// This must be called AFTER lmh_init()
	if (!lmh_setSubBandChannels(1))
	{
		return -3;
	}

	// Start Join procedure
#ifndef MAX_SAVE
	Serial.println("Start network join request");
#endif
	lmh_join();

	// Start join retry timer: fast retries first, then backs off
	joinRetryCount = 0;
	joinRetryTimer.begin(JOIN_RETRY_SHORT_MS, joinRetryHandler);
	joinRetryTimer.start();

	return 0;
}

/**
   @brief Retry handler: re-attempts lmh_join() if still not joined.
   Switches from short to long interval after JOIN_RETRY_SHORT_COUNT rounds.
*/
static void joinRetryHandler(TimerHandle_t unused) {
	if (lmh_join_status_get() == LMH_SET) {
		joinRetryTimer.stop();
		return;
	}
	joinRetryCount++;
#ifndef MAX_SAVE
	Serial.printf("LoRa join retry #%d\n", joinRetryCount);
#endif
	// Switch to long interval after initial quick retries
	if (joinRetryCount == JOIN_RETRY_SHORT_COUNT) {
		joinRetryTimer.stop();
		joinRetryTimer.begin(JOIN_RETRY_LONG_MS, joinRetryHandler);
		joinRetryTimer.start();
	}
	lmh_join();
}

/**
   @brief LoRa function for handling HasJoined event.
*/
static void lorawan_has_joined_handler(void)
{
	// Cancel retry timer — we're in
	joinRetryTimer.stop();

	if (doOTAA)
	{
		uint32_t otaaDevAddr = lmh_getDevAddr();
#ifndef MAX_SAVE
		Serial.printf("OTAA joined and got dev address %08X\n", otaaDevAddr);
#endif
		eventType = 3;
		if (taskEvent != NULL)
		{
			xSemaphoreGive(taskEvent);
		}
	}
	else
	{
#ifndef MAX_SAVE
		Serial.println("ABP joined");
#endif
	}

	// Default is Class A, where the SX1262 transceiver is in sleep mode unless a package is sent
	// If switched to Class C the power consumption is higher because the SX1262 chip remains in RX mode

	// lmh_class_request(CLASS_C);

	// Now we are connected, start the timer that will wakeup the loop frequently
	taskWakeupTimer.begin(SLEEP_TIME, periodicWakeup);
	taskWakeupTimer.start();
}

/**
   @brief Function for handling LoRaWan received data from Gateway

   @param app_data  Pointer to rx data
*/
static void lorawan_rx_handler(lmh_app_data_t *app_data)
{
#ifndef MAX_SAVE
	Serial.printf("LoRa Packet received on port %d, size:%d, rssi:%d, snr:%d\n",
				  app_data->port, app_data->buffsize, app_data->rssi, app_data->snr);
#endif

  // Memory safety: Check buffer size before accessing elements
  if (app_data->buffsize < 3) {
#ifndef MAX_SAVE
    Serial.println("Warning: Received packet too small, skipping processing");
#endif
    return; // Insufficient data, exit early
  }

  uint8_t rx_data_0 = app_data->buffer[0];
  uint8_t rx_data_1 = app_data->buffer[1];
  uint8_t rx_data_2 = app_data->buffer[2];

#ifndef MAX_SAVE
  Serial.printf("RX Buffer 0 %d\n", rx_data_0);
  Serial.printf("RX Buffer 1 %d\n", rx_data_1);
  Serial.printf("RX Buffer 2 %d\n", rx_data_2);
#endif

  // Server sends a zero-padded 3-digit ASCII string, e.g. "002" for value 2, "127" for 127.
  // Max value 255. Bytes are ASCII digits ('0'=48 .. '9'=57).
  uint8_t combined_data = (rx_data_0 - '0') * 100 + (rx_data_1 - '0') * 10 + (rx_data_2 - '0');
#ifndef MAX_SAVE
    Serial.printf("Combined Data: %u\n", combined_data);
	  Serial.printf("ACC frequency: %d\n", acc_frequency);
    Serial.printf("Interrupt threshold: %d\n", interrupt_threshold);
    Serial.printf("Interrupt duration: %d\n", interrupt_duration);
  #endif

	switch (app_data->port)
	{
	case 3:
		// Port 3 switches the class
		if (app_data->buffsize == 1)
		{
			switch (app_data->buffer[0])
			{
			case 0:
				lmh_class_request(CLASS_A);
#ifndef MAX_SAVE
				Serial.println("Request to switch to class A");
#endif
				break;

			case 1:
				lmh_class_request(CLASS_B);
#ifndef MAX_SAVE
				Serial.println("Request to switch to class B");
#endif
				break;

			case 2:
				lmh_class_request(CLASS_C);
#ifndef MAX_SAVE
				Serial.println("Request to switch to class C");
#endif
				break;

		default:
				break;
			}
		}

		break;

      case 5:
      	setInterruptThreshold(combined_data);
        eventType = 5;
        if (taskEvent != NULL) { xSemaphoreGive(taskEvent); }
        break;

      case 6:
      	setAccFrequency(combined_data);
        eventType = 5;
        if (taskEvent != NULL) { xSemaphoreGive(taskEvent); }
        break;

      case 7:
      	setInterruptDuration(combined_data);
        eventType = 5;
        if (taskEvent != NULL) { xSemaphoreGive(taskEvent); }
        break;

	case LORAWAN_APP_PORT:
		// Copy the data into loop data buffer
		memcpy(rcvdLoRaData, app_data->buffer, app_data->buffsize);
		rcvdDataLen = app_data->buffsize;
		eventType = 0;
		// Notify task about the event
		if (taskEvent != NULL)
		{
#ifndef MAX_SAVE
			Serial.println("Waking up loop task");
#endif
			xSemaphoreGive(taskEvent);
		}
	}
}

/**
   @brief Callback for class switch confirmation

   @param Class The new class
*/
static void lorawan_confirm_class_handler(DeviceClass_t Class)
{
#ifndef MAX_SAVE
	Serial.printf("switch to class %c done\n", "ABC"[Class]);
#endif

	// Informs the server that switch has occurred ASAP
	m_lora_app_data.buffsize = 0;
	m_lora_app_data.port = LORAWAN_APP_PORT;
	lmh_send(&m_lora_app_data, LMH_UNCONFIRMED_MSG);
}

/**
   @brief Send a LoRaWan package

   @return result of send request
*/
bool sendLoRaFrame(void)
{
	if (lmh_join_status_get() != LMH_SET)
	{
		// Not joined, try again later
#ifndef MAX_SAVE
		Serial.println("Did not join network, skip sending frame");
#endif
		return false;
	}

	m_lora_app_data.port = LORAWAN_APP_PORT;

	uint32_t i = 0;
  memset(m_lora_app_data.buffer, 0, LORAWAN_APP_DATA_BUFF_SIZE);
  // Send raw battery level from ADC
  m_lora_app_data.buffer[i++] = (raw_adc >> 8) & 0xFF;     // Byte 0
  m_lora_app_data.buffer[i++] = raw_adc & 0xFF;
  // Send X, Y and Z from LIS3DH
  m_lora_app_data.buffer[i++] = (acc_x >> 8) & 0xFF;       // Byte 2
  m_lora_app_data.buffer[i++] = acc_x & 0xFF;
  m_lora_app_data.buffer[i++] = (acc_y >> 8) & 0xFF;
  m_lora_app_data.buffer[i++] = acc_y & 0xFF;
  m_lora_app_data.buffer[i++] = (acc_z >> 8) & 0xFF;
  m_lora_app_data.buffer[i++] = acc_z & 0xFF;
  // Send temperature from LIS3DH
  m_lora_app_data.buffer[i++] = (int8_t)temperature;      // Byte 8, (-128 to 127 range)
  // Send info bytes
  m_lora_app_data.buffer[i++] = status_byte;              // Byte 9
  m_lora_app_data.buffer[i++] = version_byte;
  // Send LIS3DH_INT1_THS, LIS3DH_INT1_DURATION and LIS3DH_CTRL_REG1 here
  m_lora_app_data.buffer[i++] = (uint8_t)interrupt_threshold;          // Byte 11
  m_lora_app_data.buffer[i++] = (uint8_t)interrupt_duration;
  m_lora_app_data.buffer[i++] = (uint8_t)acc_frequency;
  
  m_lora_app_data.buffsize = i;                                 // Count buffer size before sending

#ifndef MAX_SAVE
  Serial.printf("LIS3DH_CTRL_REG1 in sendLoRaFrame: 0x%02X\n", acc_frequency);
 	Serial.printf("LIS3DH_INT1_THS in sendLoRaFrame: 0x%02X\n", interrupt_threshold);
  Serial.printf("LIS3DH_INT1_DURATION in sendLoRaFrame: 0x%02X\n", interrupt_duration);
  #endif

	lmh_error_status error = lmh_send(&m_lora_app_data, LMH_UNCONFIRMED_MSG);

	return (error == 0);
}
