<p align="center">
  <img src="docs/spoolsense-logo.png" width="200" alt="SpoolSense">
</p>

# SpoolSense Middleware

Python middleware that bridges NFC spool scanning to your 3D printer. Receives tag data from [SpoolSense Scanner](https://github.com/SpoolSense/spoolsense_scanner) via MQTT and orchestrates Klipper, Spoolman, and Home Assistant.

## How It Works

```
SpoolSense Scanner → MQTT → Middleware → Klipper / Spoolman / Home Assistant
```

1. Scanner reads an NFC tag on a filament spool
2. Tag data is published to MQTT as a JSON payload
3. Middleware receives the scan, looks up or creates the spool in Spoolman
4. Middleware activates the spool in Klipper (SET_ACTIVE_SPOOL or SET_SPOOL_ID)
5. For AFC setups, lane LEDs update to show the filament color (requires [AFC-Klipper-Add-On PR #681](https://github.com/ArmoredTurtle/AFC-Klipper-Add-On/pull/681))

## Supported Modes

| Mode | Description |
|------|-------------|
| **Single Toolhead** | One scanner, one extruder. Spool activates on scan. |
| **Toolchanger** | One scanner per toolhead (T0–T3+). Spool IDs tracked per tool. |
| **AFC / BoxTurtle** | One scanner per lane. Scan-lock-clear lifecycle with AFC-Klipper-Add-On. |

## Features

- Automatic spool lookup and registration in Spoolman
- Klipper spool activation (SET_ACTIVE_SPOOL for toolchanger/single, SET_SPOOL_ID for AFC)
- Low spool detection with LED breathing effect
- AFC file watcher — monitors lane state changes and manages scan lock/clear
- Spool ID persistence across reboots via Klipper save_variables
- Home Assistant online/offline status via MQTT Last Will
- Moonraker update_manager compatible for automatic updates

## Installation

### Recommended: SpoolSense Installer

```bash
curl -sL https://raw.githubusercontent.com/SpoolSense/spoolsense-installer/main/install.sh -o /tmp/install.sh && bash /tmp/install.sh
```

The installer handles cloning, config generation, dependency installation, and systemd service setup.

### Manual Setup

See [docs/middleware-setup.md](docs/middleware-setup.md) for step-by-step manual installation.

## Configuration

Config lives in `~/SpoolSense/config.yaml` (never overwritten by updates). Templates for each mode:

- `middleware/config.example.single.yaml`
- `middleware/config.example.toolchanger.yaml`
- `middleware/config.example.afc.yaml`

Validate your config without starting the service:

```bash
python3 ~/SpoolSense/middleware/spoolsense.py --check-config
```

## Automatic Updates via Moonraker

Add to your `moonraker.conf`:

```ini
[update_manager spoolsense]
type: git_repo
path: ~/SpoolSense
origin: https://github.com/SpoolSense/spoolsense_middleware.git
primary_branch: master
managed_services: spoolsense
```

## Requirements

- Python 3.6+
- MQTT broker (e.g. Mosquitto — Home Assistant users typically have this already)
- Klipper + Moonraker
- Spoolman (optional but recommended)
- [SpoolSense Scanner](https://github.com/SpoolSense/spoolsense_scanner) hardware

## Documentation

See the [docs/](docs/) folder for setup guides:

- [Middleware Setup](docs/middleware-setup.md) — manual installation and configuration
- [Klipper Setup](docs/klipper-setup.md) — macros, spool persistence, toolhead config
- [Spoolman Setup](docs/spoolman-setup.md) — extra fields, low spool warnings
- [AFC Setup](docs/afc-setup.md) — BoxTurtle lane scanning and LED colors

## Related Repos

- [spoolsense_scanner](https://github.com/SpoolSense/spoolsense_scanner) — ESP32 NFC scanner firmware
- [spoolsense-installer](https://github.com/SpoolSense/spoolsense-installer) — Interactive setup CLI
