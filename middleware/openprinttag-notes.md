# OpenPrintTag Scanner — Integration Notes & TODO

Reference: [ryanch/openprinttag_scanner](https://github.com/ryanch/openprinttag_scanner)

---

## Hardware requirement

The `openprinttag_scanner` firmware requires an **ESP32-WROOM-32 DevKit-style** board with a PN5180 NFC module attached. This is different from the rest of SpoolSense which uses an ESP32-S3-Zero + PN532.

The PN5180 is required because OpenPrintTag uses ISO 15693 (NFC-V) tags, which the PN532 cannot read.

---

## Why openprinttag_scanner instead of ESPHome + PN5180

The ESPHome community components for the PN5180 only expose the tag UID — not the full CBOR payload that OpenPrintTag stores. Writing a custom ESPHome component to read full tag memory is a significant undertaking. The `openprinttag_scanner` firmware handles the full CBOR decode and publishes ready-to-consume JSON over MQTT, which is the same pattern SpoolSense already uses with ESPHome + PN532.

---

## MQTT setup — Home Assistant config, but HA not required

The scanner's MQTT functionality is controlled by its "Home Assistant" integration toggle. Despite the naming, **actual Home Assistant is not required**. The config just asks for an MQTT broker host, port, and credentials — point it at the same Mosquitto broker SpoolSense already uses.

When enabled, the scanner:
- Publishes tag data to `openprinttag/<deviceId>/tag/state`
- Also publishes Home Assistant discovery messages to `homeassistant/...` — SpoolSense ignores these

Setup steps:
1. Flash the firmware on an ESP32-WROOM-32 DevKit + PN5180
2. Connect to the scanner's web UI
3. Enable the "Home Assistant" integration
4. Enter your MQTT broker IP, port, and credentials (same broker as the rest of SpoolSense)

---

## MQTT topics

| Topic | Content |
|---|---|
| `openprinttag/<deviceId>/tag/state` | Full tag state JSON (see below) — **subscribe to this** |
| `openprinttag/<deviceId>/tag/attributes` | Identical payload to tag/state |
| `openprinttag/<deviceId>/availability` | `online` / `offline` |

SpoolSense subscribes to `tag/state`. Both topics publish the same payload.

---

## Confirmed tag/state payload

Confirmed from source (`HomeAssistantManager.cpp`):

```json
{
  "uid": "04A2B31C5F2280",
  "present": true,
  "tag_data_valid": true,
  "manufacturer": "Prusament",
  "material_type": "PLA",
  "material_name": "Galaxy Black",
  "color": "#1A1A2E",
  "initial_weight_g": 1000.0,
  "remaining_g": 742.0,
  "spoolman_id": -1,
  "blank": false
}
```

### Field notes

- `color` — hex string with `#` prefix (e.g. `"#1A1A2E"`). SpoolSense strips the `#` to the canonical no-prefix format used by ESPHome and Spoolman.
- `remaining_g` — calculated by the firmware as `actual_full_weight - consumed_weight`. The tag itself stores consumed weight, not remaining.
- `spoolman_id` — `-1` means the tag has no Spoolman link. SpoolSense strips this to `None` and stores it as `scanner_spoolman_id` (a hint only — SpoolSense re-resolves via UID).
- `blank` — tag is present but uninitialized.
- `uid` — hardware NFC chip UID. SpoolSense uses this for Spoolman lookup via `extra.nfc_id`.

### Fields the firmware decodes but does NOT publish to MQTT

The firmware decodes these from the tag's CBOR data but the current `HomeAssistantManager` does not include them in the MQTT payload:

- `diameter` (filament diameter in mm)
- `density` (g/cm³)
- `temp_min` / `temp_max` (nozzle temperature range)
- `bed_temp_min` / `bed_temp_max` (bed temperature range)
- `remaining_m` (remaining length in meters, calculated)
- `uuid` / `format_version` / `type`

`ScanEvent` already has fields for all of these. If a future firmware version publishes them, SpoolSense only needs to add the field mappings in `scanner_parser.py`.

---

## Multi-device setup

One PN5180 per ESP32. Multiple scanners on a single ESP32 are not supported by the firmware.

Each ESP32 publishes under its own `deviceId` (derived from the last 3 octets of its WiFi MAC). Map each device to a target in `config.yaml`:

```yaml
scanner_lane_map:
  esp32-t0: T0
  esp32-t1: T1
  scanner-lane1: lane1
```

Works for single-tool, multi-tool/toolchanger, and AFC setups — same config structure, different target values.

---

## Spoolman integration

No `spoolman_id` is embedded in a way SpoolSense trusts directly. SpoolSense re-resolves Spoolman entries via the hardware `uid` — same as the existing PN532 flow. Users register the NFC UID in Spoolman's `extra.nfc_id` field.

Spoolman is **not required**. If no Spoolman entry is found, SpoolSense proceeds with tag data only. Spoolman enriches the record when available but is never a hard dependency.

> **Note:** `spoolman_url` is currently required in `config.yaml` validation (`sys.exit` if missing). This conflicts with the optional-Spoolman design — needs a decision before Phase 2 ships.

---

## Internal event model

Confirmed payload is normalized into `ScanEvent` (see `state/models.py`) by `scan_event_from_openprinttag_scanner()` in `openprinttag/scanner_parser.py`.

SpoolSense is the final authority on activation. The scanner reports what tag is present — SpoolSense decides what to do with it.

### present=False / tag_data_valid=False

The dispatcher returns a `ScanEvent` with the relevant flag set to `False` — it does not raise. The caller (`_handle_rich_tag` in `spoolsense.py`) checks both flags and returns early if either is false.

---

## Current implementation status

### Wired and implemented
- `ScanEvent` model (`state/models.py`)
- `scanner_parser.py` — parses confirmed payload into `ScanEvent`
- `dispatcher.py` — detects format, routes to correct parser
- `spoolsense.py` — dual-path `on_message`, subscribes to scanner topics, calls `_handle_rich_tag`
- `SpoolmanClient.sync_spool_from_scan()` — bridges `ScanEvent` → `SpoolInfo` → Spoolman sync

### Known bugs (not yet tested against hardware)
- `SpoolmanClient._create_spool_from_tag()` is a placeholder — hardcodes `spoolman_id = 99`. Needs real vendor/filament/spool creation logic (see TODO below).
- None of the openprinttag_scanner path has been tested against a real scanner or live MQTT broker yet.

---

## TODO

### Testing — must do before further coding

**Unit tests (no hardware needed)**
- [ ] Run `test_dispatcher.py` — verify all 9 test cases pass
- [ ] Run `test_parsers.py` — verify OpenTag3D and scanner parser output
- [ ] Run `test_db.py` — verify MoonrakerDB save/load (needs Moonraker running)
- [ ] Test `color_map.py` — scan through known Prusament color names and verify hex output. Test edge cases: empty string, already-hex, unknown name, mixed case

**Integration tests (needs MQTT broker + Spoolman)**
- [ ] Plain UID scan end-to-end — PN532 → MQTT → spoolsense.py → Spoolman lookup → activate_spool
- [ ] Verify `DISPATCHER_AVAILABLE=False` graceful degradation — rename/remove `adapters/` and confirm plain UID path still works
- [ ] Verify `scanner_lane_map` subscription — configure a fake scanner mapping, start middleware, confirm it subscribes to correct topics
- [ ] Publish a fake `openprinttag_scanner` payload to MQTT manually and verify dispatcher picks it up and routes correctly

**Hardware tests (needs ESP32-WROOM-32 + PN5180 + openprinttag_scanner)**
- [ ] Flash firmware — does it boot? Does the "Home Assistant" MQTT config accept a plain broker?
- [ ] Scan an OpenPrintTag (ISO 15693 / ICODE SLIX2) tag — capture the raw MQTT payload and verify it matches the confirmed schema
- [ ] Verify `present=False` payload — confirm the dispatcher returns a ScanEvent (not raise) and `_handle_rich_tag` skips activation cleanly
- [ ] Cross-reader test — with two PN5180 readers close together, does one reader pick up a tag meant for the other? Measure actual read distance

### Code — not yet implemented

**`SpoolmanClient._create_spool_from_tag()`** — currently a placeholder that hardcodes `spoolman_id = 99`. Needs:
- [ ] Check if vendor already exists (`GET /api/v1/vendor`)
- [ ] Create vendor if not found (`POST /api/v1/vendor`)
- [ ] Check if filament already exists (filtered by vendor + material + color)
- [ ] Create filament if not found (`POST /api/v1/filament`)
- [ ] Create spool with `filament_id` (`POST /api/v1/spool`)
- [ ] Write NFC UID back to new spool's extra fields (already implemented in `_write_nfc_id()`)

**Config**
- [ ] Decide: should `spoolman_url` remain required, or become optional for tag-only operation? Currently `sys.exit` if missing — conflicts with optional-Spoolman design.
- [ ] Validate `scanner_lane_map` values match entries in `toolheads` list
- [ ] Warn if `scanner_lane_map` is configured but `DISPATCHER_AVAILABLE=False`
- [ ] Add `scanner_lane_map` examples to all three config example files

**Error handling**
- [ ] If Spoolman is down when a rich tag is scanned, `sync_spool` will fail — fall back to tag-only data and still activate?
- [ ] If Moonraker is down, `activate_spool` fails and spool data is lost — queue and retry?
- [ ] MQTT reconnect — does paho-mqtt reconnect automatically on broker drop, or do we need `on_disconnect`?

**Write-back (future)**
- [ ] After a print completes, read remaining weight from Spoolman and write it back to the tag via openprinttag_scanner write commands
- [ ] Requires understanding the scanner's command topic (`openprinttag/<deviceId>/cmd/response`) and what write operations it supports

### Cleanup
- [ ] Remove `middleware_DO_NOT_USE/` once master middleware is confirmed stable
- [ ] Remove `middleware-diff.txt` (development artifact)
- [ ] Add `scanner_lane_map` examples to all three config example files
- [ ] Update CHANGELOG with the openprinttag-support branch work
