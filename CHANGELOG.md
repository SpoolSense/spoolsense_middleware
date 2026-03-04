# Changelog

All notable changes to nfc-toolchanger-spoolman are documented here.

---

## [1.2.2] - 2026-03-04

### Added
- **MQTT Last Will and Testament (LWT)** — broker now automatically publishes `false` to `nfc/middleware/online` if the middleware crashes or loses connection unexpectedly, with QoS 1 and retain so subscribers always have current state
- **Online status publishing** — middleware publishes `true` to `nfc/middleware/online` on successful broker connection (fired from `on_connect` callback, not after TCP connect, so it only fires once the broker has acknowledged the connection). On clean shutdown via SIGTERM or SIGINT, publishes `false` before disconnecting
- **Clean shutdown handler** — `SIGTERM` and `SIGINT` now trigger a graceful shutdown that publishes offline status before disconnecting, so a service restart looks different from a crash to any subscribers

### Home Assistant integration
Add a binary sensor to `configuration.yaml` to surface middleware status on your dashboard:
```yaml
mqtt:
  binary_sensor:
    - name: "NFC Middleware"
      state_topic: "nfc/middleware/online"
      payload_on: "true"
      payload_off: "false"
      device_class: connectivity
```

---

## [1.2.1] - 2026-03-03

### Fixed
- **ESPHome 2026.2.x compatibility** — added `chipset: WS2812` to `esp32_rmt_led_strip` config in all 4 toolhead YAML files. ESPHome 2026.2.2 made `chipset` a required field; omitting it caused a compile error: `Must contain exactly one of chipset, bit0_high`

---

## [1.2.0] - 2026-03-02

### Added
- **Configurable low spool threshold** — `LOW_SPOOL_THRESHOLD` variable added to middleware config (default: 100g). Adjust to suit your spool sizes — bump up for an earlier warning, drop down for mini spools.

---

## [1.1.0] - 2026-03-02

### Added
- **LED error indication** — unknown or unregistered NFC tags now trigger 3x red flashes on the toolhead LED, making scan failures immediately obvious
- **Low spool warning** — when a spool has 100g or less remaining, the LED breathes (pulses between 10%–80% brightness) in the filament's colour to draw attention without losing colour context
- **Low spool MQTT topic** — middleware now publishes `true`/`false` to `nfc/toolhead/Tx/low_spool` after each scan, driven by Spoolman's `remaining_weight` field
- **Pulse effect** added to ESPHome light config (`Low Spool Warning` effect, 1s transition)

### Changed
- Middleware now publishes `"error"` instead of `"000000"` to the colour topic when a tag is not found in Spoolman, allowing ESPHome to distinguish between "no spool" and "error" states

---

## [1.0.0] - 2026-02-28

### Initial Release
- NFC-based filament spool tracking for Voron multi-toolhead printers (T0–T3)
- **Hardware**: Waveshare ESP32-S3-Zero + PN532 NFC module (I2C) per toolhead
- **ESPHome firmware** for all 4 toolheads — reads NFC tag UID and publishes to MQTT
- **Python middleware** (`nfc_listener.py`) running on Raspberry Pi — subscribes to MQTT, queries Spoolman by NFC UID, sets active spool in Moonraker, publishes filament colour back to ESP32
- **Klipper macros** for spool tracking and filament usage
- **Spoolman integration** — uses `nfc_id` extra field to map NFC tags to spools
- **LED feedback** — onboard WS2812 RGB LED flashes white 3x on successful scan, then holds the filament's colour from Spoolman
- **Per-toolhead spool display** — supported in both Fluidd and Mainsail via variable_spool_id in toolchange macros
- **MQTT broker** via Home Assistant Mosquitto addon
- **3D printed case** — custom case for Waveshare ESP32-S3-Zero + PN532, modified from MakerWorld model with toolhead labels (T0–T3) and scan target area
