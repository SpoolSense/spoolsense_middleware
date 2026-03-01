# Voron NFC Spoolman Integration

Automatic filament spool tracking for multi-toolhead Voron printers using NFC tags, ESP32-S3, and Spoolman.
![IMG_2287](https://github.com/user-attachments/assets/66cfed3d-6f2a-4ca7-aada-d6a62300f70d)


## Overview

Scan an NFC tag on a filament spool → automatically sets the active spool in Spoolman → updates Fluidd per toolhead → Klipper tracks filament usage.

```
Scan NFC tag
     ↓
ESP32-S3 + PN532 reads UID
     ↓
ESPHome publishes to MQTT
     ↓
Python middleware on Pi
     ↓
Spoolman lookup by UID
     ↓
Moonraker sets active spool
     ↓
Fluidd shows spool per toolhead
```

## Hardware

- 4x ESP32-S3 DevKitC-1
- 4x PN532 NFC Module (I2C mode)
- Raspberry Pi (Klipper host)
- NFC tags (one per spool)

## Software Stack

- [ESPHome](https://esphome.io) — firmware for ESP32-S3
- [Mosquitto MQTT](https://mosquitto.org) — via Home Assistant addon
- [Spoolman](https://github.com/Donkie/Spoolman) — filament database
- [Moonraker](https://moonraker.readthedocs.io) — Klipper API
- [Fluidd](https://fluidd.xyz) — web interface with per-toolhead spool support. Unfortunetly Mainsail only supports one active spool.

## Prerequisites

- Home Assistant with ESPHome and Mosquitto addons
- Klipper + Moonraker running on Raspberry Pi
- Spoolman installed and running
- Fluidd installed (see docs/fluidd-install.md)

## Directory Structure

```
├── esphome/          # ESPHome YAML configs for each toolhead
├── middleware/       # Python MQTT listener script
├── klipper/          # Klipper macro configs
└── docs/             # Setup guides
```

## Quick Start

1. Wire the PN532 to each ESP32-S3 (see docs/wiring.md)
2. Flash ESPHome configs (see docs/esphome-setup.md)
3. Deploy middleware script (see docs/middleware-setup.md)
4. Add Klipper macros (see docs/klipper-setup.md)
5. Configure Spoolman extra fields (see docs/spoolman-setup.md)

## License

GPL-3.0
