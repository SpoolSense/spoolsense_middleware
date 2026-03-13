# OpenPrintTag Scanner Integration Notes

Reference: [ryanch/openprinttag_scanner](https://github.com/ryanch/openprinttag_scanner)

---

## Hardware requirement

The `openprinttag_scanner` firmware requires an **ESP32-WROOM-32 DevKit-style** board with a PN5180 NFC module attached. This is different from the rest of SpoolSense which uses an ESP32-S3-Zero + PN532.

The PN5180 is required because OpenPrintTag uses ISO 15693 (NFC-V) tags, which the PN532 cannot read.

---

## Why openprinttag_scanner instead of ESPHome + PN5180

The ESPHome community components for the PN5180 only expose the tag UID — not the full CBOR payload that OpenPrintTag stores. Writing a custom ESPHome component to read full tag memory is a significant undertaking. The `openprinttag_scanner` firmware handles the full CBOR decode and publishes ready-to-consume JSON over MQTT, which is the same pattern SpoolSense already uses with ESPHome + PN532.

---

## MQTT topics

The scanner publishes to two topics per device:

| Topic | Content |
|---|---|
| `openprinttag/<deviceId>/tag/uid` | Simple string — the raw NFC hardware UID |
| `openprinttag/<deviceId>/tag/attributes` | Full decoded JSON (see below) |

SpoolSense subscribes to `tag/attributes` only. It contains the UID and all tag data in one message.

---

## tag/attributes payload shape

```json
{
  "uid": "04A2B31C5F2280",
  "type": "OpenPrintTag",
  "material": "PLA",
  "color": "Galaxy Black",
  "brand": "Prusament",
  "diameter": 1.75,
  "weight_g": 1000,
  "remaining_g": 742,
  "remaining_m": 247,
  "density": 1.24,
  "temp_min": 200,
  "temp_max": 220,
  "bed_temp": 60,
  "fan_speed": 100,
  "flow": 100,
  "uuid": "c1d3e8f0-1234-4bcd-9a12-abcdef123456",
  "timestamp": 1712345678,
  "valid": true,
  "format_version": 1
}
```

### Field notes

- `uid` — hardware NFC chip UID. SpoolSense uses this to look up Spoolman (`extra.nfc_id`).
- `uuid` — UUID written into the tag by the OpenPrintTag spec. Different from `uid`.
- `color` — a human-readable name like "Galaxy Black", not a hex code. SpoolSense derives `color_hex` via a keyword lookup table.
- `bed_temp` — single value (no min/max). SpoolSense sets both `bed_temp_min_c` and `bed_temp_max_c` to this value.
- `remaining_m` — remaining length in **meters**. SpoolSense converts to `remaining_length_mm` by multiplying × 1000.
- `timestamp` — Unix timestamp of when the tag was written, not when it was scanned.
- `valid` — whether the tag data decoded cleanly. SpoolSense maps this to `tag_data_valid`.
- `fan_speed` / `flow` — printer hint fields on the tag. Not currently used by SpoolSense (Klipper/Moonraker defaults apply).

---

## Multi-device setup

One PN5180 per ESP32. Multiple scanners on a single ESP32 are not supported by the firmware.

Users with multiple scan points (e.g. toolchanger, AFC lanes) flash one ESP32+PN5180 per slot and give each device a unique `deviceId`. Each device maps to a target in SpoolSense config:

```yaml
scanners:
  - topic: openprinttag/scanner-t0/tag/attributes
    target_type: tool
    target_id: T0
  - topic: openprinttag/scanner-t1/tag/attributes
    target_type: tool
    target_id: T1
  - topic: openprinttag/scanner-lane1/tag/attributes
    target_type: afc_lane
    target_id: lane1
```

Works for single-tool, multi-tool/toolchanger, and AFC setups — same config structure, different `target_type` and `target_id` values.

---

## Spoolman integration

`spoolman_id` is not embedded in the tag or the scanner payload. SpoolSense resolves Spoolman entries via the hardware `uid` — same as the existing PN532 flow. Users register the NFC UID in Spoolman's `extra.nfc_id` field.

Spoolman is **not required**. If no Spoolman entry is found for a UID, SpoolSense proceeds with tag data only. Spoolman enriches the record when available (notes, organization fields, etc.) but is never a hard dependency.

---

## Internal event model

The scanner payload is normalized into a `ScanEvent` (see `state/models.py`) by `scan_event_from_openprinttag_scanner()` in `openprinttag/scanner_parser.py`.

SpoolSense is the final authority on activation. The scanner reports what tag is present — SpoolSense decides what to do with it.

### tag_data_valid=False

If `valid=False` arrives in the payload (tag detected but data unreadable), the dispatcher returns a `ScanEvent` with `tag_data_valid=False` and `spool_info` fields all None. The caller decides how to handle it — typically log and skip activation.

---

## Color name → hex lookup

The scanner publishes color as a name ("Galaxy Black", "Urban Grey", etc.). SpoolSense maps this to a hex code via a keyword substring lookup in `scanner_parser.py`. If no keyword matches, `color_hex` is left as `None`.

Common mappings: black→`#000000`, white→`#FFFFFF`, grey/gray/silver→`#808080`/`#C0C0C0`, gold→`#FFD700`, etc.

---

## What is not yet implemented

- MQTT subscription wiring (Phase 2)
- Config file support for scanner topic → target mapping
- Single-tool, multi-tool, and AFC backend adapters
- Startup restore from Moonraker DB
