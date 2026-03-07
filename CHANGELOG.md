# Changelog

All notable changes to nfc-toolchanger-spoolman are documented here.

---

## [1.3.0] - 2026-03-05

### Added
- **External config file** — all middleware settings now live in `~/nfc_spoolman/config.yaml` instead of being hardcoded in the Python source. This means `nfc_listener.py` is safe to overwrite on updates (`git pull`, Moonraker `update_manager`, etc.) without losing your configuration. The middleware validates the config on startup and exits with clear error messages if required fields are missing or still have placeholder values.
- **`config.example.yaml`** — documented template with all available options and sensible defaults. Copy to `~/nfc_spoolman/config.yaml` and fill in your values.
- **PyYAML dependency** — `pyyaml` added to required Python packages for config file parsing.
- **Startup config logging** — middleware now logs the loaded config summary (toolhead mode, toolheads, Spoolman/Moonraker URLs, threshold) at startup for easier debugging via `journalctl`.
- **`RESTORE_SPOOL` macro for single toolhead mode** — `spoolman_macros.cfg` now includes a delayed gcode that re-activates the last scanned spool after a Klipper restart. Previously, single toolhead users lost their active spool assignment on reboot even though the middleware was saving it to disk.
- **Retained MQTT messages for LED persistence** — colour and low spool topics are now published with `retain=True`, so the MQTT broker remembers the last state per toolhead. The ESP32 LED restores to the correct colour automatically after a Klipper restart, ESP32 reboot, or wifi reconnect — no rescan needed.
- **Shared ESPHome base config** — all 4 toolhead YAML files refactored into a single `base.yaml` with shared logic and thin per-toolhead wrappers using ESPHome substitutions. Changes to LED effects, MQTT handlers, or NFC behavior now only need to be made in one place. Dead no-op lambda in the colour handler also removed.

### Changed
- **Config no longer lives in `nfc_listener.py`** — the hardcoded configuration block at the top of the file has been replaced with a `load_config()` function that reads from the external YAML file. Existing users should copy their current values into a new `config.yaml` before updating.
- **ESPHome configs refactored** — `toolhead-t0.yaml` through `toolhead-t3.yaml` are now thin wrappers that include `base.yaml` via `packages: !include`. MQTT broker IP moved from a hardcoded placeholder to `!secret mqtt_broker` — add `mqtt_broker` to your ESPHome secrets file.
- **`.gitignore`** — `config.yaml` is now ignored so user config is never overwritten by `git pull`.
- **`docs/middleware-setup.md`** — rewritten for the new config file workflow.
- **`scripts/install-beta.sh`** (beta) — updated to write `config.yaml` instead of sed-patching the Python source, and added `pyyaml` to dependency checks.

### Migration from v1.2.x
1. Create your config file: `cp middleware/config.example.yaml ~/nfc_spoolman/config.yaml`
2. Copy your existing values (MQTT, Spoolman URL, Moonraker URL, etc.) into `config.yaml`
3. Copy the new `nfc_listener.py`: `cp middleware/nfc_listener.py ~/nfc_spoolman/`
4. Install pyyaml: `pip3 install pyyaml --break-system-packages`
5. Restart the service: `sudo systemctl restart nfc-spoolman`

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
