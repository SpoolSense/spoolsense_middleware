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
3. Middleware receives the scan and routes it based on the scanner's configured action
4. If Spoolman is configured, the spool is looked up or created and synced
5. Spool data (color, material, weight) is sent to Klipper — with or without Spoolman
6. For shared scanner modes (`afc_stage`, `toolhead_stage`), tag data is cached until a lane loads or a tool is picked up

## Supported Setups

Each scanner is configured with an **action** that determines how scans are routed:

| Action | Description |
|--------|-------------|
| **`afc_stage`** | Shared scanner for AFC (BoxTurtle, NightOwl). Scan a spool, load into any lane — AFC assigns automatically. One scanner for all lanes. |
| **`afc_lane`** | Dedicated scanner per AFC lane. Locks scanner until lane is cleared. |
| **`toolhead_stage`** | Shared scanner for toolchanger printers (klipper-toolchanger). Scan a spool, pick up any tool — spool auto-assigns. One scanner for all toolheads. |
| **`toolhead`** | Dedicated scanner per toolhead. Sets active spool and saves to Klipper variables. |
| **`single`** | One scanner, one extruder. Spool activates on scan. |

Mixed configs are supported — for example, `afc_stage` for BoxTurtle lanes and `toolhead` for direct toolheads on the same printer.

## Features

- **Per-scanner action routing** — each scanner independently routes to AFC lanes, toolheads, or shared staging
- **Works with or without Spoolman** — tag data (color, material, weight) is sent directly when Spoolman is not configured
- **Slicer integration** — publish spool data (color, material, weight, nozzle/bed temps) to Moonraker's `lane_data` database. Orca Slicer and other slicers auto-populate tool info. Opt-in via `publish_lane_data: true`. For users without AFC or Happy Hare (they already provide this).
- **Extensible publisher architecture** — spool activation output is decoupled from Klipper/Moonraker. Adding support for new printer platforms (Prusa, Bambu, etc.) requires one new file.
- Automatic spool lookup and registration in Spoolman (when configured)
- Klipper spool activation (SET_ACTIVE_SPOOL, SET_SPOOL_ID, SET_NEXT_SPOOL_ID)
- AFC lane state sync via Moonraker API polling — no file watcher dependency
- Toolchanger state sync via Moonraker — detects tool pickups for `toolhead_stage`
- Low spool detection with LED breathing effect
- Spool ID persistence across reboots via Klipper save_variables
- Legacy config auto-migration — old `toolhead_mode` + `scanner_lane_map` configs are converted automatically
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
