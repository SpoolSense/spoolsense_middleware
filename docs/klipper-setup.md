# Klipper Setup Guide

> **This guide covers toolchanger and single toolhead setups.** AFC/BoxTurtle users: see [afc-setup.md](afc-setup.md) for AFC-specific setup. The AFC LED filament color feature is pending review in [AFC-Klipper-Add-On PR #681](https://github.com/ArmoredTurtle/AFC-Klipper-Add-On/pull/681).

## Add Spoolman Macros

Add the following to your `printer.cfg` (or include `spoolman_macros.cfg`):

```ini
[gcode_macro SET_ACTIVE_SPOOL]
description: Set the active spool in Spoolman via Moonraker
gcode:
  {% if params.ID is defined %}
    {action_call_remote_method("spoolman_set_active_spool", spool_id=params.ID|int)}
  {% endif %}

[gcode_macro CLEAR_ACTIVE_SPOOL]
description: Clear the active spool in Spoolman
gcode:
  {action_call_remote_method("spoolman_set_active_spool", spool_id=None)}
```

## Persist Spool IDs Across Reboots

By default, Klipper macro variables reset to `None` when Klipper restarts (e.g. after a power cut or reboot), meaning you'd have to rescan all your spools. To fix this, we use Klipper's `[save_variables]` system to save spool IDs to disk and restore them automatically on startup.

**Step 1 — Add `[save_variables]` to your `printer.cfg`**

You may already have this if you use klipper-toolchanger offset saving or other plugins. You only need one `[save_variables]` block — do not add a second one.

Toolchanger users:
```ini
[save_variables]
filename: ~/printer_data/config/klipper-toolchanger/offset_save_file.cfg
```

Single toolhead users:
```ini
[save_variables]
filename: ~/printer_data/config/variables.cfg
```

**Step 2 — Add the startup restore macro for your mode**

### Single Toolhead Mode

If you're using `spoolman_macros.cfg`, the `RESTORE_SPOOL` macro is already included — it runs 1 second after Klipper starts and re-activates your last scanned spool automatically. No extra setup needed.

If you're defining macros directly in `printer.cfg` instead of using the include file, add this:

```ini
[delayed_gcode RESTORE_SPOOL]
initial_duration: 1
gcode:
  {% set svv = printer.save_variables.variables %}
  {% if svv.t0_spool_id is defined %}
    SET_ACTIVE_SPOOL ID={svv.t0_spool_id}
  {% endif %}
```

### Toolchanger Mode

Add the `RESTORE_SPOOL_IDS` macro from `toolhead_macros_example.cfg`. This restores all toolheads (T0–T3) on startup:

```ini
[delayed_gcode RESTORE_SPOOL_IDS]
initial_duration: 1
gcode:
  {% set svv = printer.save_variables.variables %}
  # Restore T0 spool ID if previously saved
  {% if svv.t0_spool_id is defined %}
    SET_GCODE_VARIABLE MACRO=T0 VARIABLE=spool_id VALUE={svv.t0_spool_id}
    SET_ACTIVE_SPOOL ID={svv.t0_spool_id}
  {% endif %}
  # Restore T1 spool ID if previously saved
  {% if svv.t1_spool_id is defined %}
    SET_GCODE_VARIABLE MACRO=T1 VARIABLE=spool_id VALUE={svv.t1_spool_id}
  {% endif %}
  # Restore T2 spool ID if previously saved
  {% if svv.t2_spool_id is defined %}
    SET_GCODE_VARIABLE MACRO=T2 VARIABLE=spool_id VALUE={svv.t2_spool_id}
  {% endif %}
  # Restore T3 spool ID if previously saved
  {% if svv.t3_spool_id is defined %}
    SET_GCODE_VARIABLE MACRO=T3 VARIABLE=spool_id VALUE={svv.t3_spool_id}
  {% endif %}
```

The middleware automatically saves spool IDs to disk whenever an NFC scan occurs, so the restore macro will always have up-to-date values after a reboot.

## Update Toolhead Macros (Toolchanger Only)

Add `variable_spool_id: None` to each of your T0-T3 toolchange macros so Fluidd or Mainsail can display and assign spools per toolhead.

Example for T0 (replicate for T1, T2, T3):

```ini
[gcode_macro T0]
variable_color: ""
variable_tool_number: 0
variable_spool_id: None
gcode:
  _CHANGE_TOOL T={tool_number}
  {% if spool_id != None %}
    SET_ACTIVE_SPOOL ID={spool_id}
  {% endif %}
```

## Bondtech INDX

INDX on Klipper presents as a plain toolchanger (T0–T9), so the setup is the
toolchanger flow above with one shared scanner instead of one per tool. Start
from `middleware/config.example.indx.yaml`.

What you need in `printer.cfg`:

- The `ASSIGN_SPOOL` and `UPDATE_TAG` macros from the SpoolSense macro set
- `variable_spool_id: None` on each tool macro (`T0`..`Tn`), exactly as in
  the toolchanger section above
- The `RESTORE_SPOOL_IDS` delayed gcode so assignments survive reboots

Workflow: scan a spool on the shared scanner, then assign it to a tool with
the keypad, web UI, mobile app, or `ASSIGN_SPOOL TOOL=Tn` in the console.

Two things work automatically on INDX:

- **Per-tool deduction** — Bondtech's macros do not create Klipper `tool`
  objects, so `UPDATE_TAG` uses the slicer's per-tool filament weights from
  the job metadata. Nothing extra to install, but your slicer must write
  per-tool filament usage into the gcode (OrcaSlicer and PrusaSlicer do);
  without that metadata the deduction is skipped.
- **Live active-spool sync** — INDX records the mounted tool in
  `save_variables.active_tool`; on every tool pickup the middleware switches
  Spoolman's active spool to the spool assigned to that tool, so usage
  accrues to the spool actually printing. If the picked tool has no spool
  assigned (or the head is parked), Spoolman is left unchanged, so usage
  keeps accruing to the previously active spool until the next assigned
  pickup. Disable with `active_tool_sync: false` in `config.yaml`.

## Restart Klipper

```bash
sudo systemctl restart klipper
```

## Multi-Toolhead Front End Support

Both Fluidd and Mainsail support per-toolhead spool selection when `variable_spool_id` is present in the toolchange macros. Mainsail added this support in July 2024.

---

## Tips

### Installing Fluidd alongside Mainsail

If you prefer Fluidd alongside Mainsail for its per-toolhead spool display:

1. Download Fluidd:
```bash
mkdir -p ~/fluidd && cd ~/fluidd
wget -q -O fluidd.zip https://github.com/fluidd-core/fluidd/releases/latest/download/fluidd.zip
unzip fluidd.zip && rm fluidd.zip
```

2. Create nginx config at `/etc/nginx/sites-available/fluidd` — set `root` to your fluidd path, listen on port 81, and proxy API/websocket requests to Moonraker.

3. Enable and restart nginx:
```bash
sudo ln -s /etc/nginx/sites-available/fluidd /etc/nginx/sites-enabled/fluidd
sudo nginx -t && sudo systemctl restart nginx
```

4. Access Fluidd at `http://YOUR_KLIPPER_IP:81` and add your Spoolman URL in Settings.
