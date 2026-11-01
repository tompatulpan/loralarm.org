/*
 Externall pull-up of 1M ohm between Vcc and IO1 can be used to save power
 Deep sleep ~25uA with open sw_in_pin and ~100uA (i.e ~35kohm)closed usin internall pull-up "pinMode(SW_INT_PIN, INPUT_PULLUP)"

*/

#include "app.h"

// switch stuff
// uint8_t GPIO_PIN = WB_IO1;
volatile bool switch_state = false;
volatile bool interrupt_triggered = false;
volatile uint32_t last_int_time = 0;
const uint32_t SWITCH_DEBOUNCE = 50;    // 50ms debounce time
const uint32_t SWITCH_CHECK_TIME = 100;  // 100ms for checking switch state

/**
 * @brief Initialize the GPIO interrupt system
 * @return true if initialization was successful
 */
bool init_gpio()
{
    // Create timers for handling the events
    api.system.timer.create(RAK_TIMER_2, switch_bounce_check, RAK_TIMER_ONESHOT);
    api.system.timer.create(RAK_TIMER_3, handle_gpio, RAK_TIMER_ONESHOT);

    MYLOG("GPIO", "Initialize Interrupt on pin %d", SW_INT_PIN);
    // INPUT_PULLUP uses the internal ~35kohm pull-up. When the switch is closed (pin pulled to GND)
    // this draws ~94uA, raising sleep current from ~25uA to ~100uA.
    // Alternative: fit a 1Mohm external pull-up between VCC and WB_IO1 and use INPUT instead,
    // reducing the closed-switch leakage to ~3.3uA.
    pinMode(SW_INT_PIN, INPUT_PULLUP);
    attachInterrupt(SW_INT_PIN, switch_int_handler, CHANGE);
    MYLOG("GPIO", "Interrupt Initialized");

    return true;
}

/**
 * @brief Interrupt handler for GPIO pin
 */
void switch_int_handler(void)
{
    uint32_t now = millis();
    if ((now - last_int_time) > SWITCH_DEBOUNCE)
    {
        switch_state = (bool)digitalRead(SW_INT_PIN); // capture state at interrupt time
        interrupt_triggered = true;
        last_int_time = now;
        // Start the bounce check timer
        api.system.timer.start(RAK_TIMER_2, SWITCH_CHECK_TIME, NULL);
    }
}

/**
 * @brief Check switch state after debounce time
 */
void switch_bounce_check(void* unused)
{
    if (interrupt_triggered)
    {
        switch_state = digitalRead(SW_INT_PIN);
        // Start the handler timer
        api.system.timer.start(RAK_TIMER_3, 10, NULL);
    }
}

/**
 * @brief Handle the GPIO event
 */
void handle_gpio(void* unused)
{
    if (interrupt_triggered)
    {
        interrupt_triggered = false;
      
      #if MY_DEBUG == 1
	      digitalWrite(LED_BLUE, HIGH);
      #endif
        MYLOG("GPIO", "Switch state changed to %s", switch_state ? "HIGH" : "LOW");
        
        // Clear payload and prepare new data
        g_solution_data.reset();
        g_solution_data.addDigitalOutput(LPP_CHANNEL_DIG_5, switch_state ? 0x01 : 0x00);
        previous_switch_state = switch_state;
        
        // Send the packet
        send_packet();
    }
}
