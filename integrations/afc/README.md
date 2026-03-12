# AFC / BoxTurtle NFC Integration

> ⚠️ **Experimental** — this is a new addition and has not been tested on hardware yet. The middleware, ESPHome configs, and Klipper macros are functional but the physical PN532 mounting inside a BoxTurtle has not been validated.
>
> **Current status:** I have a BoxTurtle but it is not hooked up yet. My current printer is a Voron Trident with a MadMax 4-toolhead toolchanger running klipper-toolchanger — none of my toolheads have filament sensors. I need to research how to incorporate AFC-Klipper into my existing klipper-toolchanger setup before I can test the AMS mode end-to-end. The toolchanger and single modes are tested and working on my MadMax.
>
> 📸 **Pictures and build photos coming soon** — once the BoxTurtle is wired up and the PN532 trays are mounted, I'll add photos of the full setup, wiring, and tray fitment.
>
> Feedback and testing from the community is very welcome in the meantime!

NFC spool scanning for [BoxTurtle](https://github.com/ArmoredTurtle/BoxTurtle) and other AFC-based filament changers. One ESP32 drives 4 PN532 NFC readers (one per lane) and integrates with the [AFC-Klipper Add-On](https://github.com/ArmoredTurtle/AFC-Klipper-Add-On) via Spoolman.

## How It Works

Place a spool on a BoxTurtle lane → NFC tag rotates into the reader → middleware looks up the spool in Spoolman → calls `SET_SPOOL_ID` in AFC → AFC pulls color, material, weight automatically → lane is locked to prevent repeat scans during printing.

## What's Here

```
afc/
├── esphome/
│   └── boxturtle-nfc.yaml    # ESPHome config: 1 ESP32, 4 PN532 readers
├── klipper/
│   └── nfc_led_macro.cfg     # Klipper macro for BoxTurtle LED color override
├── middleware/
│   ├── spoolsense.py       # Unified middleware (single, toolchanger, and AMS modes)
│   ├── config.example.yaml   # Config template with all three modes documented
│   └── spoolsense.service  # Systemd service file
├── stl/
│   └── Tray_plain_pn532.stl  # Modified BoxTurtle tray with PN532 mount
└── docs/
    ├── setup.md              # Full setup guide
    └── wiring.md             # Wiring guide with pin assignments
```

> **Note:** The middleware in `afc/middleware/` is the unified version — it supports all three toolhead modes (`single`, `toolchanger`, `ams`) via the `toolhead_mode` setting in `config.yaml`. You don't need a separate middleware for each mode.

## 3D Printed Tray

The `stl/` folder contains a modified BoxTurtle tray (`Tray_plain_pn532.stl`) with a built-in PN532 mounting area and a cable routing hole for connecting to the ESP32. This is a hack of the original BoxTurtle plain tray — it works but could use refinement. See the call for help below!

![PN532 mounted in the modified BoxTurtle tray](docs/tray-pn532-mounted.jpg)

*Modified BoxTurtle tray with PN532 NFC Module V3 mounted in the recessed area. The 4-wire I2C + power cable routes out through the hole in the tray wall.*

> **ESP32 mount needed!** The tray handles the PN532 but there's currently no mount or enclosure for the ESP32 board itself. If you have ideas for where to mount it inside (or on the side of) the BoxTurtle, please share — a simple bracket or clip design would be a great contribution.

## 🙏 Help Wanted — Testers & CAD Contributors

This project is in early development and we need your help:

- **Testers** — If you have a BoxTurtle and some PN532 modules, we'd love for you to try this out and report back. Does the tray fit? Does the PN532 reliably read tags through the spool? What's the scan distance like? Open an issue with your findings.
- **CAD help** — The current tray STL is a functional hack, not a polished design. If you have CAD skills (Fusion 360, SolidWorks, FreeCAD, etc.) and want to help improve the PN532 mount, cable routing, or overall fit, contributions are very welcome. The tray needs proper parametric source files, better PN532 retention, and cleaner cable management.
- **ESP32 mount** — We need a mount or bracket for the ESP32 DevKit board inside (or on the side of) the BoxTurtle. Something that keeps it accessible for USB flashing but out of the way of the filament path. Even a simple zip-tie bracket or DIN rail clip would be great.

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
