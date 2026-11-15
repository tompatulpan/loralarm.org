/**
 * @file lorawan.cpp
 * @brief LoRaWAN / P2P stack configuration and callbacks.
 *
 * Callbacks run in the LORA context: no I2C and no flash access in here. Work
 * that needs them is flagged (acc_config_pending) and done by sensor_handler().
 */
#include "app.h"
#include "downlink_parse.h"

// ---------------------------------------------------------------------------
// LoRaWAN callbacks
// ---------------------------------------------------------------------------

/** Callback after join request cycle */
static void joinCallback(int32_t status)
{
	if (status != 0)
	{
		MYLOG("JOIN-CB", "LoRaWan OTAA - join fail! Next retry in %d s (auto-rejoin active)\r\n", JOIN_RETRY_SEC);
		return;
	}

	MYLOG("JOIN-CB", "LoRaWan OTAA - joined!\r\n");

	// Enable ADR — network will now manage DR and TX power adaptively
	api.lorawan.adr.set(true);
	// Step down from join TX power to POST_JOIN_TXP. ADR will adjust from this
	// baseline — it can go up or down. Staying at TXP=0 saturates a nearby
	// gateway LNA → wild RSSI swings and COLLISION_PACKET on RX1 downlink.
	api.lorawan.txp.set(POST_JOIN_TXP);
	MYLOG("JOIN-CB", "ADR enabled. TXP set to %d (~%d dBm EIRP). DR=%d",
		  POST_JOIN_TXP, 16 - POST_JOIN_TXP * 2, api.lorawan.dr.get());

	digitalWrite(LED_BLUE, LOW);

	// Start normal application send timer now that we are joined.
	// Guard: if setup() is still running (join completed from flash before sensor init
	// finished), skip — lorawan_resume_cached_join() will start the timer and trigger
	// the first TX. Starting it here AND there would double-start the timer.
	if (setup_complete)
	{
		uplink_start_after_join();
	}
	else
	{
		MYLOG("JOIN-CB", "Setup not complete — timer and first TX deferred to setup()");
	}
}

/** Callback after a packet was received (downlink) */
static void receiveCallback(SERVICE_LORA_RECEIVE_T *data)
{
	MYLOG("RX-CB", "RX, port %d, DR %d, RSSI %d, SNR %d", data->Port, data->RxDatarate, data->Rssi, data->Snr);

#if MY_DEBUG == 1
	Serial.printf("[RX-CB] Payload (%d bytes):", data->BufferSize);
	for (int i = 0; i < data->BufferSize; i++)
	{
		Serial.printf(" %02X", data->Buffer[i]);
	}
	Serial.println();
#endif

	AccParams params = {interrupt_threshold, acc_frequency, interrupt_duration};
	switch (downlink::apply(data->Port, data->Buffer, data->BufferSize, params))
	{
	case DL_EMPTY:
		MYLOG("RX-CB", "Empty payload received (network acknowledgment)");
		return;
	case DL_TOO_SHORT:
		MYLOG("RX-CB", "Invalid payload size: %d (minimum 3 bytes required)", data->BufferSize);
		return;
	case DL_BAD_DIGIT:
		MYLOG("RX-CB", "Payload is not 3 ASCII digits");
		return;
	case DL_OUT_OF_RANGE:
		MYLOG("RX-CB", "Value out of range for port %d, ignored", data->Port);
		return;
	case DL_UNKNOWN_PORT:
		MYLOG("RX-CB", "Unknown port: %d", data->Port);
		return;
	case DL_APPLIED:
		break;
	}

	interrupt_threshold = params.threshold;
	acc_frequency = params.frequency;
	interrupt_duration = params.duration;
	MYLOG("RX-CB", "Updated ACC config: threshold=%d freq=%d duration=%d", params.threshold, params.frequency, params.duration);

	// Defer setup_rak1904() to sensor_handler — never call I2C from inside the LoRa stack callback.
	// Do NOT guard on sensor_present() here: a downlink can arrive during init_rak1904()
	// (before the sensor is marked present). sensor_handler guards before applying.
	acc_config_pending = true;
}

/** Callback for LinkCheck result */
static void linkcheckCallback(SERVICE_LORA_LINKCHECK_T *data)
{
	MYLOG("LC_CB", "%s Margin %d GW# %d RSSI%d SNR %d", data->State == 0 ? "Success" : "Failed",
		  data->DemodMargin, data->NbGateways,
		  data->Rssi, data->Snr);
}

/** Callback after TX is finished */
static void sendCallback(int32_t status)
{
	if (status == 0)
	{
		MYLOG("TX-CB", "Transmission successful - ready for sleep");
	}
	else
	{
		MYLOG("TX-CB", "Transmission failed with status %d", status);
	}
	digitalWrite(LED_BLUE, LOW);
	// RUI3 nRF52840 restarts BLE advertising without a timeout after each TX.
	// Re-arm with a 1 s auto-stop to prevent the ~425 uA stuck-advertise state.
	// No-op on STM32WLE5.
	platform_ble_rearm();
}

// ---------------------------------------------------------------------------
// LoRa P2P callbacks
// ---------------------------------------------------------------------------

static void recv_cb(rui_lora_p2p_recv_t data)
{
	MYLOG("RX-P2P-CB", "P2P RX, RSSI %d, SNR %d", data.Rssi, data.Snr);
	for (int i = 0; i < data.BufferSize; i++)
	{
		Serial.printf("%02X", data.Buffer[i]);
	}
	Serial.print("\r\n");
}

static void send_cb(void)
{
	MYLOG("TX-P2P-CB", "P2P TX finished");
	digitalWrite(LED_BLUE, LOW);
}

static void cad_cb(bool result)
{
	MYLOG("CAD-P2P-CB", "P2P CAD reports %s", result ? "activity" : "no activity");
}

// ---------------------------------------------------------------------------
// Stack configuration
// ---------------------------------------------------------------------------

/** True if buf[0..len) contains at least one non-zero byte. */
static bool any_nonzero(const uint8_t *buf, size_t len)
{
	for (size_t i = 0; i < len; i++)
	{
		if (buf[i] != 0)
		{
			return true;
		}
	}
	return false;
}

/** AppEUI + AppKey must be provisioned (non-zero) before a join makes sense. */
static bool otaa_credentials_ok(void)
{
	uint8_t key_buf[16] = {0};
	bool ok = true;

	api.lorawan.appeui.get(key_buf, 8);
	if (!any_nonzero(key_buf, 8))
	{
		MYLOG("SETUP", "ERROR: AppEUI is all-zeros — not provisioned! Use AT+APPEUI");
		ok = false;
	}

	memset(key_buf, 0, sizeof(key_buf));
	api.lorawan.appkey.get(key_buf, 16);
	if (!any_nonzero(key_buf, 16))
	{
		MYLOG("SETUP", "ERROR: AppKey is all-zeros — not provisioned! Use AT+APPKEY");
		ok = false;
	}
	return ok;
}

static void start_otaa_join(void)
{
	// Join profile: ADR off, max TX power, robust DR — best chance of first join
	api.lorawan.adr.set(false);
	MYLOG("SETUP", "ADR=off for join");

	api.lorawan.txp.set(JOIN_MAX_TXP);
	MYLOG("SETUP", "TXP=%d (max EIRP)", JOIN_MAX_TXP);

	api.lorawan.dr.set(JOIN_DR);
	MYLOG("SETUP", "Join DR=%d", JOIN_DR);

	// Start join; RUI3 valid interval range: 7-255 s. Count 255 = retry ~35 h before stopping.
	// api.lorawan.join() returns false when a cached OTAA session is already being
	// restored in the background — joinCallback will still fire with status=0.
	// Check njs to distinguish "already joining" from a genuine failure.
	if (!api.lorawan.join(1, 1, JOIN_RETRY_SEC, 255))
	{
		MYLOG("SETUP", "join() returned false — cached session restore in progress or stack busy. Waiting for joinCallback...");
	}
	else
	{
		MYLOG("SETUP", "Join started — retry every %d s, up to 255 attempts", JOIN_RETRY_SEC);
	}
}

void lorawan_setup(void)
{
	if (api.lorawan.nwm.get() != 1)
	{
		// LoRa P2P
		api.lora.registerPRecvCallback(recv_cb);
		api.lora.registerPSendCallback(send_cb);
		api.lora.registerPSendCADCallback(cad_cb);
		return;
	}

	// Read AT-configurable TX settings (CFM/RETY remain user-settable via AT commands)
	g_confirmed_mode = api.lorawan.cfm.get();
	g_confirmed_retry = api.lorawan.rety.get();
	g_data_rate = api.lorawan.dr.get();

	// Setup the callbacks for joined and send finished
	api.lorawan.registerRecvCallback(receiveCallback);
	api.lorawan.registerSendCallback(sendCallback);
	api.lorawan.registerJoinCallback(joinCallback);
	api.lorawan.registerLinkCheckCallback(linkcheckCallback);

	// --- Join configuration: enforced in code each boot ---

	// OTAA join mode (overrides any ABP set via AT+NJM)
	api.lorawan.njm.set(1);
	MYLOG("SETUP", "Join mode: OTAA");

	// Region EU868 (overrides AT+BAND; change LORA_REGION define in config.h)
	api.lorawan.band.set(LORA_REGION);
	MYLOG("SETUP", "Region set to %d (EU868)", LORA_REGION);

	// Enforce Class A (overrides any Class B/C set via AT+CLASS)
	api.lorawan.deviceClass.set(RAK_LORA_CLASS_A);
	MYLOG("SETUP", "Class A enforced");

	if (!otaa_credentials_ok())
	{
		MYLOG("SETUP", "OTAA credentials missing — join skipped. Provision with AT+APPEUI / AT+APPKEY");
		return;
	}
	MYLOG("SETUP", "OTAA credentials verified");

	if (api.lorawan.njs.get())
	{
		// Already joined via cached session (joinCallback fired before this point).
		// Do NOT call join() again — it would overwrite the ADR/TXP set by
		// joinCallback, kick off a redundant OTAA exchange, and if that fails
		// (JOIN_FAILED_RX_TIMEOUT) the cached session is lost.
		MYLOG("SETUP", "Already joined (cached session) — skipping join setup");
		return;
	}

	start_otaa_join();
}

void lorawan_log_tx_settings(void)
{
	if (api.lorawan.nwm.get() == 1)
	{
		MYLOG("SETUP", "CFM=%s, RETY=%d (AT-configurable)",
			  g_confirmed_mode ? "confirmed" : "unconfirmed", g_confirmed_retry);
	}
}

/**
 * If the join already completed while setup() was running (cached credentials),
 * joinCallback saw setup_complete=false and deferred. Start the timer and
 * trigger the first TX now. Guard on timer_started: if joinCallback fired in the
 * narrow window after setup_complete=true but before this check, it already
 * armed the timer — skip to avoid a double-start.
 */
void lorawan_resume_cached_join(void)
{
	if (api.lorawan.nwm.get() == 1 && api.lorawan.njs.get() && !timer_started)
	{
		MYLOG("SETUP", "Already joined — starting send timer and triggering first transmission");
		if (custom_parameters.send_interval != 0)
		{
			api.lorawan.adr.set(true);
			api.lorawan.txp.set(POST_JOIN_TXP); // same moderate baseline as joinCallback path
		}
		uplink_start_after_join();
	}
}
