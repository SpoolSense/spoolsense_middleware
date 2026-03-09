# AFC NFC Integration — TODO / Future Ideas

## LED Enhancements

- **Breathing effect for low spool using led_effect plugin** — Currently low
  spool lanes dim to 20%. With the `led_effect` Klipper plugin, we could do a
  true pulsing/breathing animation per lane. Would need separate effect
  definitions for each lane since led_effect operates on the full chain by
  default. Example config:
  ```ini
  [led_effect lane_breathe]
  leds:
      bt_leds
  autostart: false
  frame_rate: 24
  layers:
      breathing  2  0  top  (0.0, 0.0, 0.0)
  ```
  Then in the macro, replace the dimmed SET_LED with:
  ```
  SET_LED LED=bt_leds RED={r} GREEN={g} BLUE={b} INDEX={led_index}
  SET_LED_EFFECT EFFECT=lane_breathe
  ```
  And add `STOP_LED_EFFECTS` in the non-breathing path.

## Hardware

- **ESP32 mount** — need a bracket or enclosure for the ESP32 inside the
  BoxTurtle, possibly under one of the trays
- **PN532 tray redesign** — current STL is a hack, needs a proper parametric
  design with better PN532 retention and cable management

## PN5180 & OpenPrintTag Support

The PN5180 is an NFC reader that supports both ISO 14443 (NTAG/MIFARE) and
ISO 15693 (NFC-V/ICODE), meaning it could read our current NTAG tags AND
OpenPrintTag (Prusa's open standard) ICODE SLIX2 tags. It also supports
FeliCa. Essentially reads everything.

**Why it's interesting:**
- OpenPrintTag stores filament data (material, color, temps, remaining
  filament) directly on the tag — no Spoolman lookup needed
- Tags are writable — remaining filament can be synced back to the tag
  after each print, so data travels with the spool between printers
- Prusa is pushing this as an industry standard, adoption is growing
- PN5180 modules are ~$6-8 on AliExpress (vs $2-3 for PN532)

**Challenges:**
- ESPHome has no PN5180 support — would need a custom ESPHome component
  or Arduino firmware using the PN5180-Library
- PN5180 uses SPI instead of I2C (actually simpler wiring for 4 readers —
  shared MOSI/MISO/SCK bus with one CS pin per reader)
- Cross-lane reads are a real concern in the BoxTurtle — ISO 15693 read
  range is ~10-20cm with the small breakout board antenna, and BoxTurtle
  lanes are only ~5-8cm apart. Would need RF power attenuation, shielding,
  or software filtering by signal strength (RSSI)
- OpenPrintTag data is CBOR-encoded in NDEF records on ISO 15693 tags —
  need to implement NDEF + CBOR parsing on the ESP32
- The OpenPrintTag data format cannot be put on NTAG215 tags (different
  protocol layer, not just different data)

**Possible approach:**
- Use PN5180 so the hardware CAN read both tag types
- Keep NTAG215 for user's own tags (UID lookup via Spoolman, short range,
  no cross-lane issues)
- Read OpenPrintTag data from Prusament spools when detected (auto-create
  spool in Spoolman from tag data)
- Attenuate RF power for ISO 15693 mode to reduce cross-lane reads
- Sync remaining filament back to OpenPrintTag tags on lane eject

**Also worth tracking:**
- OpenTag3D — a competing open standard that uses NTAG213/215/216 (ISO
  14443), so it works with the PN532 we already have. Stores similar data
  to OpenPrintTag but on cheap NTAG tags. Could be a simpler path to
  rich tag data without changing hardware.

## Middleware

- **Lane ejection via Moonraker websocket** — alternative to file watcher,
  subscribe to AFC's Moonraker object namespace for real-time lane state
- **MQTT auto-reconnect** — add `on_disconnect` callback for automatic
  reconnection after broker drops
- **Deprecated MQTT client API** — `mqtt.Client()` without CallbackAPIVersion
  will break in a future paho-mqtt release

## Integration

- **Feature request to AFC/ArmoredTurtle** — ask about native Spoolman
  filament color support for `led_ready` and `led_tool_loaded` states, which
  would eliminate the need for the LED override macro entirely
- **AFC + klipper-toolchanger** — research how to run AFC-Klipper alongside
  klipper-toolchanger on a MadMax setup without filament sensors. There is a
  `multi_extruder` branch on the AFC repo that may support this:
  https://github.com/ArmoredTurtle/AFC-Klipper-Add-On/blob/multi_extruder/CHANGELOG.md
  (mentioned by jimmyjon711 on the ArmoredTurtle Discord). Need to test this
  branch to get the BoxTurtle working with the existing MadMax toolchanger setup.
