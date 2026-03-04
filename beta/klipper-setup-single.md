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

## Step 2 — Add Spoolman Macros to Klipper

Include `spoolman_macros.cfg` in your `printer.cfg`:

```ini
[include klipper/spoolman_macros.cfg]
```

This defines the `SET_ACTIVE_SPOOL` and `CLEAR_ACTIVE_SPOOL` macros that the
middleware uses to tell Spoolman which spool is currently active.

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
3. Spoolman starts tracking filament usage against that spool
4. LED updates to the filament colour
