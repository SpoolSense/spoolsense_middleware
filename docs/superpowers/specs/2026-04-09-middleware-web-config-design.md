# Middleware Web Config Panel — Design Spec

**Date:** 2026-04-09
**Status:** Approved

## What

A web-based config and status panel for the SpoolSense middleware, served alongside the existing REST API on port 5001. Matches the scanner web UI style exactly.

## Why

Users currently SSH in and edit YAML to configure the middleware. A web panel lets them view status, manage scanners, and edit config from any browser.

## Design

Tabbed interface with three sections:

### Status Tab (default)
- Connection indicators: MQTT, Moonraker, Spoolman, Klipper (green/red dots)
- Active Spools table: Tool, Spool ID, Material, Color swatch, Remaining weight
- Pending Deductions table: UID, Grams pending, Status

### Scanners Tab
- Table: Device ID (monospace), Action, Target, Status (badge)

### Config Tab
- Editable fields: Moonraker URL, Spoolman URL, MQTT Broker, MQTT Port, Low Spool Threshold, Scanner Topic Prefix
- Toggles: Tag Writeback, Publish Lane Data
- Save Config & Restart button (writes to config.yaml, restarts middleware)

## Implementation

- Serve HTML from FastAPI on the existing port 5001
- Single HTML file with embedded CSS/JS — no build step, no dependencies
- CSS matches scanner web UI exactly (same variables, fonts, card styles, nav)
- Status tab polls `GET /api/status` for live data
- Config tab reads from `GET /api/config`, writes via `POST /api/config`
- New endpoint: `POST /api/config` to write config.yaml and trigger restart

## Mockup

Approved mockup at `.superpowers/brainstorm/2789-1775789228/content/config-panel-v3.html`
