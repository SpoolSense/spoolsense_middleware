# Middleware Developer Guide

This directory contains the SpoolSense middleware — a Python service that bridges NFC tag scans from the SpoolSense Scanner to Klipper, Spoolman, and Home Assistant via MQTT.

## Background

SpoolSense originally used ESPHome with PN532 NFC readers. Each toolhead or lane had its own ESP32 running ESPHome, which published tag UIDs to MQTT. The middleware parsed those UIDs and looked up spools in Spoolman.

The project has since moved to a custom firmware ([spoolsense_scanner](https://github.com/SpoolSense/spoolsense_scanner)) running on ESP32 with a PN5180 NFC reader. The scanner reads full tag data (not just UIDs), supports multiple tag formats (OpenPrintTag, TigerTag, OpenTag3D), publishes rich JSON payloads to MQTT, and handles its own Spoolman sync. This is a more capable and reliable approach than ESPHome.

The middleware still plays a key role — it receives scan events from the scanner, manages spool assignments, controls Klipper (SET_ACTIVE_SPOOL, SET_SPOOL_ID), handles tag writeback, and provides the scan-lock-clear lifecycle for AFC setups.

## Directory Structure

```
middleware/
├── spoolsense.py              # Main entry point — MQTT client, scan handlers, AFC file watcher
├── spoolsense.service         # systemd service file
├── config.example.*.yaml      # Config templates for each mode (single, toolchanger, AFC)
│
├── adapters/
│   └── dispatcher.py          # Auto-detects tag format and routes to the correct parser
│
├── openprinttag/
│   ├── scanner_parser.py      # Parses spoolsense_scanner MQTT payloads into ScanEvents
│   ├── parser.py              # [unused] Direct CBOR parser — from the ESPHome era
│   └── color_map.py           # [unused] Color name to hex lookup
│
├── opentag3d/
│   └── parser.py              # Parses OpenTag3D Web API payloads into ScanEvents
│
├── spoolman/
│   └── client.py              # Spoolman API client — vendor/filament/spool CRUD, NFC UID cache
│
├── state/
│   ├── models.py              # Core data models: ScanEvent, SpoolInfo, SpoolAssignment
│   └── moonraker_db.py        # Moonraker database persistence for spool state
│
└── tag_sync/
    ├── policy.py              # Write decision logic — determines when to write back to tags
    └── scanner_writer.py      # Publishes write commands to scanner via MQTT
```

## Dead Code

Some modules contain code from the ESPHome era that is no longer actively used but remains in the codebase:

- `openprinttag/parser.py` — Direct CBOR parser for OpenPrintTag spec payloads. Was intended for a custom ESPHome component that never shipped. The scanner firmware handles OpenPrintTag parsing natively.
- `openprinttag/color_map.py` — Color name to hex lookup table. Not imported anywhere.
- `opentag3d/parser.py` — Parses OpenTag3D "Web API" format payloads. The scanner sends OpenTag3D data through the standard scanner payload format instead.
- `adapters/dispatcher.py` — Has format detection branches for `opentag3d` and `openprinttag` direct formats that never trigger in practice. All scanner data arrives as `spoolsense_scanner` format.

This dead code is harmless and will be cleaned up in a future release.

## Config

The middleware loads settings from `~/SpoolSense/config.yaml`. See the `config.example.*.yaml` files for documented templates:

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
