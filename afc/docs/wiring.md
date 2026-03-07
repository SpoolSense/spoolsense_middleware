# AFC Wiring Guide — 4 Lane Single ESP32

## Overview

One ESP32-WROOM DevKit drives 4 PN532 NFC readers, one per BoxTurtle lane.
Power comes from the AFC-Lite board's 5V rail. Each PN532 gets its own I2C
bus since they all share the same fixed address (0x24).

## PN532 DIP Switch Settings (all 4 modules)

Set the DIP switches on every PN532 board:
- Switch 1: **ON**
- Switch 2: **OFF**

## Power Wiring

The AFC-Lite board has a 5V buck converter. Tap the 5V rail from the fan
header or any available 5V point on the board.

**Do NOT power the PN532 modules through the ESP32** — wire them directly
to the AFC-Lite 5V rail. The ESP32 itself is also powered from this rail
via its VIN pin.

```
AFC-Lite 5V ──┬── ESP32 VIN (5V pin)
              ├── PN532 #1 VCC
              ├── PN532 #2 VCC
              ├── PN532 #3 VCC
              └── PN532 #4 VCC

AFC-Lite GND ─┬── ESP32 GND
              ├── PN532 #1 GND
              ├── PN532 #2 GND
              ├── PN532 #3 GND
              └── PN532 #4 GND
```

Use a Wago connector, terminal block, or solder junction as a distribution
point for the 5V and GND lines.

## I2C Wiring

Each PN532 connects to its own I2C bus on the ESP32. The first two use
hardware I2C controllers, the last two use software I2C (handled by ESPHome).

```
Lane 1 — Hardware I2C Bus 0:
  ESP32 GPIO21 (SDA) ── PN532 #1 SDA
  ESP32 GPIO22 (SCL) ── PN532 #1 SCL

Lane 2 — Hardware I2C Bus 1:
  ESP32 GPIO16 (SDA) ── PN532 #2 SDA
  ESP32 GPIO17 (SCL) ── PN532 #2 SCL

Lane 3 — Software I2C Bus:
  ESP32 GPIO25 (SDA) ── PN532 #3 SDA
  ESP32 GPIO26 (SCL) ── PN532 #3 SCL

Lane 4 — Software I2C Bus:
  ESP32 GPIO27 (SDA) ── PN532 #4 SDA
  ESP32 GPIO33 (SCL) ── PN532 #4 SCL
```

## Important Notes on GPIO Selection

The GPIO pins above were chosen to avoid boot-sensitive pins:
- **GPIO0, 2, 15** — affect boot mode, do not use for I2C
- **GPIO12** — affects flash voltage on some modules, avoid
- **GPIO6–11** — connected to internal flash, not usable
- **GPIO34–39** — input only, cannot be used for I2C (needs bidirectional)

If your ESP32 board uses a **WROVER** module (has PSRAM), **GPIO16 and
GPIO17 are not available** — they're used for PSRAM. In that case, use
these alternative pins for Lane 2:

```
Lane 2 (WROVER alternative):
  ESP32 GPIO32 (SDA) ── PN532 #2 SDA
  ESP32 GPIO14 (SCL) ── PN532 #2 SCL
```

## Complete Wiring Summary

| Lane | I2C Type | SDA Pin | SCL Pin | PN532 VCC | PN532 GND |
|------|----------|---------|---------|-----------|-----------|
| 1    | Hardware | GPIO21  | GPIO22  | AFC-Lite 5V | AFC-Lite GND |
| 2    | Hardware | GPIO16  | GPIO17  | AFC-Lite 5V | AFC-Lite GND |
| 3    | Software | GPIO25  | GPIO26  | AFC-Lite 5V | AFC-Lite GND |
| 4    | Software | GPIO27  | GPIO33  | AFC-Lite 5V | AFC-Lite GND |

ESP32 VIN → AFC-Lite 5V
ESP32 GND → AFC-Lite GND

## Total Wire Count

- 2 wires from AFC-Lite to distribution point (5V + GND)
- 2 wires from distribution point to ESP32 (VIN + GND)
- 4 wires per PN532 (VCC, GND, SDA, SCL) × 4 modules = 16 wires
- **Total: 20 wires**

## Testing

After flashing ESPHome, check the logs for all 4 I2C buses:

```
[i2c_lane1] Results from bus scan:
Found i2c device at address 0x24
[i2c_lane2] Results from bus scan:
Found i2c device at address 0x24
[i2c_lane3] Results from bus scan:
Found i2c device at address 0x24
[i2c_lane4] Results from bus scan:
Found i2c device at address 0x24
```

If a bus shows no devices, check wiring on that lane's PN532 (SDA/SCL
swapped is the most common mistake).

## PN532 Physical Mounting

The PN532 reader should be positioned inline with the spool on the
BoxTurtle respooler — so the NFC tag on the spool passes through the
read zone as the spool rotates. The PN532 reads at approximately 5cm
range. A custom mount will be needed to hold the reader in position
for each lane.
