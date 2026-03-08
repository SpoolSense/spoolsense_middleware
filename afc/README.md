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
│   ├── nfc_listener.py       # Unified middleware (single, toolchanger, and AMS modes)
│   ├── config.example.yaml   # Config template with all three modes documented
│   └── nfc-spoolman.service  # Systemd service file
├── stl/
│   └── Tray_plain_pn532.stl  # Modified BoxTurtle tray with PN532 mount
└── docs/
    ├── setup.md              # Full setup guide
    └── wiring.md             # Wiring guide with pin assignments
```

> **Note:** The middleware in `afc/middleware/` is the unified version — it supports all three toolhead modes (`single`, `toolchanger`, `ams`) via the `toolhead_mode` setting in `config.yaml`. You don't need a separate middleware for each mode.

## 3D Printed Tray

The `stl/` folder contains a modified BoxTurtle tray (`Tray_plain_pn532.stl`) with a built-in PN532 mounting area and a cable routing hole for connecting to the ESP32. This is a hack of the original BoxTurtle plain tray — it works but could use refinement. See the call for help below!

## 🙏 Help Wanted — Testers & CAD Contributors

This project is in early development and we need your help:

- **Testers** — If you have a BoxTurtle and some PN532 modules, we'd love for you to try this out and report back. Does the tray fit? Does the PN532 reliably read tags through the spool? What's the scan distance like? Open an issue with your findings.
- **CAD help** — The current tray STL is a functional hack, not a polished design. If you have CAD skills (Fusion 360, SolidWorks, FreeCAD, etc.) and want to help improve the PN532 mount, cable routing, or overall fit, contributions are very welcome. The tray needs proper parametric source files, better PN532 retention, and cleaner cable management.

If you're interested in contributing, open an issue or submit a PR — all skill levels welcome.

## Quick Start

1. Wire 4 PN532 modules to an ESP32 DevKit ([wiring guide](docs/wiring.md))
2. Print the modified tray from `stl/Tray_plain_pn532.stl`
3. Flash ESPHome with `boxturtle-nfc.yaml`
4. Deploy the middleware with AMS mode config
5. Place tagged spools on the respoolers — they auto-identify

See [docs/setup.md](docs/setup.md) for the full walkthrough.

## Hardware

- 1x ESP32-WROOM DevKit (any esp32dev compatible board)
- 4x PN532 NFC Module (I2C mode)
- NFC tags on each spool
- Power from AFC-Lite 5V rail
- 4x Modified BoxTurtle tray (print from `stl/Tray_plain_pn532.stl`)

## Requirements

- BoxTurtle with AFC-Klipper Add-On installed
- Spoolman with `nfc_id` extra field configured
- Klipper + Moonraker
- MQTT broker (Mosquitto via Home Assistant)
