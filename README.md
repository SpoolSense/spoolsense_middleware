# Voron NFC Spoolman Integration

Automatic filament spool tracking for Voron printers using NFC tags, ESP32-S3, and Spoolman. Built for multi-toolhead setups, works just as well with a single toolhead.
![IMG_2287](https://github.com/user-attachments/assets/66cfed3d-6f2a-4ca7-aada-d6a62300f70d)

> 🚧 **Work in Progress**
> This project is a work in progress — honestly more of a guide than a finished product. Yes, there's custom code here, but the main idea is to leverage as much open-source software and tooling as possible to build a working system. Check back often because there's a lot planned. Ideas and contributions are very welcome — open an issue or submit a PR!

> 🙋 **Help Wanted**
> - **Case Design** — I'm currently using a basic printed case and would love help designing something slicker. Ideally something that styles well with popular filament dryers — I'm running the Creality Space Pi X4, but open to designs that work across different dryers. If you're a CAD person, let's collaborate!
> - **Testers & Feedback** — If you build this or any variation of it, please open an issue and let me know how it went. Hardware differences, edge cases, things that didn't work — all of it is valuable.

> 🎯 **Stretch Goal — Bondtech INDX Support**
> A top priority stretch goal is to extend this project to support the [Bondtech INDX](https://www.bondtech.se/indx-by-bondtech/) system as soon as it's publicly available. INDX on a Klipper-based printer is a natural fit for this project — the architecture is already there. If you're from the Bondtech team and want to collaborate, feel free to open an issue or reach out via GitHub.

## Overview

Scan an NFC tag on a filament spool → automatically sets the active spool in Spoolman → updates Spoolman per toolhead → Klipper tracks filament usage → LED on each toolhead confirms the scan and displays the spool color.

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
Mainsail/Fluidd shows spool per toolhead
     ↓
LED flashes white 3x (scan confirmed) → holds spool color
```

## Features

Tap an NFC tag on your spool and walk away. That's really it — everything else happens automatically.

**Scan to track** — each toolhead has its own ESP32-S3 constantly listening for NFC tags. The moment you tap a spool, it's registered as active in Spoolman, Moonraker is updated, and your front end reflects it per toolhead. No typing spool IDs, no dropdown menus, no forgetting to update it.

**LED feedback that actually tells you something** — the onboard RGB LED isn't just an "it worked" light. It flashes the filament's exact color from Spoolman so you can glance at the toolhead and know what's loaded. Scan an unknown tag and it goes red. Running low (under 100g)? The LED starts breathing in the filament color so it catches your eye without being obnoxious.

**Survives reboots** — spool IDs are saved to disk via Klipper's save_variables system and restored automatically on startup. Pull the power, come back the next day, and everything is still assigned correctly.

**Scales from single to as many toolheads as you need** — originally built for multi-toolhead setups like MadMax, StealthChanger, and other Voron toolchanger systems, but there's nothing stopping you from running a single reader on a standard setup. Each toolhead is completely independent with its own reader and ESP32, but they all feed into the same Spoolman instance. Configure the `TOOLHEADS` list in the middleware to match your setup — `["T0"]` for single, or as many as you're running. Both Fluidd and Mainsail support per-toolhead spool status natively via `variable_spool_id` in the toolchange macros (Mainsail added this in July 2024).

**Single or toolchanger mode** — set `TOOLHEAD_MODE` in the middleware config to match your printer. Single mode calls `SET_ACTIVE_SPOOL` directly on every scan. Toolchanger mode stores the spool ID per toolhead and lets klipper-toolchanger handle activation automatically at each toolchange — tested and confirmed working on MadMax T0–T3.

**Printable case included** — a custom case is in the `3mf/` folder designed specifically for the Waveshare ESP32-S3-Zero + PN532. Print it clear so the LED glows through. Print the scan target in red so you know where to tap.

## LED Status Indicator
![LED Status Demo](IMG_2293.gif)

Each toolhead's onboard WS2812 RGB LED provides visual feedback. This project is tested with the **Waveshare ESP32-S3-Zero**, which includes an onboard WS2812 LED on GPIO21 — no additional wiring required for the LED.

- **3x white flash** — NFC tag successfully scanned
- **Solid spool color** — displays the filament color pulled from Spoolman after a successful scan
- **3x red flash** — tag scanned but not found in Spoolman (unknown or unregistered tag)
- **Breathing in spool color** — spool is running low (at or below `LOW_SPOOL_THRESHOLD`, default 100g)

The LED color is published via MQTT and driven by the middleware using the `color_hex` value stored in Spoolman for each spool.

## Hardware

- 4x [Waveshare ESP32-S3-Zero](https://www.waveshare.com/esp32-s3-zero.htm) *(tested and recommended — onboard WS2812 LED on GPIO21)*
- 4x PN532 NFC Module (I2C mode)
- 4x WS2812 RGB LED (onboard on Waveshare ESP32-S3-Zero, GPIO21)
- Raspberry Pi (Klipper host)
- NFC tags (one per spool)

## Software Stack

- [ESPHome](https://esphome.io) — firmware for ESP32-S3
- [Mosquitto MQTT](https://mosquitto.org) — via Home Assistant addon
- [Spoolman](https://github.com/Donkie/Spoolman) — filament database
- [Moonraker](https://moonraker.readthedocs.io) — Klipper API
- [Fluidd](https://fluidd.xyz) or [Mainsail](https://docs.mainsail.xyz) — both support per-toolhead spool display via `variable_spool_id` in toolchange macros.

## Prerequisites

- Home Assistant with ESPHome and Mosquitto addons
- Klipper + Moonraker running on Raspberry Pi
- Spoolman installed and running
- Fluidd or Mainsail installed

## 3D Printed Case

A custom case is included in the `3mf/` directory, modified from [this model on MakerWorld](https://makerworld.com/en/models/2108947-esp32-c3-pn532-nfc-reader-case-usb-c#profileId-2552448).

Modifications made:
- **Toolhead labels** — T0, T1, T2, T3 text added to each case
- **ESP32-S3-Zero fit** — modified bay designed specifically for the Waveshare ESP32-S3-Zero (get the version without pins and solder wires directly)
- **Scan target** — added a scan target area on the case, suggested to print in red filament

**Printing tips:**
- Print the case itself in a **clear material** (PLA, PETG, or PC) to let the onboard LED shine through for full visual effect
- Print the scan target insert in red for easy identification

## Directory Structure

```
├── esphome/          # ESPHome YAML configs for each toolhead
├── middleware/       # Python MQTT listener script
├── klipper/          # Klipper macro configs
├── 3mf/              # 3D printable case files
├── docs/             # Setup guides
├── scripts/          # Install script (beta)
└── beta/             # Beta features and design docs
```

## Quick Start

**Option A — Interactive install script (beta):**
```bash
git clone https://github.com/sjordan0228/nfc-toolchanger-spoolman.git
cd nfc-toolchanger-spoolman
bash scripts/install-beta.sh
```
The script walks you through configuration, installs dependencies, sets up the systemd service, and generates ESPHome YAML files for each scanner.

**Option B — Manual setup:**
1. Wire the PN532 to each ESP32-S3 (see docs/wiring.md)
2. Flash ESPHome configs (see docs/esphome-setup.md)
3. Deploy middleware script (see docs/middleware-setup.md)
4. Add Klipper macros (see docs/klipper-setup.md)
5. Configure Spoolman extra fields (see docs/spoolman-setup.md)

## License

GPL-3.0
