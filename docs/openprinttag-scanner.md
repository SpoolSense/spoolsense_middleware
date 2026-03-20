# SpoolSense Scanner Support

SpoolSense supports NFC scanners running the [spoolsense/spoolsense_scanner](https://github.com/spoolsense/spoolsense_scanner) firmware alongside the standard PN532/ESPHome path.

This enables reading **OpenPrintTag** (ISO 15693 / ICODE SLIX2) tags, which carry rich filament metadata — brand, material, color, and remaining weight — directly on the tag. The PN532 cannot read these tags; a PN5180 is required.

---

## How It Works

```
OpenPrintTag NFC tag
       ↓
ESP32-WROOM-32 + PN5180
(spoolsense/spoolsense_scanner firmware)
       ↓
MQTT broker  (spoolsense/<device_id>/tag/state)
       ↓
SpoolSense middleware
       ↓
Spoolman lookup / create / sync   (optional)
       ↓
Moonraker / AFC activation
       ↓
LED color + lock state published
```

SpoolSense reads the tag payload from MQTT, resolves or creates the spool in Spoolman, activates the lane, and publishes color and lock state — the same as the PN532 path. If Spoolman is unreachable, activation still proceeds from tag data alone.

---

## Hardware Required

| Component | Notes |
|-----------|-------|
| ESP32-WROOM-32 DevKit | Standard 38-pin DevKit. Not the ESP32-S3-Zero used for PN532. |
| PN5180 NFC module | Required for ISO 15693 tag support. PN532 cannot read these tags. |
| OpenPrintTag NFC tags | ISO 15693 / ICODE SLIX2 format, pre-written with filament data. |

The PN532 readers used for the standard SpoolSense path continue to work alongside PN5180 scanners — the two paths are independent and can run simultaneously.

---

## Firmware Setup

1. Flash [spoolsense/spoolsense_scanner](https://github.com/spoolsense/spoolsense_scanner) onto an ESP32-WROOM-32 + PN5180.
2. Connect to the scanner's web UI after first boot.
3. Enable the **Home Assistant** integration (despite the name, actual Home Assistant is not required — this just enables MQTT).
4. Enter your MQTT broker IP, port, and credentials — use the same Mosquitto broker as the rest of SpoolSense.

The scanner will begin publishing tag data to:
```
spoolsense/<device_id>/tag/state
```

---

## Finding Your Scanner's Device ID

The `device_id` is automatically assigned by the firmware and appears in the MQTT topic. You do not choose it.

**To find it:**

1. Power on the scanner.
2. Scan any tag (or just power on — the scanner publishes presence events).
3. Watch your MQTT broker for topics matching:
   ```
   spoolsense/+/tag/state
   ```
4. The middle segment is your `device_id`.

**Example:**
```
spoolsense/ab12cd/tag/state
               ↑
          device_id = "ab12cd"
```

You can use any MQTT client to watch topics — MQTT Explorer, `mosquitto_sub`, or Home Assistant's MQTT listener:

```bash
mosquitto_sub -h <broker_ip> -u <user> -P <password> -t "spoolsense/#" -v
```

---

## Config Setup

Add `scanner_lane_map` to your `config.yaml`, mapping each scanner's `device_id` to the lane or toolhead it serves. The mapped value must match an entry in your `toolheads` list.

```yaml
# AFC example
toolheads:
  - "lane1"
  - "lane2"
  - "lane3"
  - "lane4"

scanner_lane_map:
  ab12cd: "lane1"
  ef34gh: "lane2"

# Toolchanger example
toolheads:
  - "T0"
  - "T1"

scanner_lane_map:
  ab12cd: "T0"
  ef34gh: "T1"
```

SpoolSense will error at startup if a mapped lane is not in the `toolheads` list.

The `scanner_topic_prefix` defaults to `"spoolsense"` and does not need to be set unless you have customized the firmware:

```yaml
# Only set this if you changed the prefix in the scanner firmware
scanner_topic_prefix: "spoolsense"
```

---

## Tag-Only Mode

`spoolman_url` is optional. If omitted, SpoolSense runs in **tag-only mode**:

- NFC scans still activate lanes and toolheads
- LED color and lock state are published normally
- Spoolman lookup, spool creation, and weight sync are disabled

This is useful for lightweight setups, testing, or running without a Spoolman instance.

---

## Tag Writeback (Phase 1)

SpoolSense can write updated remaining weight back to OpenPrintTag tags when the tag is out of date.

**When a write occurs:**
- Tag remaining weight is higher than Spoolman's value → tag is stale, write Spoolman value
- Tag is missing remaining weight → write Spoolman value

**When no write occurs:**
- Tag and Spoolman remaining are equal → no action
- Spoolman remaining is higher than the tag → no write (prevents accidental overwrites)
- Spoolman is unavailable → no write (no authoritative value)

Writes are published to the scanner firmware via MQTT:
```
spoolsense/<device_id>/cmd/update_remaining/<uid>
{"remaining_g": <spoolman_remaining>}
```

The scanner firmware handles UID validation, write queueing, and the remaining → consumed weight conversion internally. Tag write failures are logged but never block spool activation.

---

## Troubleshooting

**Scanner not activating lane**

- Check that `scanner_lane_map` contains the correct `device_id` for your scanner.
- Watch MQTT topics to confirm the scanner is publishing:
  ```bash
  mosquitto_sub -h <broker_ip> -u <user> -P <password> -t "spoolsense/#" -v
  ```
- Confirm the mapped lane name matches an entry in your `toolheads` list exactly.
- Check the middleware log for startup warnings:
  ```bash
  journalctl -u spoolsense -f
  ```

**"scanner_lane_map contains lanes not in toolheads" error at startup**

A mapped lane value does not match any entry in your `toolheads` list. Add the lane to `toolheads` or correct the mapping.

**"scanner_lane_map is configured but the rich-tag dispatcher is not available" warning**

The `adapters/` directory is missing from the middleware installation. The scanner topics will be subscribed but payloads will not be parsed. Reinstall or restore the `adapters/` directory.

**Tag scanned but Spoolman lookup fails**

- If `spoolman_url` is not set, SpoolSense is in tag-only mode — Spoolman lookup is disabled by design.
- If `spoolman_url` is set, check that Spoolman is reachable from the Pi and that the NFC UID has been registered in Spoolman's `extra.nfc_id` field.

**Tag write not happening**

- Confirm `spoolman_url` is set and Spoolman is reachable.
- Check that the tag's remaining weight is actually higher than Spoolman's value (write only fires when the tag is stale).
- Check the middleware log for `[tag_sync.scanner_writer]` entries.

---

## Supported Tag Formats

| Format | Reader | Notes |
|--------|--------|-------|
| Plain UID (any NFC tag) | PN532 via ESPHome | Lookup by UID in Spoolman `extra.nfc_id` |
| OpenTag3D | PN532 via ESPHome | Rich metadata read via OpenTag3D Web API |
| OpenPrintTag (ISO 15693) | PN5180 via spoolsense_scanner | Rich metadata + writeback support |
