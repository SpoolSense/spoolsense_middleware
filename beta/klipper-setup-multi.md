# Klipper Setup — Multi-Toolhead (Toolchanger)

> ⚠️ **Beta doc — not yet linked from main documentation.**

This guide covers Klipper setup for multi-toolhead printers running
klipper-toolchanger (e.g. MadMax, StealthChanger).

---

## Step 1 — Moonraker Spoolman Integration

Add the following to your `moonraker.conf` if you haven't already:

```ini
[spoolman]
server: http://YOUR_SPOOLMAN_IP:7912
sync_rate: 5
```

Restart Moonraker:
```bash
sudo systemctl restart moonraker
```

---

## Step 2 — Add Spoolman Macros to Klipper

Include `spoolman_macros.cfg` in your `printer.cfg`:

```ini
[include klipper/spoolman_macros.cfg]
```

This defines `SET_ACTIVE_SPOOL` and `CLEAR_ACTIVE_SPOOL` which your toolchange
macros call at each toolchange.

---

## Step 3 — Update Your Toolchange Macros

Check `klipper/toolhead_macros_example.cfg` for the full example. You need to
add two things to each of your existing T0–TX macros in your
klipper-toolchanger config directory:

**1. Add `variable_spool_id: None`** to the variable block:

```ini
[gcode_macro T0]
variable_color: ""
variable_tool_number: 0
variable_spool_id: None    # ← add this
gcode:
  ...
```

**2. Add the `SET_ACTIVE_SPOOL` call** at the end of the gcode block:

```ini
gcode:
  _CHANGE_TOOL T={tool_number}
  {% if spool_id != None %}    # ← add this
    SET_ACTIVE_SPOOL ID={spool_id}
  {% endif %}
```

Repeat for each toolhead (T1, T2, T3, etc.).

---

## Step 4 — Persist Spool IDs Across Reboots

Add the `RESTORE_SPOOL_IDS` delayed gcode macro so spool assignments survive
a reboot without needing to rescan. See `klipper/toolhead_macros_example.cfg`
for the full macro.

This requires `[save_variables]` in your `printer.cfg` — you likely already
have this if you use klipper-toolchanger's offset saving:

```ini
[save_variables]
filename: ~/printer_data/config/klipper-toolchanger/offset_save_file.cfg
```

---

## Restart Klipper

```bash
sudo systemctl restart klipper
```

---

## How It Works

In toolchanger mode the middleware does NOT call `SET_ACTIVE_SPOOL` directly
on scan. Instead:

1. NFC scan → middleware saves spool ID to Klipper variable and disk
2. Toolchange fires → T0/T1/etc. macro calls `SET_ACTIVE_SPOOL` for the
   incoming toolhead automatically
3. Spoolman switches tracking to the correct spool
4. LED updates to the filament colour

This means Spoolman always tracks whichever toolhead is physically loaded,
with no manual intervention required during a print.
