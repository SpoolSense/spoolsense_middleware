# Proposal: Optional RGB Status LED for PN5180 Scanners

## Summary

Add support for a **single RGB status LED** (for example, a WS2812B or SK6812) to `spoolsense_scanner` so PN5180-based scanners can provide the same kind of visual status feedback that PN532 + ESPHome setups already provide.

This is especially useful for:

- **single toolhead** printers
- **multi-toolhead / toolchanger** setups
- any PN5180 scanner setup **without an LCD**

The goal is to let the scanner visually show:

- booting / connecting state
- Wi-Fi failure
- tag detection
- successful tag parse
- write success / write failure
- the **actual filament color** after a valid scan

---

## Why This Is Useful

Right now, PN5180 + ESP32-WROOM-32 scanners can read and write tags and publish over MQTT, but there is **no built-in LED status path** in `spoolsense_scanner`.

From reviewing the firmware:

- there is no `LEDManager`
- no `FastLED`, `Adafruit_NeoPixel`, `NeoPixelBus`, etc.
- no existing RGB pixel control path
- no GPIO status LED logic
- display feedback currently assumes the LCD path

That leaves PN5180 users with no simple visual scanner feedback unless they also wire in the LCD.

An RGB LED would provide a lightweight, low-cost, low-wiring alternative.

---

## Recommended Hardware

### Preferred option
One **addressable RGB LED** per scanner:

- **WS2812B**
- **SK6812**

### Why an addressable RGB LED
Compared to a single-color LED, this allows:

- error states
- success states
- scanning feedback
- filament color display

### Wiring concept
Per scanner:

- 1 × ESP32
- 1 × PN5180
- 1 × WS2812B or SK6812 LED

Typical connections:

- LED VCC → 5V
- LED GND → GND
- LED DIN → GPIO4 (recommended default — avoids strapping pins and input-only pins)
- common ground required

### GPIO pin guidance
Avoid the following ESP32 pins for the LED data line:

- **GPIO0, GPIO2, GPIO5, GPIO12, GPIO15** — strapping pins, can affect boot behavior
- **GPIO34, GPIO35, GPIO36, GPIO39** — input-only, cannot drive output

GPIO4 is a safe, recommended default. Any other available output-capable GPIO works.

### Electrical note
WS2812 data is typically 5V logic, while ESP32 GPIO is 3.3V. In many short-wire setups this works fine, but a level shifter is the safest option if signal integrity becomes an issue.

---

## Design Goals

1. **Optional feature**
   - firmware should still run without an LED

2. **Scanner-local control**
   - LED behavior should be handled inside `spoolsense_scanner`
   - not controlled by SpoolSense over MQTT in the first version

3. **Minimal wiring**
   - one data pin only

4. **Clear visual states**
   - status should be obvious at a glance

5. **Filament color display**
   - after a successful scan, LED can show the spool color

---

## Library Choice

`Adafruit_NeoPixel` integrates cleanly with this Arduino-based firmware and is recommended for the initial implementation.

`spoolsense_scanner` uses:

```ini
platform = espressif32
board = esp32dev
framework = arduino
```

Add it to `platformio.ini`:

```ini
lib_deps =
    adafruit/Adafruit NeoPixel
```

---

## Recommended Architecture

Add a small `LEDManager` to the firmware.

### Responsibilities
- initialize the RGB LED
- set colors for known states
- flash for transient events (tag detect, write success/failure)
- support setting filament color from scanned tag data
- allow the LED feature to be compiled out or disabled

### Why local LED control is better than MQTT-driven LED control
The scanner already knows:

- when it is booting
- whether Wi-Fi connected
- whether a tag is present
- whether parsing succeeded
- whether a write succeeded or failed
- what filament color was read

So the scanner can provide better immediate status than an external controller.

---

## Suggested Visual Behavior

The first version should use simple instantaneous color changes and brief blocking flashes. No animation engine, no `tick()`, no FreeRTOS LED task.

| Event | LED behavior |
|---|---|
| booting | white |
| Wi-Fi failed | red |
| tag detected | yellow flash |
| valid scan | solid filament color |
| parse failed | red flash |
| write success | green flash, return to filament color |
| write failed | red flash |

Animations (pulse, breathe, idle dimming) are intentionally deferred to a future version. Keeping the first PR to simple `setColor()` / `flash()` calls keeps the diff small and the review easy.

---

## Implementation Sketch

### 1. Add an `LEDManager`

Suggested files:

- `src/LEDManager.h`
- `src/LEDManager.cpp`

### Suggested API

```cpp
class LEDManager {
public:
    bool begin(int dataPin, int pixelCount = 1);
    void showOff();

    void showBooting();
    void showWifiFailed();
    void showReady();

    void flashTagDetected();
    void flashParseFailed();
    void flashWriteSuccess();
    void flashWriteFailure();

    void showFilamentColor(uint32_t rgb);
    void setFilamentColorFromHex(const char* hex);

private:
    void setColor(uint8_t r, uint8_t g, uint8_t b);
    Adafruit_NeoPixel pixel;
};
```

No `tick()` and no FreeRTOS task in the first version. All state changes are direct calls.

### Internal implementation example

```cpp
void LEDManager::setColor(uint8_t r, uint8_t g, uint8_t b) {
    pixel.setPixelColor(0, pixel.Color(r, g, b));
    pixel.show();
}

void LEDManager::flashTagDetected() {
    setColor(255, 200, 0);  // yellow
    delay(100);
    setColor(0, 0, 0);
}

void LEDManager::showFilamentColor(uint32_t rgb) {
    pixel.setPixelColor(0, rgb);
    pixel.show();
}
```

> **Note on `tick()` and animations:** If a future version adds time-based animations (pulse, blink, idle dimming), `tick()` should be driven from a dedicated FreeRTOS task rather than `loop()`, since this firmware uses FreeRTOS tasks extensively. That is out of scope for this PR.

---

### 2. Make LED support optional

#### Compile-time flag (recommended for first version)

In `main.cpp` or a shared config header:

```cpp
#define USE_STATUS_LED 1
#define STATUS_LED_PIN 4   // GPIO4 — safe default, change to match your wiring
```

Then guard initialization and usage:

```cpp
#if USE_STATUS_LED
#include "LEDManager.h"
LEDManager ledManager;
#endif
```

This keeps existing builds completely unaffected.

#### Option B: runtime config (future enhancement)

A later version could add web UI config for `led_enabled` and `led_pin`. Not required for the first PR.

---

### 3. Integrate in `main.cpp`

```cpp
#if USE_STATUS_LED
  ledManager.begin(STATUS_LED_PIN);
  ledManager.showBooting();
#endif
```

On Wi-Fi failure:

```cpp
#if USE_STATUS_LED
  ledManager.showWifiFailed();
#endif
```

On ready:

```cpp
#if USE_STATUS_LED
  ledManager.showReady();
#endif
```

---

### 4. Hook into scan / spool events

Best places to update the LED are likely:

- tag detected
- spool parsed successfully
- spool updated
- write success / failure

Potential files to integrate with:

- `ApplicationManager.cpp`
- `NFCManager.cpp`
- possibly `HomeAssistantManager.cpp` for write-command feedback

### Suggested behavior hook examples

#### On valid spool detected

Note: `HomeAssistantManager` publishes `color` as `#RRGGBB` — the hex parser must handle both `#RRGGBB` and `RRGGBB` formats:

```cpp
ledManager.setFilamentColorFromHex(spool.tag_data.color_hex);
```

#### On tag detection before parse

```cpp
ledManager.flashTagDetected();
```

#### On write success / failure

```cpp
ledManager.flashWriteSuccess();
ledManager.flashWriteFailure();
```

---

### 5. Hex color conversion helper

The firmware's `HomeAssistantManager` publishes `color` as `#RRGGBB`. The parser must handle both formats:

```cpp
bool parseHexColor(const char* hex, uint8_t& r, uint8_t& g, uint8_t& b);
```

Should support:

- `"ff0000"`
- `"#ff0000"`
- `"FF0000"`
- `"#FF0000"`

Fallback: invalid or empty color → white or off.

---

## Why This Should Be a Good PR

This feature would:

- make PN5180 scanner builds easier to use without the LCD
- provide immediate visual scanner feedback
- preserve the current architecture
- stay optional
- require very little hardware

It also fits well with the existing firmware design — `ApplicationManager` already tolerates optional UI concepts and the scanner logic already knows enough to drive visual feedback locally.

---

## Non-Goals for the First Version

These are intentionally **out of scope** for the initial PR:

- `tick()` / animation engine / FreeRTOS LED task
- pulse, blink, or idle dimming effects
- MQTT-controlled LED color
- multi-pixel animations
- per-lane external LED strips
- replacing AFC-native LED handling
- syncing LEDs from SpoolSense

The first version should stay simple:

- one LED
- one scanner
- direct color calls only
- local status only

---

## Suggested First Milestone

Implement the smallest useful version:

- optional `LEDManager` with compile-time flag (`USE_STATUS_LED`)
- recommended default pin: GPIO4
- `Adafruit_NeoPixel` library
- direct `setColor()` / `flash()` calls — no animation engine
- basic states: booting, Wi-Fi failed, tag detected, valid scan → filament color, write success/failure

That would already make the scanner much friendlier to use in single-toolhead and multi-toolhead setups without requiring an LCD.

---

## Suggested PR Title

**Add optional RGB status LED support for PN5180 scanner builds**

---

## Suggested PR Summary

This PR adds optional support for a single addressable RGB status LED (WS2812B/SK6812) to `spoolsense_scanner`.

The LED is intended as a lightweight alternative to the LCD for PN5180-based builds and provides local visual feedback for:

- boot / Wi-Fi status
- tag detection
- parse success/failure
- write success/failure
- filament color display after a valid scan

The feature is optional and controlled via a compile-time flag (`USE_STATUS_LED`) so existing builds are unaffected. The first version uses simple direct color calls with no animation engine — animations can be added in a follow-up PR using a FreeRTOS task for `tick()`.
