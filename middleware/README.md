# Middleware Developer Guide

This directory contains the SpoolSense middleware — a Python service that bridges NFC tag scans from SpoolSense scanners to Klipper, Spoolman, and Home Assistant via MQTT.

## How It Works

```
SpoolSense Scanner (ESP32 + PN5180/PN532)
        ↓ MQTT
        spoolsense/<device_id>/tag/state
        ↓
SpoolSense Middleware (Python on printer host)
        ↓                    ↓                    ↓
   Moonraker/Klipper    Spoolman (optional)    Home Assistant
   (gcode, AFC, vars)   (spool tracking)       (MQTT status)
```

The scanner reads NFC tags (OpenPrintTag, OpenTag3D, TigerTag, UID-only), publishes rich JSON payloads to MQTT. The middleware receives scan events, resolves which scanner sent the data, enriches them with Spoolman data (color, material, weight), and activates the spool in Klipper via Moonraker — setting gcode variables, persisting spool IDs, and managing lock state for multi-scanner setups. It also monitors AFC lane state, handles tool assignment via the ASSIGN_SPOOL macro, tracks filament usage via the UPDATE_TAG macro, and serves the SpoolSense Mobile app via REST API.

## Directory Structure

```
middleware/
├── spoolsense.py              # Entry point — startup wiring, signal handlers
├── app_state.py               # Shared mutable state (config, caches, locks)
├── config.py                  # Config loading, validation, legacy migration
├── mqtt_handler.py            # MQTT callbacks — tag parsing, activation pipeline
├── activation.py              # Spool activation orchestrator — lock, cache, route
│
├── publishers/
│   ├── base.py                # SpoolEvent dataclass, Publisher ABC, Action enum
│   └── klipper.py             # KlipperPublisher — gcode commands, Moonraker REST
├── publisher_manager.py       # Fan-out dispatcher — primary + secondary publishers
│
├── afc_status.py              # AFC lane state sync via Moonraker (websocket/polling)
├── toolchanger_status.py      # ASSIGN_SPOOL macro polling for toolhead_stage
├── toolhead_status.py         # Toolhead spool eject detection
├── filament_usage.py          # UPDATE_TAG macro — filament deduction tracking
├── moonraker_ws.py            # Moonraker websocket — real-time AFC and macro events
├── var_watcher.py             # Klipper save_variables file watcher (toolhead modes)
│
├── spoolman/
│   └── client.py              # SpoolmanClient — read-only spool lookup and tag enrichment
├── spoolman_cache.py          # In-memory UID→spool cache for UID-only tag lookups
│
├── adapters/
│   └── dispatcher.py          # Tag format auto-detection and parser routing
├── openprinttag/
│   └── scanner_parser.py      # spoolsense_scanner JSON → ScanEvent
├── opentag3d/
│   └── parser.py              # OpenTag3D JSON → ScanEvent
│
├── state/
│   ├── models.py              # ScanEvent, SpoolInfo, SpoolAssignment dataclasses
│   └── moonraker_db.py        # Moonraker database access for lane_data
│
├── tag_sync/
│   ├── policy.py              # Tag writeback decision logic (stale weight detection)
│   └── scanner_writer.py      # MQTT write commands to scanner firmware
│
├── rest_api.py                # FastAPI REST server for SpoolSense Mobile app
├── klipper/
│   └── spoolsense.cfg         # Klipper macros (ASSIGN_SPOOL, UPDATE_TAG)
│
├── config.example.single.yaml
├── config.example.toolchanger.yaml
├── config.example.afc.yaml
├── spoolsense.service         # systemd service file
├── requirements.txt
└── tests/                     # unittest + mocks (one file per module)
```

## Scanner Actions

Each scanner in `config.yaml` has an `action` that determines how scans are routed:

| Action | What it does | Locks scanner? |
|--------|-------------|----------------|
| `afc_stage` | Caches tag data, waits for AFC lane load | No |
| `afc_lane` | Assigns spool to a specific AFC lane | Yes |
| `toolhead_stage` | Caches tag data, waits for ASSIGN_SPOOL macro | No |
| `toolhead` | Activates spool on a specific toolhead | Yes |

## Background Services

The middleware runs several background threads:

- **AfcStatusSync** — monitors AFC lane state via Moonraker websocket (primary) or HTTP polling (fallback). Detects spool load/eject events.
- **ToolchangerStatusSync** — watches the ASSIGN_SPOOL Klipper macro for manual tool assignment.
- **FilamentUsageSync** — watches the UPDATE_TAG Klipper macro for filament deduction after each print.
- **ToolheadStatusSync** — polls Moonraker for spool eject detection on toolhead macros.
- **MoonrakerWebsocket** — single websocket connection to Moonraker for real-time AFC stepper and macro variable updates.
- **VarWatcher** — watchdog file watcher on Klipper's `save_variables.cfg` for toolhead/single modes.

## Config

Settings are loaded from `~/SpoolSense/config.yaml`. See the `config.example.*.yaml` files for documented templates:

- `config.example.single.yaml` — Single toolhead
- `config.example.toolchanger.yaml` — Multi-toolhead toolchanger
- `config.example.afc.yaml` — AFC / BoxTurtle

## Running

```bash
# Manual
python3 middleware/spoolsense.py

# Validate config without connecting
python3 middleware/spoolsense.py --check-config

# As a service
sudo systemctl start spoolsense
journalctl -u spoolsense -f
```

## Testing

```bash
# Full test suite
pytest middleware/tests/ -v

# Single file
pytest middleware/tests/test_activation.py -v
```

Tests use `unittest.TestCase` with `unittest.mock`. One test file per module.
