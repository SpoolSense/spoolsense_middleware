# KTC Macro Changes — Design Notes

> ⚠️ **Everything in this file is assumption and theory at this point. None of this has been tested. Treat it as a starting point for implementation, not a finished solution.**

Design notes for implementing automatic spool activation via klipper-toolchanger (KTC) macros. Suggested by Jim's ID from the #madmax-toolchanger Discord channel.

See ENHANCEMENTS.md (Klipper section) for full context.

---

## The Idea

Currently the T0–T3 macros call `SET_ACTIVE_SPOOL` when a toolchange happens, but only if a `spool_id` is already assigned. The active spool doesn't automatically clear when switching away from a toolhead either — it just stays set until the next toolchange.

The improvement is to make the toolchange sequence explicitly:
1. **Clear** the outgoing toolhead's spool before the change
2. **Activate** the incoming toolhead's spool after the change

This means Spoolman is always tracking the correct spool for whichever toolhead is physically loaded, with no dependency on Fluidd to manage assignments.

---

## What Changes

### spoolman_macros.cfg

No changes needed here — `SET_ACTIVE_SPOOL` and `CLEAR_ACTIVE_SPOOL` already exist and work correctly.

### toolhead_macros_example.cfg

The T0–T3 macros need two small changes:

1. Call `CLEAR_ACTIVE_SPOOL` at the **start** of a toolchange (before `_CHANGE_TOOL`)
2. Call `SET_ACTIVE_SPOOL` at the **end** of a toolchange (after `_CHANGE_TOOL`)

This ensures there's never a window where the wrong spool is being tracked.

---

## What the Updated Macros Look Like

```ini
[gcode_macro T0]
variable_color: ""
variable_tool_number: 0
variable_spool_id: None
gcode:
  # Clear the current spool before switching — stops tracking the outgoing toolhead
  CLEAR_ACTIVE_SPOOL
  # Perform the toolchange
  _CHANGE_TOOL T={tool_number}
  # Activate the incoming toolhead's spool — Spoolman now tracks T0
  {% if spool_id != None %}
    SET_ACTIVE_SPOOL ID={spool_id}
  {% endif %}

[gcode_macro T1]
variable_color: ""
variable_tool_number: 1
variable_spool_id: None
gcode:
  CLEAR_ACTIVE_SPOOL
  _CHANGE_TOOL T={tool_number}
  {% if spool_id != None %}
    SET_ACTIVE_SPOOL ID={spool_id}
  {% endif %}

[gcode_macro T2]
variable_color: ""
variable_tool_number: 2
variable_spool_id: None
gcode:
  CLEAR_ACTIVE_SPOOL
  _CHANGE_TOOL T={tool_number}
  {% if spool_id != None %}
    SET_ACTIVE_SPOOL ID={spool_id}
  {% endif %}

[gcode_macro T3]
variable_color: ""
variable_tool_number: 3
variable_spool_id: None
gcode:
  CLEAR_ACTIVE_SPOOL
  _CHANGE_TOOL T={tool_number}
  {% if spool_id != None %}
    SET_ACTIVE_SPOOL ID={spool_id}
  {% endif %}
```

---

## TOOLHEAD_MODE Awareness

When implementing this, the middleware needs a `TOOLHEAD_MODE` config variable (`"single"` or `"toolchanger"`) so users not running klipper-toolchanger aren't affected. See ENHANCEMENTS.md for details.

The Klipper side should be handled similarly — the `CLEAR_ACTIVE_SPOOL` call at the start of the toolchange macro is harmless for single toolhead users, but the install script should make it clear which parts of the Klipper config are only needed for toolchanger setups.

---

## RESTORE_SPOOL_IDS

No changes needed to the `RESTORE_SPOOL_IDS` delayed_gcode macro — it already restores all toolhead spool IDs from disk on startup, which feeds directly into the updated macros above.

---

## What You Gain

- Spoolman always tracks the correct spool automatically during a print
- No longer dependent on Fluidd to manage spool assignments
- Works with Mainsail, Fluidd, or any other front end
- Single toolhead users are unaffected if `TOOLHEAD_MODE = "single"`

---

## Middleware Changes Required

> ⚠️ Theory — needs testing.

With the KTC macro changes handling `SET_ACTIVE_SPOOL` automatically at toolchange time, the middleware needs to be aware of the mode to avoid redundant or conflicting calls.

**Current middleware behavior on scan:**
1. Look up spool in Spoolman
2. Call `SET_ACTIVE_SPOOL` via Moonraker immediately
3. Save spool ID to disk via `SAVE_VARIABLE`
4. Publish LED color via MQTT

**Problem in toolchanger mode:**
Step 2 becomes redundant. The middleware activates the spool immediately on scan, then the KTC macro clears it and re-activates at the next toolchange anyway. Not broken, but noisy — and could theoretically cause a brief window where the wrong spool is being tracked if a toolchange fires between the scan and the macro running.

**Proposed middleware behavior based on TOOLHEAD_MODE:**

- `TOOLHEAD_MODE = "single"` — no change, keep calling `SET_ACTIVE_SPOOL` on scan as today
- `TOOLHEAD_MODE = "toolchanger"` — skip `SET_ACTIVE_SPOOL` on scan, only save the spool ID to disk and publish the LED color. Let the KTC macro handle activation at toolchange time.

The `SAVE_VARIABLE` call stays in both modes — this is what `RESTORE_SPOOL_IDS` reads on reboot to restore spool assignments after a power cycle.

---

## Print Scenario Walkthrough

> ⚠️ Theory — needs testing.

**Setup:** All 4 spools scanned, spool IDs saved to disk. Print job uses all 4 toolheads.

**What should happen:**

1. Print starts with T0 active → KTC macro fires `SET_ACTIVE_SPOOL` for T0's spool → Spoolman starts tracking filament usage on T0's spool
2. Toolchange to T1 → `CLEAR_ACTIVE_SPOOL` fires first → then `SET_ACTIVE_SPOOL` for T1's spool → Spoolman switches tracking to T1
3. Toolchange to T2 → same pattern → Spoolman tracks T2
4. Toolchange to T3 → same pattern → Spoolman tracks T3
5. Toolchanges back to T0, T1 etc. → continues correctly throughout the print

Filament usage should update correctly at every toolchange since Spoolman is always tracking whichever toolhead is currently active.

**After shutdown:**

Yes — spool assignments survive a shutdown. The middleware saves each toolhead's spool ID to disk via `SAVE_VARIABLE` every time an NFC scan occurs. The `RESTORE_SPOOL_IDS` delayed_gcode macro runs 1 second after Klipper starts and restores all four assignments from disk. You should not need to rescan anything after a reboot or power cut.

**What needs testing:**
- Does `CLEAR_ACTIVE_SPOOL` firing at the start of every toolchange cause any issues with Spoolman's usage tracking mid-print
- Does the timing of `SET_ACTIVE_SPOOL` vs `_CHANGE_TOOL` matter — should activation happen before or after the physical toolchange completes
- Does `RESTORE_SPOOL_IDS` correctly re-activate the last active spool on startup so tracking resumes immediately without needing a scan
