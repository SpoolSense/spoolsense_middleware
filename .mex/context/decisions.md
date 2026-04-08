---
name: decisions
description: Key architectural and technical decisions with reasoning. Load when making design choices or understanding why something is built a certain way.
triggers:
  - "why do we"
  - "why is it"
  - "decision"
  - "alternative"
  - "we chose"
edges:
  - target: context/architecture.md
    condition: when a decision relates to system structure
  - target: context/stack.md
    condition: when a decision relates to technology choice
last_updated: 2026-04-05
---

# Decisions

<!-- When a decision changes: DO NOT delete the old entry. Mark it as superseded, add the new entry above it. -->

## Decision Log

### Mobile filament deduction — REST-based with explicit user action
**Date:** 2026-04-07
**Status:** Planned (middleware #56, mobile app in progress)
**Decision:** Mobile-only users (no physical scanner) apply deductions via an explicit "Update Filament Usage" button in the app. The middleware stores pending deductions via REST, the app fetches them on button tap, and the user scans the tag to write. Manual entry fallback when no pending deduction exists.
**Alternatives rejected:**
- HA blueprints — adds unnecessary dependency for non-Bambu users
- Automatic deduction on scan — confusing UX, user doesn't know what's happening
- Warning-only approach — tells user there's a problem but doesn't solve it
**Key decisions:**
- Button only shows for writable tags (OpenPrintTag/OpenTag3D)
- New NFC session on button tap — initial scan session is already closed by then
- Middleware stores deductions, not the phone — phone isn't always on
- Clamp to zero if deduction > remaining — same as scanner behavior
- Manual entry option covers forgot-to-run-UPDATE_TAG and cancelled print cases

### Filament usage deduction via UPDATE_TAG macro
**Date:** 2026-04-06
**Status:** Active (middleware #51, scanner side pending)
**Decision:** `UPDATE_TAG` macro fires in PRINT_END. Middleware calculates per-tool usage and sends deductions to the scanner via MQTT. Scanner stores deductions in `deductions.json` and writes to the NFC tag on next scan. Tag is source of truth.
**Three paths by setup:**
- **Toolchanger/single (primary):** Read per-tool `filament_used` from klipper-toolchanger tool objects (mm), convert to grams using tag's diameter + density. Requires klipper-toolchanger per-tool tracking mod (PR pending on viesturz/klipper-toolchanger).
- **Toolchanger/single (fallback):** If mod not installed, use slicer `filament_weights` from Moonraker print history. Per-tool grams, but slicer estimate only — inaccurate for cancelled prints.
- **AFC:** Read current per-lane weight from `/printer/afc/status`, compare to initial tag weight at scan time, send difference as deduction. AFC tracks weight in real-time via extruder position.
- **UID-only/TigerTag/OpenSpool:** No-op. Moonraker's built-in Spoolman sync handles tracking. Requires `[spoolman]` in moonraker.conf (installer #28).
**Key design decisions:**
- UPDATE_TAG fires per print (in PRINT_END), not per spool swap — each print's usage goes to the right spool
- Scanner accumulates deductions per UID persistently — no timing pressure
- Scanner writes deduction to tag first, then publishes post-deduction data — tag is source of truth
- mm→grams conversion uses filament diameter and density from the tag data, not hardcoded defaults

### Bambu Lab support: Home Assistant blueprints
**Date:** 2026-03-30
**Status:** Active (shipped in scanner repo)
**Decision:** Bambu Lab support uses Home Assistant blueprints, not direct scanner-to-printer MQTT or a middleware publisher. Two blueprints in `spoolsense_scanner/homeassistant/blueprints/`:
1. **`spoolsense_bambu_ams.yaml`** — Scan spool → HA detects SpoolSense sensor change → user loads AMS tray → HA detects tray loaded → calls `bambu_lab.set_filament` with material, color (RGB+FF alpha), and temps. 300s timeout.
2. **`spoolsense_bambu_deduction.yaml`** — After print completes/cancels, deducts per-tray filament weight from Spoolman via spoolman-homeassistant integration.
**Reasoning:** Bambu users already have HA + Bambu Lab integration. Blueprints require zero custom firmware, no TLS MQTT from ESP32, and work with any scanner. Lower barrier than direct scanner approach.
**Previous approach (shelved):** Direct scanner strategy (`BambuStrategy` on ESP32 talking TLS MQTT to printer:8883) — shelved because it required TLS on ESP32 and users without HA. Research doc still at `research/bambu-mqtt-integration.md`.
**Consequences:** Requires Home Assistant with Bambu Lab integration installed. No middleware changes needed.

### Publisher pattern for output decoupling
**Date:** 2026-03-25
**Status:** Active
**Decision:** All printer-platform output goes through a `Publisher` ABC registered in `PublisherManager`; `activation.py` builds a platform-neutral `SpoolEvent` and calls `manager.publish()`.
**Reasoning:** Adding support for Bambu, Prusa, OctoPrint, etc. should require one new file in `publishers/` with no changes to `activation.py` or `mqtt_handler.py`. Before this, activation.py contained direct Moonraker HTTP calls mixed with orchestration logic.
**Alternatives considered:** Direct platform dispatch in activation.py (rejected — adding a new printer type required modifying the orchestration core); plugin system (rejected — overkill for the current number of publishers).
**Consequences:** `KlipperPublisher` is the only publisher today. New publishers are registered in `spoolsense.py main()`. One publisher is marked `primary=True`; its return value drives lock decisions in `activation.py`.

### Moonraker websocket with HTTP polling fallback
**Date:** 2026-03-28 (merged PR #39)
**Status:** Active
**Decision:** AFC lane state and ASSIGN_SPOOL macro are monitored via Moonraker websocket when `websocket-client` is installed; HTTP polling is used as a fallback when the library is absent.
**Reasoning:** Websocket delivers real-time state deltas without the 2-second polling lag. HTTP polling is retained as a fallback for users who don't install the optional dependency.
**Alternatives considered:** HTTP polling only (rejected — 2s lag causes visible delay between physical load and state sync); pure websocket with no fallback (rejected — reduces compatibility for minimal maintenance setups).
**Consequences:** `WEBSOCKET_AVAILABLE` is checked at import time in `moonraker_ws.py`. Both `AfcStatusSync` and `ToolchangerStatusSync` accept `use_ws=True` to skip their poll loops. AFC lane names are discovered from Moonraker at startup to build the subscription object list.

### AFC status via Moonraker REST API (not file watcher)
**Date:** ~2026-03-20
**Status:** Active (upgraded with websocket in PR #39)
**Decision:** AFC lane state is read from `GET /printer/afc/status` (and now also via websocket), not by watching AFC's `.var.unit` file on disk.
**Reasoning:** The file-based approach required the middleware to run on the same machine as Klipper. API polling works over the network and is independent of the filesystem layout. AFC's status response contains structured data including `spool_id`, `status`, and `load` fields per lane.
**Alternatives considered:** Watchdog on `AFC.var.unit` file (rejected — requires local disk access, brittle path assumptions); polling Klipper object status directly (same as the API approach, just via Moonraker's object query endpoint — deferred in favor of AFC's dedicated endpoint).
**Consequences:** 2-second polling interval (now superseded by websocket for real-time updates). Exponential backoff on consecutive failures. The AFC status response has a quirky `"status:"` key (with trailing colon) that `_sync_lane_state()` handles explicitly.

### Per-scanner action routing (v1.5.0)
**Date:** 2026-03-24
**Status:** Active — supersedes legacy `toolhead_mode` + `scanner_lane_map`
**Decision:** Each scanner entry in config declares its own `action` field (`afc_stage`, `afc_lane`, `toolhead`, `toolhead_stage`). The scanner's device_id is the key.
**Reasoning:** The old format conflated printer mode (afc vs toolchanger) with scanner routing. The new format lets a single config support mixed setups (e.g., AFC lanes + direct toolheads on the same printer).
**Alternatives considered:** Keeping `toolhead_mode` global (rejected — prevents mixed setups); per-printer config sections (rejected — unnecessary abstraction for the current use case).
**Consequences:** Legacy configs are auto-migrated at startup via `_migrate_legacy_config()` with a deprecation warning. The `_LEGACY_KEYS` set in `config.py` controls what triggers migration.

### Single `pending_spool` shared slot for staged actions
**Date:** 2026-03-24
**Status:** Active (with known limitation)
**Decision:** A single `app_state.pending_spool` dict stores tag data from `afc_stage` and `toolhead_stage` scans, consumed by `afc_status.py` (on lane load) or `toolchanger_status.py` (on macro assignment).
**Reasoning:** The vast majority of users have either an AFC setup or a toolchanger, not both. A single slot keeps the code simple.
**Alternatives considered:** Two separate slots — `pending_spool_afc` and `pending_spool_toolchanger` (accepted as the future path if the shared-slot issue is reported by users).
**Consequences:** If a user has both `afc_stage` and `toolhead_stage` scanners in the same config, a scan on one scanner could be consumed by the other's poller. This race condition is documented in `app_state.py`.

### Spoolman color always wins over tag color
**Date:** ~2026-02-01
**Status:** Active
**Decision:** When a spool already exists in Spoolman and `prefer_tag=True`, the Spoolman `color_hex` overrides the tag's color if Spoolman has one set.
**Reasoning:** A human likely chose the Spoolman color deliberately (accurate hex value), whereas the tag color is derived from a color name → hex lookup table which is often imprecise (e.g., "Galaxy Black" → best-guess hex).
**Alternatives considered:** Tag always wins for everything (rejected — would overwrite deliberate human corrections); Spoolman always wins for everything (rejected — tag is more authoritative for weight).
**Consequences:** `sync_spool()` in `SpoolmanClient` always checks `filament.color_hex` first before using `tag_spool.color_hex`. Weight update still uses tag data (tag is source of truth for remaining weight).

### Atomic toolhead activation with rollback
**Date:** 2026-03-28
**Status:** Active
**Decision:** When activating a toolhead spool, if `SAVE_VARIABLE` fails after `POST /server/spoolman/spool_id` has succeeded, a rollback posts `spool_id: 0` to Moonraker to revert the Spoolman assignment.
**Reasoning:** Without rollback, a failure midway through the sequence leaves Spoolman with a stale `spool_id` that disappears after Klipper restart (issue #15).
**Alternatives considered:** Accept partial state (rejected — confusing UX); write Klipper variable first (rejected — Klipper variables are not transactional and cannot easily roll back).
**Consequences:** Both `KlipperPublisher._handle_toolhead()` and `toolchanger_status._assign_spool_to_tool()` implement this rollback pattern. The same pattern applies to ASSIGN_SPOOL macro assignment.

### Tag writeback is opt-in, dry-run by default
**Date:** 2026-03-24
**Status:** Active
**Decision:** `tag_writeback_enabled: false` is the config default. In dry-run mode, `build_write_plan()` decisions are logged but no MQTT publish command is sent.
**Reasoning:** Tag writeback is a destructive operation (overwrites NFC tag data). Users should explicitly opt in to avoid surprise tag modifications. Dry-run mode lets users verify what would be written before enabling.
**Alternatives considered:** Enabled by default (rejected — risk of overwriting tags unexpectedly); separate enable/disable per scanner (rejected — added complexity without clear user benefit yet).
**Consequences:** `tag_writeback_enabled` is always checked in `mqtt_handler._handle_rich_tag()` before calling `scanner_writer.execute()`. The write plan is always built and logged regardless of the flag.
