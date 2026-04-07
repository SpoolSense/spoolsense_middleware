---
name: conventions
description: How code is written in this project — naming, structure, patterns, and style. Load when writing new code or reviewing existing code.
triggers:
  - "convention"
  - "pattern"
  - "naming"
  - "style"
  - "how should I"
  - "what's the right way"
edges:
  - target: context/architecture.md
    condition: when a convention depends on understanding the system structure
last_updated: 2026-04-05
---

# Conventions

## Naming

- **Files**: `snake_case.py` — e.g. `mqtt_handler.py`, `afc_status.py`, `publisher_manager.py`
- **Classes**: `PascalCase` — e.g. `KlipperPublisher`, `AfcStatusSync`, `MoonrakerWebsocket`
- **Functions/methods**: `snake_case`, verb-first for actions — e.g. `activate_spool()`, `publish_lock()`, `sync_spool_from_scan()`
- **Private functions**: prefix with `_` — e.g. `_sync_lane_state()`, `_handle_rich_tag()`, `_validate_color_hex()`
- **Module-level constants**: `UPPER_SNAKE_CASE` — e.g. `POLL_INTERVAL`, `RETRY_MAX`, `CACHE_TTL`
- **Config keys**: `snake_case` matching YAML keys — e.g. `moonraker_url`, `spoolman_url`, `tag_writeback_enabled`
- **Klipper variable names**: lowercase — e.g. `t0_spool_id` not `T0_spool_id` (Klipper rejects uppercase in `SAVE_VARIABLE`)
- **Color hex strings**: 6-digit uppercase, no `#` prefix everywhere in Python — e.g. `"1A1A2E"` not `"#1a1a2e"`; `_validate_color_hex()` enforces this

## Structure

- **All Moonraker HTTP calls** live in `publishers/klipper.py` — `activation.py` and other modules must not call `requests.post/get` on Moonraker URLs directly
- **Each publisher** is a separate file in `publishers/` implementing `Publisher` ABC from `publishers/base.py`; registered in `spoolsense.py main()`
- **`app_state.py`** is the only global state module — everything else reads from it via `import app_state`; never create module-level mutable state elsewhere
- **Tests** live in `middleware/tests/`; one file per module under test — `test_activation.py`, `test_afc_status.py`, etc.; uses `unittest.TestCase` with `unittest.mock`
- **Parser modules** live in subdirectories by tag format — `openprinttag/`, `opentag3d/`; each exposes a parse function called from `adapters/dispatcher.py`
- **Config validation** is strict and fails fast — `_validate_scanners()` calls `sys.exit(1)` on any invalid config; the middleware does not limp along with a bad config

## Patterns

**Thread-safe state access** — always acquire `app_state.state_lock` before reading or writing shared state fields:
```python
# Correct
with app_state.state_lock:
    pending = app_state.pending_spool
    app_state.pending_spool = None

# Wrong — race condition between check and consume
if app_state.pending_spool:
    pending = app_state.pending_spool
    app_state.pending_spool = None
```

**Publisher pattern** — activation builds a `SpoolEvent` and calls `manager.publish(event)`; it never branches on printer type or calls Moonraker directly:
```python
# Correct
event = SpoolEvent(spool_id=..., action=Action.AFC_LANE, target="lane1", ...)
manager.publish(event)

# Wrong — Moonraker call in activation.py
requests.post(f"{moonraker}/printer/gcode/script", ...)
```

**Background poll loops** — use `threading.Event.wait(timeout=N)` not `time.sleep(N)`; this allows clean shutdown:
```python
# Correct
while not self._stop_event.is_set():
    ...
    self._stop_event.wait(timeout=POLL_INTERVAL)

# Wrong
while True:
    ...
    time.sleep(POLL_INTERVAL)
```

**Error isolation in publishers** — `publish()` must never raise; catch all exceptions internally and return `False`:
```python
def publish(self, event: SpoolEvent) -> bool:
    try:
        return self._dispatch(moonraker, event)
    except Exception:
        logger.exception("KlipperPublisher: unhandled error ...")
        return False
```

**Gcode injection safety** — validate all user-derived strings before interpolating into gcode scripts; use `_validate_color_hex()` for hex colors and `_validate_material()` for material strings:
```python
if not _validate_material(material):
    logger.warning(f"Skipping SET_MATERIAL — invalid material: {material!r}")
    return
```

## Code Readability

All code should look like a senior developer wrote it, not AI. Follow the AFC-Klipper-Add-On style as a reference.

**Comments:**
- Inline comments explain **why**, not what — no `# set x to 5`, instead `# 5MB per log file`
- Comment every non-obvious block with intent: `# Credentials are optional — anonymous connections work for most setups`
- No docstrings on private helpers — use inline comments instead
- Public functions can have short docstrings but keep them practical, not boilerplate

**Structure:**
- Section headers to group related code: `# ── Logging ──────────────────────────────`
- Aligned assignments where it improves scanning: `LOG_FORMAT = ...` / `LOG_FILE = ...`
- Early returns to flatten nesting — check failure cases first, bail out, keep the happy path flat
- Extract duplicated blocks into named helpers — if the same 5+ lines appear twice, pull them out
- Keep functions focused — one concern per function, orchestration functions read like a checklist
- Max nesting depth of 3 — if deeper, extract a helper

**Complexity:**
- Target grade A-B (complexity 1-10) for all new functions
- Grade C (11-15) is acceptable for orchestration functions with legitimate branching
- Grade D+ (16+) requires refactoring before merge

## CHANGELOG

- Always use a proper semver version bump (e.g. `[1.5.9]`), never `[Unreleased]`
- Look at the latest version and increment the patch number
- Include today's date in `YYYY-MM-DD` format
- Format: `## [x.y.z] - YYYY-MM-DD`

## Spoolman Write Paths

**`color_hex` belongs on filament, not spool extras.** Spoolman only allows registered extra fields on spools — `nfc_id` is registered, `color_hex` is not. Color is a property of the filament object.

- Write `color_hex` via `POST /api/v1/filament` with `color_hex` in the body — this is what `_create_filament()` in `spoolman/client.py` does.
- Write `nfc_id` via `PATCH /api/v1/spool/{id}` with `extra: {nfc_id: ...}` — this is what `_write_nfc_id()` does.
- Do not put `color_hex` in spool `extra` fields — Spoolman will reject it.

This applies to both middleware (`spoolman/client.py`) and scanner firmware (`SpoolmanManager.cpp`).

## Testing Workflow

Two-pass testing protocol for all middleware changes:

1. **pytest first** — run automated unit tests locally before deploying to the printer
2. **Printer test** — deploy to printer, scan tags, verify end-to-end behavior

```bash
# Run full test suite (150 tests)
/opt/homebrew/bin/pytest middleware/tests/ -v

# Run a single test file
/opt/homebrew/bin/pytest middleware/tests/test_activation.py -v
```

If any tests fail, fix before deploying. Tests live in `middleware/tests/` — one file per module (e.g. `test_activation.py`, `test_afc_status.py`). Uses `unittest.TestCase` with `unittest.mock`.

## Verify Checklist

Before presenting any code:
- [ ] All new functions have type hints on all parameters and return type
- [ ] Multi-thread access to `app_state` shared fields uses `app_state.state_lock`
- [ ] Moonraker HTTP calls are in `publishers/klipper.py`, not in `activation.py` or `mqtt_handler.py`
- [ ] Background loops use `self._stop_event.wait(timeout=N)`, not `time.sleep(N)`
- [ ] `publish()` implementations catch all exceptions and return `bool`, never raise
- [ ] Color hex strings are validated through `_validate_color_hex()` before use in gcode
- [ ] `tag_writeback_enabled` gate is checked before publishing any MQTT write command
- [ ] New publishers are registered in `spoolsense.py main()` via `publisher_manager.register()`
