# AFC Wiring Guide — Per-Lane (ESP32-S3-Zero)

> This guide covers the per-lane approach: one ESP32-S3-Zero + PN532 per BoxTurtle lane. For the alternative single-ESP32 4-reader approach, see [hardware-approaches.md](hardware-approaches.md).

## Overview

Each lane gets its own ESP32-S3-Zero and PN532 module. The wiring per board is identical to the toolchanger setup — the only difference is the power source (AFC-Lite 5V rail instead of USB).

## PN532 DIP Switch Settings (I2C Mode)

Set the DIP switches on every PN532 board:
- Switch 1: **ON**
- Switch 2: **OFF**

## PN532 to ESP32-S3-Zero Wiring

| PN532 Pin | ESP32-S3-Zero Pin |
|-----------|-------------------|
| VCC       | 3V3               |
| GND       | GND               |
| SDA       | GPIO1             |
| SCL       | GPIO2             |

> **Note:** The 3.3V pin on the Waveshare ESP32-S3-Zero is labeled **3V3** on the board. Connect VCC to **3V3**, not 5V — the PN532 operates at 3.3V logic.

All four lane boards use the same GPIO pins — each is a separate device so there's no conflict.

## Power

Each ESP32-S3-Zero can be powered from the AFC-Lite 5V rail via its **5V** pin (or USB-C). The PN532 is then powered from the ESP32's 3V3 output as shown in the table above.

```
AFC-Lite 5V ── ESP32-S3-Zero 5V pin
AFC-Lite GND ── ESP32-S3-Zero GND
                    │
                    ├── PN532 VCC (via 3V3 output)
                    └── PN532 GND
```

> Test with dupont wires before soldering. Get the ESP32-S3-Zero version **without pins** and solder wires directly for the best fit in the printed tray.

## I2C Address

The PN532 should appear at address **0x24** on the I2C bus.
Verify in ESPHome logs after flashing:
```
Results from bus scan:
Found i2c device at address 0x24
```

## Notes

- One ESP32-S3-Zero + PN532 per lane (4 sets total for a standard BoxTurtle)
- No onboard LED is used for AFC — lane feedback comes from BoxTurtle LEDs via the Klipper macro
- The ESP32-S3-Zero does have a WS2812 LED on GPIO21 but it is not configured in `lane-pn532.yaml`
