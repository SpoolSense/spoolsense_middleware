# KTC Macro Changes — Design Notes

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
