# AFC BoxTurtle NFC Setup Guide

> **OUTDATED — This document needs a full rewrite to reflect the current PN5180 scanner firmware and installer. Will be updated soon.**

## Overview

This guide covers NFC spool scanning for BoxTurtle lanes. Each lane gets a SpoolSense Scanner that communicates with the middleware via MQTT.

When you place a spool on a respooler, the NFC tag is scanned automatically as it rotates into range. The middleware looks up the spool in Spoolman and calls AFC's `SET_SPOOL_ID` to register it in the correct lane. AFC automatically pulls color, material, and weight from Spoolman — one call does everything.

## How It Works

### Scan-Lock-Clear Lifecycle

**Scanning** — when no spool is registered on a lane, the scanner is actively polling. Any NFC tag that enters the read zone triggers a scan.

**Locked** — after a successful scan and spool registration, the middleware publishes a "lock" command. The scanner stops publishing on that lane. The spool can rotate freely during printing without triggering more scans.

**Clear** — the middleware watches AFC's variable file (`AFC.var.unit`) for changes. When a lane is ejected and the spool_id is cleared, the middleware automatically publishes "clear" to resume scanning on that lane. On shutdown, all lanes are cleared so scanners resume on next startup.

### AFC Variable File Watcher

The middleware uses `watchdog` to monitor `AFC.var.unit` for changes. When the file is written (e.g. after a lane load, eject, or state change), the middleware reads the updated lane data and:
- Locks scanners for lanes with spools, clears empty lanes
- Caches lane statuses so NFC scan handlers can check AFC state instantly

### Lane LED Colors

Lane LED color is owned entirely by AFC-Klipper-Add-On. When `SET_SPOOL_ID` is called, AFC stores the Spoolman filament color on the lane object. AFC's `_get_lane_color()` then uses that color when updating LEDs during load, unload, and state transitions.

SpoolSense does not call any LED macros in AFC mode. No custom `_SET_LANE_LED` macro is needed.

> This requires AFC-Klipper-Add-On with `led_use_filament_color` support. See [AFC-Klipper-Add-On PR #681](https://github.com/ArmoredTurtle/AFC-Klipper-Add-On/pull/681). Without it, AFC uses its default configured colors.

### AFC Integration

The middleware calls `SET_SPOOL_ID LANE=<lane> SPOOL_ID=<id>` via Moonraker's gcode script API. AFC then:
- Pulls filament color from Spoolman → updates lane color in UI
- Pulls material type from Spoolman → sets lane material
- Pulls remaining weight from Spoolman → sets lane weight
- Manages active spool tracking automatically on lane changes

## Differences from Toolchanger Mode

| Feature | Toolchanger | AFC/AMS |
|---------|-------------|---------|
| Scanner location | Per toolhead | Per lane in BoxTurtle |
| Spool registration | SET_ACTIVE_SPOOL / SET_GCODE_VARIABLE | SET_SPOOL_ID (AFC) |
| LED feedback | Scanner onboard LED | BoxTurtle lane LEDs (via AFC natively) |
| Scan behavior | Always scanning | Scan-lock-clear lifecycle |
| File watcher | Klipper save_variables | AFC.var.unit |
| Klipper macros | spoolman_macros.cfg + toolhead macros | None required for LEDs |
