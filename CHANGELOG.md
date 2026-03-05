# Changelog

All notable changes to nfc-toolchanger-spoolman are documented here.

---

## [1.2.2] - 2026-03-04

### Added
- **`TOOLHEAD_MODE` config variable** — middleware now supports `"single"` and `"toolchanger"` modes. Single mode works exactly as before — scan a tag, set the active spool, done. Toolchanger mode stores spool IDs per toolhead via `SAVE_VARIABLE` and lets the Klipper toolchange macros handle `SET_ACTIVE_SPOOL` / `CLEAR_ACTIVE_SPOOL` automatically at each toolchange.
- **MQTT Last Will and Testament (LWT)** — broker now automatically publishes `false` to `nfc/middleware/online` if the middleware crashes or loses connection unexpectedly, with QoS 1 and retain so subscribers always have current state
- **Online status publishing** — middleware publishes `true` to `nfc/middleware/online` on successful broker connection. On clean shutdown via SIGTERM or SIGINT, publishes `false` before disconnecting
- **Clean shutdown handler** — `SIGTERM` and `SIGINT` now trigger a graceful shutdown that publishes offline status before disconnecting, so a service restart looks different from a crash to any subscribers

Optionally surface middleware status in Home Assistant — see [middleware-setup.md](docs/middleware-setup.md) for the binary sensor config.

### Changed
- **`TOOLHEADS` config variable** — replaces the hardcoded `["T0", "T1", "T2", "T3"]` list in the subscribe loop. Adjust to match your setup — single toolhead users set `["T0"]`, larger toolchanger setups add entries as needed.

### Confirmed
- **Automatic spool tracking works for toolchanger users** — tested and confirmed that Spoolman correctly tracks filament usage per spool throughout a multi-toolhead print with no Klipper macro changes needed.

### Removed
- `beta/ktc-macro.md` — design doc for KTC macro changes, removed as the behavior it described is already handled natively by klipper-toolchanger

---

## [1.2.1] - 2026-03-03

### Fixed
- **ESPHome 2026.2.x compatibility** — added `chipset: WS2812` to `esp32_rmt_led_strip` config in all 4 toolhead YAML files. ESPHome 2026.2.2 made `chipset` a required field; omitting it caused a compile error: `Must contain exactly one of chipset, bit0_high`

---

## [1.2.0] - 2026-03-02

### Added
- **Configurable low spool threshold** — `LOW_SPOOL_THRESHOLD` variable added to middleware config (default: 100g). Adjust to suit your spool sizes — bump up for an earlier warning, drop down for mini spools.
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
