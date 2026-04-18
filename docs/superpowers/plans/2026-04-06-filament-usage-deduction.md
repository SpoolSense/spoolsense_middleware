# Filament Usage Deduction — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deduct filament usage from NFC tags automatically — `UPDATE_TAG` fires in PRINT_END, middleware grabs the last completed job's per-tool weight, sends deductions to scanner via MQTT, scanner stores them and writes to the tag on next scan.

**Architecture:** `UPDATE_TAG` Klipper macro sets a gcode variable → middleware detects via Moonraker websocket (or HTTP poll fallback) → grabs last completed job's `filament_weights` (single toolhead/multi-tool) or reads AFC lane weights → publishes per-spool deduction to scanner via MQTT → scanner persists in `deductions.json` on LittleFS → on next scan, scanner subtracts and writes to tag.

**Tech Stack:** Python 3.10+, paho-mqtt, requests, Moonraker REST API, Klipper gcode macros, ESP32 Arduino (LittleFS, ArduinoJson)

---

## Design Decisions (Locked)

- **UPDATE_TAG fires per print** (in PRINT_END) — each print's usage is attributed to whatever spool was active
- **Last completed job only** — no scan_time tracking, no history summing. Just grab the most recent job.
- **Scanner accumulates** — if multiple prints happen before a spool is scanned, deductions add up in `deductions.json`
- **Tag is source of truth** — scanner writes deduction to tag first, then publishes post-deduction data via MQTT. Middleware syncs to Spoolman as normal.
- **Single toolhead:** `filament_weight_total` from last job
- **Multi-toolhead:** `filament_weights[N]` per tool index, mapped to active spool UIDs
- **AFC:** Read current weight per lane from `/printer/afc/status`, calculate difference from initial tag weight
- **UID-only/TigerTag/OpenSpool:** No-op — Moonraker/Spoolman handles tracking
- **No direct Spoolman update** — Moonraker handles Spoolman sync. Installer issue #28 ensures `[spoolman]` is configured.

## Subsystem Split

This feature spans two repos:
1. **Middleware** (this plan) — macro detection, last job query, MQTT deduction command
2. **Scanner** (separate plan) — persistent deduction store, write-on-scan logic

This plan covers **middleware only**.

---

## File Structure

| Action | File | Responsibility |
|--------|------|----------------|
| Create | `middleware/filament_usage.py` | `UPDATE_TAG` handler: grab last job, calculate per-tool/lane usage, send MQTT deductions |
| Modify | `middleware/app_state.py` | Add `active_spool_weights: dict` to track initial tag weight per tool/lane, `filament_usage_sync` reference |
| Modify | `middleware/mqtt_handler.py` | Record initial tag weight in `app_state` when a tag is scanned |
| Modify | `middleware/moonraker_ws.py` | Subscribe to `gcode_macro UPDATE_TAG`, add `on_update_tag` callback |
| Modify | `middleware/spoolsense.py` | Wire `FilamentUsageSync` into startup, connect websocket callback |
| Modify | `middleware/klipper/spoolsense.cfg` | Add `UPDATE_TAG` macro definition |
| Create | `middleware/tests/test_filament_usage.py` | Unit tests |

---

## Task 1: Track initial tag weight per tool/lane

**Files:**
- Modify: `middleware/app_state.py`
- Modify: `middleware/mqtt_handler.py`

- [ ] **Step 1: Add weight tracking to app_state**

Add after `tag_write_timestamps` in `app_state.py`:

```python
# Initial tag weight per tool/lane — recorded at scan time.
# Used by UPDATE_TAG to calculate deductions for AFC (compare initial vs current).
# Key is target name (e.g. "T0", "lane1"), value is weight in grams.
# Protected by state_lock.
active_spool_weights: dict[str, float] = {}

# Maps target (e.g. "T0", "lane1") to the spool's NFC UID.
# Used by UPDATE_TAG to send deductions to the correct UID.
# Protected by state_lock.
active_spool_uids: dict[str, str] = {}

# Maps target to the device_id of the scanner that scanned it.
# Used by UPDATE_TAG to publish MQTT deduction to the correct scanner.
# Protected by state_lock.
active_spool_devices: dict[str, str] = {}
```

- [ ] **Step 2: Record initial weight and UID on scan in mqtt_handler.py**

In `_handle_rich_tag()`, after the `_activate_from_scan()` call, record the weight:

```python
        # Record initial weight for filament usage tracking (UPDATE_TAG)
        target = _get_scanner_target(scanner_cfg)
        device_id = _extract_scanner_device_id(topic)
        if target and scan.uid and scan.remaining_weight_g is not None:
            with app_state.state_lock:
                app_state.active_spool_weights[target] = scan.remaining_weight_g
                app_state.active_spool_uids[target] = scan.uid.lower()
                app_state.active_spool_devices[target] = device_id or ""
```

For the UID-only path (around line 125, after `activate_spool` succeeds), add similar tracking:

```python
                if remaining is not None:
                    device_id_for_tracking = _extract_scanner_device_id(topic)
                    with app_state.state_lock:
                        app_state.active_spool_weights[target] = remaining
                        app_state.active_spool_uids[target] = uid
                        app_state.active_spool_devices[target] = device_id_for_tracking or ""
```

- [ ] **Step 3: Commit**

```bash
git add middleware/app_state.py middleware/mqtt_handler.py
git commit -m "Track initial tag weight and UID per tool/lane for UPDATE_TAG (#51)"
```

---

## Task 2: Klipper macro

**Files:**
- Modify: `middleware/klipper/spoolsense.cfg`

- [ ] **Step 1: Add UPDATE_TAG macro**

Append to `middleware/klipper/spoolsense.cfg`:

```ini
# Signal the middleware to calculate filament usage and send a deduction
# to the scanner. Add UPDATE_TAG to your PRINT_END macro for automatic
# filament tracking. The scanner stores the deduction and writes it to
# the NFC tag next time that spool is scanned.
[gcode_macro UPDATE_TAG]
variable_pending: 0
gcode:
  SET_GCODE_VARIABLE MACRO=UPDATE_TAG VARIABLE=pending VALUE=1
```

- [ ] **Step 2: Commit**

```bash
git add middleware/klipper/spoolsense.cfg
git commit -m "Add UPDATE_TAG Klipper macro (#51)"
```

---

## Task 3: Filament usage handler

**Files:**
- Create: `middleware/filament_usage.py`
- Create: `middleware/tests/test_filament_usage.py`

- [ ] **Step 1: Write failing tests**

Create `middleware/tests/test_filament_usage.py` with tests for:
- `_fetch_last_job_weights()` — returns per-tool weights from last completed job
- `_fetch_last_job_weights()` — returns None when no completed jobs
- `_handle_update_tag()` — sends MQTT deduction for single toolhead
- `_handle_update_tag()` — sends per-tool deductions for multi-tool
- `_handle_update_tag()` — skips tools with zero usage
- `_handle_update_tag()` — no active spools logs warning
- `_handle_update_tag_afc()` — calculates deduction from AFC weight delta

- [ ] **Step 2: Run tests to verify they fail**

- [ ] **Step 3: Write implementation**

Create `middleware/filament_usage.py`:

Key functions:
- `_fetch_last_job_weights() -> list[float] | None` — GET last completed job, return `filament_weights` array
- `_fetch_afc_weights() -> dict[str, float] | None` — GET `/printer/afc/status`, return `{lane_name: weight}` dict
- `_handle_update_tag()` — main handler:
  - Check if AFC setup → use `_fetch_afc_weights()`, compare to `active_spool_weights`, send deductions
  - Otherwise → use `_fetch_last_job_weights()`, map indices to tools, send deductions
  - For each deduction: publish `{prefix}/{device_id}/cmd/deduct/{uid}` with `{"deduct_g": X}`
  - Clear `pending` variable back to 0
- `_fetch_pending() -> int | None` — poll macro variable (fallback)
- `_clear_pending()` — reset macro to 0
- `FilamentUsageSync` class — websocket callback + HTTP poll fallback (same pattern as `ToolchangerStatusSync`)

- [ ] **Step 4: Run tests to verify they pass**

- [ ] **Step 5: Commit**

```bash
git add middleware/filament_usage.py middleware/tests/test_filament_usage.py
git commit -m "Add filament usage handler for UPDATE_TAG (#51)"
```

---

## Task 4: Wire websocket subscription

**Files:**
- Modify: `middleware/moonraker_ws.py`

- [ ] **Step 1: Add UPDATE_TAG subscription**

In `MoonrakerWebsocket.__init__()`:
```python
self.on_update_tag: Callable[[int], None] | None = None
```

In `_build_subscribe_objects()`:
```python
objects["gcode_macro UPDATE_TAG"] = None
```

In `_dispatch_status()`:
```python
elif key == "gcode_macro UPDATE_TAG" and self.on_update_tag:
    pending = value.get("pending", 0)
    self.on_update_tag(pending)
```

- [ ] **Step 2: Commit**

```bash
git add middleware/moonraker_ws.py
git commit -m "Subscribe to UPDATE_TAG macro via websocket (#51)"
```

---

## Task 5: Wire into startup

**Files:**
- Modify: `middleware/app_state.py`
- Modify: `middleware/spoolsense.py`

- [ ] **Step 1: Add FilamentUsageSync to app_state**

Add TYPE_CHECKING import and field:
```python
from filament_usage import FilamentUsageSync  # in TYPE_CHECKING block
filament_usage_sync: FilamentUsageSync | None = None
```

- [ ] **Step 2: Wire into spoolsense.py main()**

Import, create instance, connect websocket callback, start, add to shutdown.

- [ ] **Step 3: Commit**

```bash
git add middleware/app_state.py middleware/spoolsense.py
git commit -m "Wire FilamentUsageSync into startup (#51)"
```

---

## Task 6: CHANGELOG and version bump

- [ ] **Step 1: Update CHANGELOG.md**

```markdown
## [1.6.0] - 2026-04-06

### Added

- **Filament usage deduction via UPDATE_TAG macro** — add `UPDATE_TAG` to PRINT_END for automatic filament tracking. Middleware grabs per-tool usage from the last completed job and sends deductions to the scanner. Scanner writes updated weight to OpenPrintTag/OpenTag3D tags on next scan. AFC uses real-time lane weight tracking. UID-only/TigerTag/OpenSpool tags are a no-op (Moonraker handles Spoolman sync). (#51)
```

- [ ] **Step 2: Commit**

```bash
git add CHANGELOG.md
git commit -m "Release v1.6.0 (#51)"
```

---

## Open Items (not in this plan)

- **Scanner-side implementation** — `deductions.json` on LittleFS, MQTT `cmd/deduct/{uid}` handler, write-on-scan logic, accumulation. Separate plan in scanner repo.
- **Multi-tool issue** — create GitHub issue for multi-tool UPDATE_TAG support
- **Cancelled print handling** — currently only counts completed jobs. Consider partial extrusion.
