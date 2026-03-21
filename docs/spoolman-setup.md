# Spoolman Setup Guide

> **This guide applies to all setups** (toolchanger, single toolhead, and AFC).

## Add Extra Fields

Spoolman needs custom extra fields to work with SpoolSense. The [installer](https://github.com/SpoolSense/spoolsense-installer) can create these automatically, or you can add them manually.

1. Go to your Spoolman UI (e.g. `http://YOUR_SPOOLMAN_IP:7912`)
2. Go to **Settings → Extra Fields → Spool**
3. Add the following fields:

### Spool Extra Fields

| Key | Name | Field Type | Required |
|-----|------|-----------|----------|
| `nfc_id` | nfc_id | Text | Yes — used to match NFC tags to spools |
| `tag_format` | tag_format | Text | Optional — stores the tag format (OpenPrintTag, TigerTag, OpenTag3D) |

### Filament Extra Fields

Go to **Settings → Extra Fields → Filament** and add:

| Key | Name | Field Type | Required |
|-----|------|-----------|----------|
| `aspect` | aspect | Text | Optional — filament finish (Silk, Matte, etc.) |
| `dry_temp` | dry_temp | Text | Optional — recommended drying temperature |
| `dry_time_hours` | dry_time_hours | Text | Optional — recommended drying time in hours |

## Automatic Spool Registration

When a tagged spool is scanned, the scanner automatically creates or updates the spool entry in Spoolman — no manual data entry needed. The scanner:

- Creates the vendor if it doesn't exist
- Creates or matches the filament (by vendor + material + color)
- Creates the spool with the NFC tag UID in the `nfc_id` field
- If a tag is re-written with different filament, the old spool is archived and a new one created

## Low Spool Warning

The LED breathing effect and low spool warning require Spoolman to track filament usage **by weight in grams**. If a spool is only tracking by length (meters), the middleware cannot read `remaining_weight` and the warning will never fire.

To enable weight-based tracking:

1. Go to Spoolman → **Filaments**
2. Find the filament type used by the spool
3. Edit it and set **Spool Weight** — this is the weight of a full new spool in grams (e.g. `1000` for a 1kg spool)
4. Go to Spoolman → **Spools**
5. Edit the spool and set **Used Weight** to reflect how much has been used, or set **Remaining Weight** directly

Once `spool_weight` is set on the filament profile, Spoolman will calculate and return `remaining_weight` in grams. The middleware compares this against `low_spool_threshold` (default: 100g) and triggers the breathing LED effect if the spool is at or below that level.

> **Note:** If your spool is showing remaining length in meters instead of grams in Mainsail or Fluidd, this means `spool_weight` is not set on the filament profile. Set it and the display will switch to grams.

---

## Moonraker Integration

Add the following to your `moonraker.conf`:

```ini
[spoolman]
server: http://YOUR_SPOOLMAN_IP:7912
sync_rate: 5
```

Then restart Moonraker:
```bash
sudo systemctl restart moonraker
```
