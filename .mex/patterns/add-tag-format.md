---
name: add-tag-format
description: Steps to add support for a new NFC tag format or scanner firmware variant.
triggers:
  - "new tag format"
  - "new scanner firmware"
  - "openprinttag"
  - "parser"
  - "tag format"
last_updated: 2026-04-05
---

# Pattern: Add a New Tag Format

## When to Use

When adding support for a new NFC tag format (e.g. OpenPrintTag CBOR direct from PN5180) or a new scanner firmware variant that publishes a different MQTT payload shape.

## Steps

1. **Create `<format_name>/parser.py`** — write a parse function that accepts a raw `payload: dict` and a `target_id: str` and returns a fully populated `ScanEvent` from `state/models.py`:
   ```python
   from state.models import ScanEvent

   def parse_myformat(payload: dict, target_id: str, topic: str = "") -> ScanEvent:
       ...
       return ScanEvent(
           source="myformat",
           target_id=target_id,
           scanned_at=datetime.now(timezone.utc).isoformat(),
           uid=...,
           present=...,
           tag_data_valid=...,
           ...
       )
   ```

2. **Add format detection in `adapters/dispatcher.py`**:
   - Add a detection condition in `detect_format(payload)` — check for payload keys that are unique to the new format
   - Add a routing branch in `detect_and_parse()`:
     ```python
     elif fmt == "myformat":
         return parse_myformat(payload, target_id, topic)
     ```

3. **Update `state/models.py` `ScanSource` type** if adding a new source literal:
   ```python
   ScanSource = Literal["legacy_uid", "spoolsense_scanner", "opentag3d", "myformat"]
   ```

4. **Write tests** — add a test file for the parser and update `test_mqtt_handler.py` to cover the new format path through the dispatcher

## Key ScanEvent Field Rules

- `uid` — the NFC hardware chip UID (hex string, lowercase); used for Spoolman lookup
- `present` — set to `False` when scanner reports tag removed; `mqtt_handler` returns early on `present=False`
- `tag_data_valid` — `True` only when the tag contained readable filament data; `False` for blank tags or read errors
- `blank` — `True` when tag is uninitialized; the UID-only path in `mqtt_handler` checks `not scan.tag_data_valid and not scan.blank`
- `color_hex` — 6-digit uppercase hex, no `#` prefix (e.g. `"1A1A2E"`); derive from color name if not directly available
- All temperature fields use `_c` suffix: `nozzle_temp_min_c`, `nozzle_temp_max_c`, `bed_temp_min_c`, `bed_temp_max_c`
- `remaining_weight_g` and `full_weight_g` are in grams as floats

## Existing Formats (reference)

- **`spoolsense_scanner`** — `openprinttag/scanner_parser.py`; identified by `"present" in payload and "tag_data_valid" in payload`; MQTT JSON from the SpoolSense ESP32 scanner firmware
- **`opentag3d`** — `opentag3d/parser.py`; identified by `"opentag_version"` or `"spool_weight_nominal"` key; JSON from OpenTag3D NFC tags
- **`openprinttag` (spec CBOR direct)** — detected in dispatcher but raises `NotImplementedError`; requires PN5180 ESPHome component that exposes full CBOR tag data (not yet available)
