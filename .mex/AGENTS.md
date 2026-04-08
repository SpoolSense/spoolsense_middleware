---
name: agents
description: Always-loaded project anchor. Read this first. Contains project identity, non-negotiables, commands, and pointer to ROUTER.md for full context.
last_updated: 2026-04-05
---

# SpoolSense Middleware

## What This Is

Python middleware that bridges NFC scanner scans (via MQTT) to Spoolman (filament database) and Klipper/Moonraker (3D printer firmware) — reading tag data, syncing spools, and sending gcode commands to activate the right spool on the right tool or AFC lane.

## Non-Negotiables

- All new Python functions must have type hints — no untyped function signatures
- Never commit secrets, API keys, or real IP addresses — config files use placeholder strings
- Activation must never silently fail — log errors explicitly, return False, do not swallow exceptions
- `activation.py` builds SpoolEvents and routes them; it must not contain direct Moonraker HTTP calls — those live in `publishers/klipper.py`
- `app_state` is the shared mutable process state — all multi-thread access to `lane_locks`, `active_spools`, `lane_statuses`, `lane_load_states`, `pending_spool`, and `tag_write_timestamps` must be protected by `app_state.state_lock`
- `tag_writeback_enabled: false` is the default — writeback is opt-in; dry-run mode must log the write plan but never publish

## Commands

- Run: `python3 middleware/spoolsense.py` (from repo root or `~/SpoolSense/`)
- Run (service): `sudo systemctl start spoolsense`
- Validate config: `python3 middleware/spoolsense.py --check-config`
- Test: `python3 -m pytest middleware/tests/ -v` (from repo root)
- Lint: `python3 -m flake8 middleware/` or `python3 -m pylint middleware/`
- Install deps: `pip install -r middleware/requirements.txt`

## After Every Task

After completing any task: update `.mex/ROUTER.md` project state and any `.mex/` files that are now out of date. If no pattern existed for the task you just completed, create one in `.mex/patterns/`.

## Navigation

At the start of every session, read `.mex/ROUTER.md` before doing anything else.
For full project context, patterns, and task guidance — everything is there.
