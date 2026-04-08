---
name: architecture
description: How the major pieces of this project connect and flow. Load when working on system design, integrations, or understanding how components interact.
triggers:
  - "architecture"
  - "system design"
  - "how does X connect to Y"
  - "integration"
  - "flow"
edges:
  - target: context/stack.md
    condition: when specific technology details are needed
  - target: context/decisions.md
    condition: when understanding why the architecture is structured this way
last_updated: 2026-04-05
---

# Architecture

## System Overview

NFC scanner scans a filament spool → publishes JSON payload to MQTT broker →
`mqtt_handler.on_message()` receives it → resolves scanner config (device_id → action + target) →
calls `adapters/dispatcher.detect_and_parse()` to normalize into a `ScanEvent` →
`SpoolmanClient.sync_spool_from_scan()` looks up or creates a spool in Spoolman (best-effort) →
`activation._activate_from_scan()` builds a platform-neutral `SpoolEvent` and routes through `PublisherManager` →
`KlipperPublisher.publish()` sends gcode commands + Moonraker REST calls to activate the spool on the correct tool/lane.

Parallel to the MQTT loop: background threads run `AfcStatusSync` (polls or listens via websocket to
Moonraker AFC lane state to detect load/eject) and `ToolchangerStatusSync` (polls ASSIGN_SPOOL macro
variable to detect manual tool assignment). Both consume `app_state.pending_spool` set at scan time.

## Key Components

- **`spoolsense.py`** — entry point; wires all components together at startup, handles signals
- **`app_state.py`** — shared mutable process state (cfg, spool_cache, lane_locks, active_spools, pending_spool, etc.); all multi-thread state lives here, protected by `state_lock`
- **`config.py`** — loads `~/SpoolSense/config.yaml`, validates, migrates legacy format, derives toolheads list
- **`mqtt_handler.py`** — MQTT `on_connect` / `on_message` callbacks; resolves scanner from topic, routes to `_handle_rich_tag()`
- **`adapters/dispatcher.py`** — detects tag format (`spoolsense_scanner` vs `opentag3d`) and routes to the correct parser; always returns a `ScanEvent`
- **`activation.py`** — orchestration layer: builds `SpoolEvent`, routes through `PublisherManager`, manages lock decisions and `pending_spool` caching; owns no HTTP calls
- **`publishers/base.py`** — `SpoolEvent` dataclass, `Publisher` ABC, `Action` enum
- **`publishers/klipper.py`** — `KlipperPublisher` (primary publisher): gcode commands and Moonraker REST calls; `display_spoolcolor()`, `_validate_color_hex()`, `_validate_material()` helpers
- **`publisher_manager.py`** — fan-out dispatcher; primary publisher result drives lock decisions; secondary failures are logged but do not block
- **`spoolman/client.py`** — `SpoolmanClient`: NFC UID → spool lookup (with TTL cache), spool creation (vendor+filament+spool), weight update via PATCH
- **`spoolman_cache.py`** — simpler NFC UID cache used for the UID-only (non-rich-tag) path; shared `app_state.spool_cache`
- **`afc_status.py`** — `AfcStatusSync`: polls `GET /printer/afc/status` (or receives websocket deltas); detects lane load/eject transitions; pushes pending tag data on load
- **`toolchanger_status.py`** — `ToolchangerStatusSync`: polls `ASSIGN_SPOOL` macro variable (or receives websocket callbacks); assigns pending spool to the named tool
- **`moonraker_ws.py`** — `MoonrakerWebsocket`: single websocket connection to Moonraker, subscribes to AFC_stepper objects and `gcode_macro ASSIGN_SPOOL`; dispatches deltas via `on_lane_update` / `on_assign_spool` callbacks
- **`filament_usage.py`** — `FilamentUsageSync`: monitors `UPDATE_TAG` macro via websocket/polling; calculates per-tool deductions from klipper-toolchanger tool objects (primary) or slicer `filament_weights` (fallback); AFC path reads lane weights from `/printer/afc/status`; sends `cmd/deduct/{uid}` to scanner via MQTT; gates deductions by `tag_format` (only OpenPrintTag/OpenTag3D)
- **`var_watcher.py`** — watchdog file watcher on Klipper's `save_variables.cfg`; used for toolhead/single modes to sync state on manual changes
- **`state/models.py`** — `ScanEvent`, `SpoolInfo`, `SpoolAssignment` dataclasses
- **`tag_sync/policy.py`** — `build_write_plan()` decides whether to write updated remaining weight back to tag; enforces downward-only writes and per-UID cooldown
- **`tag_sync/scanner_writer.py`** — executes a `TagWritePlan` by publishing MQTT command to scanner

## Per-Scanner Action Routing

Each scanner entry in `config.yaml` declares an `action` field that determines what happens on scan:

| Action | Gcode Sent | Scanner Locked? | Use Case |
|---|---|---|---|
| `afc_stage` | `SET_NEXT_SPOOL_ID` (with Spoolman) or cache-then-push (tag-only) | No | Shared scanner for AFC — scan spool, then load any lane |
| `afc_lane` | `SET_SPOOL_ID LANE={lane}` | Yes | Dedicated scanner per AFC lane |
| `toolhead_stage` | `SET_GCODE_VARIABLE` + `SAVE_VARIABLE` on tool pickup | No | Shared scanner for toolchanger — scan spool, then pick up any tool |
| `toolhead` | `SET_ACTIVE_SPOOL` + `SET_GCODE_VARIABLE` + `SAVE_VARIABLE` | Yes | Dedicated scanner per toolhead |

Mixed configs are supported (e.g. `afc_stage` + `toolhead` on the same printer).

### Shared Scanner Flow (afc_stage / toolhead_stage)

```
Scan tag → tag data cached in app_state.pending_spool
  → afc_stage: AfcStatusSync detects lane load → push data to that lane
  → toolhead_stage: ToolchangerStatusSync detects tool pickup via ASSIGN_SPOOL macro → push data to that tool
Scanner stays unlocked — can scan again immediately.
Works with and without Spoolman.
```

## MQTT Topics

| Topic | Direction | Content |
|---|---|---|
| `spoolsense/<id>/tag/state` | Scanner → Middleware | Rich tag JSON (spoolsense_scanner format or OpenTag3D) |
| `spoolsense/<id>/cmd/<cmd>/<uid>` | Middleware → Scanner | Write command (e.g. `update_remaining`) |
| `spoolsense/<id>/lock` | Middleware → Scanner | `lock` / `clear` |
| `nfc/middleware/online` | Middleware → Broker | `true`/`false` LWT |

The topic prefix (`spoolsense` by default) is configurable via `scanner_topic_prefix` in config.

## Moonraker API Endpoints Used

| Endpoint | Method | Purpose |
|---|---|---|
| `/printer/gcode/script` | POST | Send gcode (SET_SPOOL_ID, SET_COLOR, etc.) |
| `/server/spoolman/spool_id` | POST | Set active spool in Moonraker |
| `/printer/afc/status` | GET | AFC lane state (spool_id, load, color, material) |
| `/printer/objects/query?toolchanger` | GET | Active tool number |
| `/printer/objects/query?gcode_macro T{n}` | GET | Tool macro variables (color, spool_id) |
| `/printer/objects/query?save_variables` | GET | Persisted spool IDs |
| `/server/database/item` | POST | Write lane data for Orca Slicer integration |
| `/printer/configfile/settings` | GET | Config discovery (e.g. Klipper var file path) |

## Module Structure

```
middleware/
├── spoolsense.py              ← Entry point: main(), signal handlers, component wiring
├── config.py                  ← load_config(), validation, legacy migration
├── app_state.py               ← Shared mutable state (cfg, caches, locks, pending_spool)
├── activation.py              ← activate_spool(), publish_lock(), _activate_from_scan()
├── mqtt_handler.py            ← on_connect(), on_message(), _handle_rich_tag()
├── afc_status.py              ← AFC lane state polling via Moonraker API
├── toolchanger_status.py      ← Toolchanger state polling via Moonraker API
├── moonraker_ws.py            ← MoonrakerWebsocket — optional websocket connection
├── publisher_manager.py       ← PublisherManager fan-out: primary + secondary publishers
├── publishers/
│   ├── base.py                ← SpoolEvent dataclass, Publisher ABC, Action enum
│   └── klipper.py             ← KlipperPublisher — all Moonraker HTTP + gcode calls
├── spoolman_cache.py          ← Spoolman spool cache, NFC UID lookup (non-rich path)
├── spoolman/
│   └── client.py              ← SpoolmanClient: NFC UID → spool, spool creation, weight sync
├── var_watcher.py             ← Klipper save_variables file watcher (non-AFC modes)
├── adapters/
│   └── dispatcher.py          ← detect_format() + route to correct parser
├── state/
│   └── models.py              ← ScanEvent, SpoolInfo, SpoolAssignment dataclasses
├── tag_sync/
│   ├── policy.py              ← build_write_plan(), downward-only + per-UID cooldown
│   └── scanner_writer.py      ← Execute TagWritePlan via MQTT publish
├── openprinttag/
│   └── scanner_parser.py      ← spoolsense_scanner JSON → ScanEvent
├── opentag3d/
│   └── parser.py              ← OpenTag3D JSON → ScanEvent
├── config.example.afc.yaml
├── config.example.single.yaml
├── config.example.toolchanger.yaml
└── tests/                     ← pytest unit tests (unittest + mocks)
```

## Tag Format Detection

`adapters/dispatcher.detect_format()` auto-detects the tag payload format:
- **`spoolsense_scanner`** — payload has `present` + `tag_data_valid` keys
- **`OpenTag3D`** — payload has `opentag_version` or `spool_weight_nominal` keys
- **UID-only fallback** — `present=True, tag_data_valid=False, blank=False, uid set` → Spoolman `find_spool_by_nfc()` lookup

## External Dependencies

- **MQTT broker** — receives scanner payloads; SpoolSense subscribes to `{prefix}/{device_id}/tag/state`; also publishes lock state and `spoolsense/middleware/online`
- **Spoolman** (`/api/v1/spool`, `/api/v1/filament`, `/api/v1/vendor`) — filament database; NFC UIDs stored in spool `extra.nfc_id`; weight stored as `used_weight` (not remaining)
- **Moonraker** — Klipper API gateway; used for gcode scripts (`/printer/gcode/script`), Spoolman spool activation (`/server/spoolman/spool_id`), AFC status (`/printer/afc/status`), lane data DB (`/server/database/item`), and config discovery (`/printer/configfile/settings`); also connected via websocket (`ws://…/websocket`)
- **Klipper** — 3D printer firmware; receives gcode via Moonraker; `SAVE_VARIABLE` persists spool IDs; `ASSIGN_SPOOL` macro provides manual toolhead assignment

## What Does NOT Exist Here

- No web UI or REST API — SpoolSense is a headless background service
- No direct Klipper socket connection — all Klipper interaction goes through Moonraker
- No file-based AFC state sync — the old `watchdog` on `AFC.var.unit` was replaced by `afc_status.py` Moonraker polling; `var_watcher.py` is only used for toolhead/single modes
- No publisher implementations for non-Klipper printers (Bambu, Prusa, OctoPrint) — the Publisher ABC is ready but only `KlipperPublisher` is implemented
- No persistent database — all state is in-memory; `app_state.py` is reset on restart; spool IDs are recovered from Klipper variables or Spoolman on startup
