# Middleware TODO — feature/openprinttag-support Branch

## Status

The dispatcher, parsers, SpoolmanClient, and MoonrakerDB are built and wired
into `spoolsense.py`. The dual-path `on_message` handler routes plain UID scans
through the original Spoolman lookup and rich-data scans through the dispatcher.
None of this has been tested against real hardware or a live MQTT broker yet.

---

## Testing — Must Do Before Further Coding

### Unit Tests (no hardware needed)
- [ ] Run `test_dispatcher.py` — verify all 8 test cases pass
- [ ] Run `test_parsers.py` — verify OpenTag3D and scanner parser output
- [ ] Run `test_db.py` — verify MoonrakerDB save/load (needs Moonraker running)
- [ ] Test `color_map.py` — scan through known Prusament color names and verify
      hex output matches expectations. Test edge cases: empty string, already-hex,
      unknown name, mixed case

### Integration Tests (needs MQTT broker + Spoolman)
- [ ] Plain UID scan end-to-end — PN532 scans NTAG tag → MQTT → spoolsense.py
      → Spoolman lookup → activate_spool → verify Klipper gets the right spool
- [ ] Verify `DISPATCHER_AVAILABLE=False` graceful degradation — rename/remove
      the `adapters/` folder and confirm the middleware starts and handles plain
      UID scans normally with no errors
- [ ] Verify scanner_lane_map subscription — configure a fake scanner mapping,
      start the middleware, confirm it subscribes to the correct MQTT topics
- [ ] Publish a fake openprinttag_scanner payload to MQTT manually and verify
      the dispatcher picks it up, parses it, and routes correctly

### Hardware Tests (needs PN5180 + openprinttag_scanner)
- [ ] Flash openprinttag_scanner on ESP32 + PN5180 — does it boot?
- [ ] Scan an ICODE SLIX2 / OpenPrintTag tag — capture the raw MQTT payload
      and verify it matches what `scanner_parser.py` expects
- [ ] Verify the `color` field format — is it a name ("Galaxy Black"), hex,
      or something else? Adjust `scanner_parser.py` and `color_map.py` if needed
- [ ] Test `present=False` and `tag_data_valid=False` payloads — confirm the
      dispatcher rejects them cleanly
- [ ] Cross-lane read test — with two PN5180 readers close together, does one
      reader pick up a tag meant for the other? Measure actual read distance

---

## Code — Not Yet Implemented

### SpoolmanClient._create_spool_from_tag()
Currently a placeholder that hardcodes `spoolman_id = 99`. Needs:
- [ ] Check if vendor already exists in Spoolman (GET `/api/v1/vendor`)
- [ ] Create vendor if not found (POST `/api/v1/vendor`)
- [ ] Check if filament already exists (GET `/api/v1/filament` filtered by
      vendor + material + color)
- [ ] Create filament if not found (POST `/api/v1/filament`)
- [ ] Create spool with filament_id (POST `/api/v1/spool`)
- [ ] Write NFC UID back to the new spool's extra fields (already implemented
      in `_write_nfc_id()`)

### Scanner MQTT Topic Verification
- [ ] Confirm the actual topic format from openprinttag_scanner — is it
      `openprinttag/<deviceId>/tag/state` or something different?
- [ ] Confirm the full payload schema — document all fields and types
- [ ] Check if the scanner publishes on tag removal (for lock/clear lifecycle)

### Config Validation
- [ ] Validate `scanner_lane_map` values match entries in `toolheads` list
- [ ] Warn if `scanner_lane_map` is configured but `DISPATCHER_AVAILABLE=False`
- [ ] Add `scanner_lane_map` to the config example files

### Error Handling
- [ ] What happens if Spoolman is down when a rich tag is scanned? Currently
      `sync_spool` will fail — should we fall back to tag-only data and still
      activate the spool?
- [ ] What happens if Moonraker is down? `activate_spool` will fail but the
      spool data is lost — should we queue it and retry?
- [ ] MQTT reconnect handling — if the broker drops, does paho-mqtt reconnect
      automatically or do we need `on_disconnect` callback?

### Write-Back (Future)
- [ ] After a print completes, read remaining weight from Spoolman and write
      it back to the OpenPrintTag tag via openprinttag_scanner
- [ ] Requires understanding how openprinttag_scanner handles write commands —
      does it accept MQTT commands to write tag data?

---

## Cleanup

- [ ] Remove `middleware_DO_NOT_USE/` once master middleware is confirmed stable
- [ ] Remove `middleware-diff.txt` (development artifact)
- [ ] Update `middleware/spoolsense.service` if the config path changed
- [ ] Add `scanner_lane_map` examples to all three config example files
- [ ] Update the main README to reflect the SpoolSense rename
- [ ] Update CHANGELOG with the openprinttag-support branch work
