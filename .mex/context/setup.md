---
name: setup
description: Dev environment setup and commands. Load when setting up the project for the first time or when environment issues arise.
triggers:
  - "setup"
  - "install"
  - "environment"
  - "getting started"
  - "how do I run"
  - "local development"
edges:
  - target: context/stack.md
    condition: when specific technology versions or library details are needed
  - target: context/architecture.md
    condition: when understanding how components connect during setup
last_updated: 2026-04-05
---

# Setup

## Prerequisites

- Python 3.10+ (3.9 may work due to `from __future__ import annotations` but is not tested)
- MQTT broker accessible on the network (e.g. Mosquitto via Home Assistant)
- Moonraker running on the 3D printer (Klipper)
- Spoolman (optional — middleware runs in tag-only mode without it)

## First-time Setup

1. Clone repo and navigate to it
2. `pip install -r middleware/requirements.txt`
3. `mkdir -p ~/SpoolSense`
4. Copy the appropriate config template: `cp middleware/config.example.afc.yaml ~/SpoolSense/config.yaml` (or `.single.yaml` / `.toolchanger.yaml`)
5. Edit `~/SpoolSense/config.yaml` — fill in `mqtt.broker`, `moonraker_url`, `spoolman_url`, and `scanners`
6. Validate: `python3 middleware/spoolsense.py --check-config`
7. Run: `python3 middleware/spoolsense.py`

## Service Installation (production)

1. Copy `middleware/spoolsense.service` to `/etc/systemd/system/spoolsense.service`
2. Edit the service file — replace `YOUR_USERNAME` and `YOUR_USERNAME` placeholders
3. `sudo systemctl daemon-reload`
4. `sudo systemctl enable --now spoolsense`
5. `journalctl -u spoolsense -f` to watch logs

## Environment Variables

There are no environment variables — all configuration is in `~/SpoolSense/config.yaml`.

## Config Key Reference

**Required:**
- `mqtt.broker` — MQTT broker IP or hostname
- `moonraker_url` — Moonraker base URL (e.g. `http://192.168.1.10`)
- `scanners` — dict of device_id → `{action: ..., lane: ...}` or `{action: ..., toolhead: ...}`

**Optional:**
- `spoolman_url` — if omitted, runs in tag-only mode (no Spoolman lookup or creation)
- `mqtt.port` — default `1883`
- `mqtt.username` / `mqtt.password` — MQTT auth
- `low_spool_threshold` — grams remaining to trigger warning (default `100`)
- `klipper_var_path` — path to Klipper save_variables file; auto-discovered from Moonraker if not set
- `scanner_topic_prefix` — MQTT topic prefix (default `"spoolsense"`)
- `tag_writeback_enabled` — default `false`; set to `true` to enable NFC tag weight writeback
- `publish_lane_data` — default `false`; set to `true` to write spool data to Moonraker `lane_data` DB for Orca Slicer
- `toolheads` — explicit list of lane/toolhead names; derived from scanner config if not set

## Common Commands

- Run middleware: `python3 middleware/spoolsense.py`
- Validate config: `python3 middleware/spoolsense.py --check-config`
- Run tests: `python3 -m pytest middleware/tests/ -v`
- Run single test file: `python3 -m pytest middleware/tests/test_activation.py -v`
- Lint: `python3 -m flake8 middleware/` or `python3 -m pylint middleware/`
- Install deps: `pip install -r middleware/requirements.txt`
- Service logs: `journalctl -u spoolsense -f`
- Service restart: `sudo systemctl restart spoolsense`

## Config File

The live config lives at `~/SpoolSense/config.yaml`. This path is **never overwritten by git**. The repo provides three example templates:
- `middleware/config.example.afc.yaml` — AFC multi-lane setup
- `middleware/config.example.single.yaml` — single toolhead
- `middleware/config.example.toolchanger.yaml` — klipper-toolchanger setup

## Printer & Service Access

| Service | Address |
|---------|---------|
| Printer (Moonraker) | `http://192.168.1.72` (port 80) |
| Spoolman | `http://192.168.1.32:7912` |
| MQTT broker | `192.168.1.167:1883` |
| Scanner | `http://spoolsense.local` or direct IP |
| WROOM scanner (USB) | `/dev/cu.usbserial-310` |

Printer setup: toolchanger with T0–T3 (klipper-toolchanger) + BoxTurtle AFC on T0 (lane1–lane4 mapped as T4–T7). Primary scanner device ID: `f3d360`.

Use `curl` via Bash for all printer API calls — do not use WebFetch.

## Common Issues

**`adapters/ directory not found` at startup:**
The `adapters/` and `openprinttag/` directories contain the rich-tag dispatcher. Make sure you're running from the `middleware/` directory or that the `middleware/` directory is on the Python path. The `WorkingDirectory` in the service file must point to the directory containing `spoolsense.py`.

**`spoolsense/middleware/online` stays `false`:**
MQTT connection failed — check `mqtt.broker` address and credentials in config.

**AFC status returns 404:**
AFC Klipper Add-On is not installed or not running. The `/printer/afc/status` endpoint only exists when AFC is active. The middleware will warn and fall back to polling without crashing.

**`ASSIGN_SPOOL macro not found` warning:**
For `toolhead_stage` action, the user must add the `ASSIGN_SPOOL` macro to `printer.cfg`. See `middleware/klipper/spoolsense.cfg` — users add `[include spoolsense.cfg]` to their Klipper config.

**Klipper `SAVE_VARIABLE` rejected with uppercase variable name:**
Klipper requires lowercase variable names. The publisher lowercases toolhead names automatically (e.g. `T0` → `t0_spool_id`). If you see this error it is a bug in the publisher code.
