# Klipper Setup — Single Toolhead

> ⚠️ **Beta doc — not yet linked from main documentation.**

This guide covers Klipper setup for single toolhead printers.

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

## Step 2 — Add save_variables to Klipper

Add the following to your `printer.cfg` if you don't already have it:

```ini
[save_variables]
filename: ~/printer_data/config/variables.cfg
```

This is required for the middleware to persist your spool assignment to disk so it survives Klipper restarts and power cuts. You only need one `[save_variables]` block — do not add a second one if you already have it (e.g. from klipper-toolchanger).

---

## Step 3 — Add Spoolman Macros to Klipper

Include `spoolman_macros.cfg` in your `printer.cfg`:

```ini
[include klipper/spoolman_macros.cfg]
```

This defines:
- `SET_ACTIVE_SPOOL` / `CLEAR_ACTIVE_SPOOL` — the macros the middleware uses to tell Spoolman which spool is active
- `RESTORE_SPOOL` — a delayed gcode that runs on startup and re-activates the last scanned spool after a Klipper restart or power cut

Restart Klipper:
```bash
sudo systemctl restart klipper
```

---

## That's it

In single toolhead mode the middleware handles everything automatically on each
NFC scan — no toolchange macros or `variable_spool_id` setup needed.

When you scan a tag:
1. Middleware looks up the spool in Spoolman
2. Calls `SET_ACTIVE_SPOOL` directly via Moonraker
3. Saves the spool ID to disk via `SAVE_VARIABLE`
4. Spoolman starts tracking filament usage against that spool
5. LED updates to the filament colour

After a Klipper restart, the `RESTORE_SPOOL` macro automatically re-activates the last scanned spool — no rescan needed.
