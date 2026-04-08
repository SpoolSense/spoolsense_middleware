---
name: router
description: Session bootstrap and navigation hub. Read at the start of every session before any task. Contains project state, routing table, and behavioural contract.
edges:
  - target: context/architecture.md
    condition: when working on system design, integrations, or understanding how components connect
  - target: context/stack.md
    condition: when working with specific technologies, libraries, or making tech decisions
  - target: context/conventions.md
    condition: when writing new code, reviewing code, or unsure about project patterns
  - target: context/decisions.md
    condition: when making architectural choices or understanding why something is built a certain way
  - target: context/setup.md
    condition: when setting up the dev environment or running the project for the first time
  - target: patterns/INDEX.md
    condition: when starting a task — check the pattern index for a matching pattern file
last_updated: 2026-04-05
---

# Session Bootstrap

If you haven't already read `AGENTS.md`, read it now — it contains the project identity, non-negotiables, and commands.

Then read this file fully before doing anything else in this session.

## Current Project State

**Working:**
- Full NFC scan-to-activation pipeline: scanner → MQTT → dispatcher → activation → Klipper/Moonraker
- Four scanner actions: `afc_stage`, `afc_lane`, `toolhead`, `toolhead_stage`
- AFC lane state sync via Moonraker websocket (primary) with HTTP polling fallback
- ASSIGN_SPOOL macro polling for toolhead_stage (websocket + HTTP fallback)
- Spoolman spool creation, weight sync, and NFC UID writeback on first scan
- Tag writeback (update_remaining) with 10s per-UID cooldown and dry-run mode
- Publisher architecture: SpoolEvent → PublisherManager → KlipperPublisher (primary)
- Klipper variables file watcher (toolhead/single modes)
- Lane data publishing to Moonraker `lane_data` DB for Orca Slicer integration
- Two tag formats: `spoolsense_scanner` (MQTT JSON) and `opentag3d` (CBOR)
- Atomic toolhead activation with Spoolman + SAVE_VARIABLE rollback on failure
- `--check-config` flag for config validation without starting MQTT loop
- Legacy config migration (toolhead_mode + scanner_lane_map → scanners format)
- Full test suite in `middleware/tests/` using unittest + mocks
- Bambu Lab support via HA blueprints (scanner repo: `homeassistant/blueprints/`) — AMS tray auto-fill + Spoolman weight deduction
- UPDATE_TAG macro for filament usage deduction — per-tool tracking via klipper-toolchanger mod (primary) with slicer estimate fallback; AFC uses real-time lane weights; UID-only/TigerTag/OpenSpool is no-op (#51)
- Tag format gating — middleware reads `tag_format` from scanner payload, only sends deductions for writable tags (OpenPrintTag/OpenTag3D) (#54)
- Scanner-side deduction storage — NVS-backed `DeductionManager`, receives `cmd/deduct/{uid}` via MQTT, applies on next scan, writes updated weight to tag (scanner #123, shipped)
- klipper-toolchanger per-tool filament tracking — `filament_used` per tool object, tested on hardware (PR #167 submitted upstream)

**Not yet built:**
- **Mobile filament deduction** (#56) — REST endpoints `GET /api/deductions/{uid}` and `POST /api/deductions/{uid}/applied` for mobile-only users. App shows "Update Filament Usage" button for writable tags, reads pending from middleware or accepts manual entry, writes to tag via NFC.
- **Additional middleware publishers (Prusa, OctoPrint)** — publisher ABC is ready, no implementations yet
- **Scanner #32** — Import from Spoolman on writer pages (UX approach agreed, needs implementation)
- **Scanner #39** — PN532 NFC reader support
- **OpenPrintTag spec** (CBOR direct from PN5180) — detected by dispatcher but raises NotImplementedError
- **#18** — Replace Klipper vars file watcher (`var_watcher.py`) with Moonraker API; remove `watchdog` dependency

**Known issues:**
- `pending_spool` is a single shared slot — if a user has both `afc_stage` and `toolhead_stage` scanners in the same config, a scan on one could be consumed by the other's poller (documented in `app_state.py` with a note to split if reported)
- **#20** — `_create_spool_from_tag()` vendor_id field needs live validation against Spoolman
- **#21** — No write loop protection for tag writeback beyond cooldown; needs per-UID rate limiting
- **#22** — Dual weight-sync ownership between scanner firmware and middleware is ambiguous
- **#15** — Toolhead activation was non-atomic (partial state on `SAVE_VARIABLE` failure) — rollback implemented but not all paths covered

## Routing Table

Load the relevant file based on the current task. Always load `context/architecture.md` first if not already in context this session.

| Task type | Load |
|-----------|------|
| Understanding how the system works | `context/architecture.md` |
| Working with a specific technology | `context/stack.md` |
| Writing or reviewing code | `context/conventions.md` |
| Making a design decision | `context/decisions.md` |
| Setting up or running the project | `context/setup.md` |
| Any specific task | Check `patterns/INDEX.md` for a matching pattern |

## Behavioural Contract

For every task, follow this loop:

1. **CONTEXT** — Load the relevant context file(s) from the routing table above. Check `patterns/INDEX.md` for a matching pattern. If one exists, follow it. Narrate what you load: "Loading architecture context..."
2. **BUILD** — Do the work. If a pattern exists, follow its Steps. If you are about to deviate from an established pattern, say so before writing any code — state the deviation and why.
3. **VERIFY** — Load `context/conventions.md` and run the Verify Checklist item by item. State each item and whether the output passes. Do not summarise — enumerate explicitly.
4. **DEBUG** — If verification fails or something breaks, check `patterns/INDEX.md` for a debug pattern. Follow it. Fix the issue and re-run VERIFY.
5. **GROW** — After completing the task:
   - If no pattern exists for this task type, create one in `patterns/` using the format in `patterns/README.md`. Add it to `patterns/INDEX.md`. Flag it: "Created `patterns/<name>.md` from this session."
   - If a pattern exists but you deviated from it or discovered a new gotcha, update it with what you learned.
   - If any `context/` file is now out of date because of this work, update it surgically — do not rewrite entire files.
   - Update the "Current Project State" section above if the work was significant.
