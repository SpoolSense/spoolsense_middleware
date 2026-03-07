# AFC / BoxTurtle NFC Integration

> ⚠️ **Experimental** — this is a new addition and has not been tested on hardware yet. The middleware and ESPHome configs are functional but the physical PN532 mounting inside a BoxTurtle has not been validated. Feedback welcome!

NFC spool scanning for [BoxTurtle](https://github.com/ArmoredTurtle/BoxTurtle) and other AFC-based filament changers. One ESP32 drives 4 PN532 NFC readers (one per lane) and integrates with the [AFC-Klipper Add-On](https://github.com/ArmoredTurtle/AFC-Klipper-Add-On) via Spoolman.

## How It Works

Place a spool on a BoxTurtle lane → NFC tag rotates into the reader → middleware looks up the spool in Spoolman → calls `SET_SPOOL_ID` in AFC → AFC pulls color, material, weight automatically → lane is locked to prevent repeat scans during printing.

## What's Here

```
afc/
├── esphome/
│   └── boxturtle-nfc.yaml    # ESPHome config: 1 ESP32, 4 PN532 readers
├── middleware/
│   ├── nfc_listener.py       # AFC-aware middleware (AMS mode)
│   ├── config.example.yaml   # Config template for AMS mode
│   └── nfc-spoolman.service  # Systemd service file
└── docs/
    ├── setup.md              # Full setup guide
    └── wiring.md             # Wiring guide with pin assignments
```

## Quick Start

1. Wire 4 PN532 modules to an ESP32 DevKit ([wiring guide](docs/wiring.md))
2. Flash ESPHome with `boxturtle-nfc.yaml`
3. Deploy the middleware with AMS mode config
4. Place tagged spools on the respoolers — they auto-identify

See [docs/setup.md](docs/setup.md) for the full walkthrough.

## Hardware

- 1x ESP32-WROOM DevKit (any esp32dev compatible board)
- 4x PN532 NFC Module (I2C mode)
- NFC tags on each spool
- Power from AFC-Lite 5V rail

## Requirements

- BoxTurtle with AFC-Klipper Add-On installed
- Spoolman with `nfc_id` extra field configured
- Klipper + Moonraker
- MQTT broker (Mosquitto via Home Assistant)
