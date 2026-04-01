# Changelog

All notable changes to SpoolSense are documented here.

---

## [1.5.6] - 2026-04-01

### Added

- **Moonraker websocket support** — AFC and toolchanger status sync can now use Moonraker's websocket for real-time updates instead of HTTP polling. Graceful fallback to polling if websocket-client is not installed. Auto-reconnect with exponential backoff. (#11)

### Fixed

- **Toolhead lock never clears on spool eject** — new toolhead_status.py polls Moonraker's /server/spoolman/spool_id every 2 seconds. When spool is ejected via Mainsail, the toolhead lock clears automatically. Covers both toolhead and toolhead_stage modes. Previously required a middleware restart. (#40)

---

## [1.5.5] - 2026-03-28

### Fixed

- **Tag writeback loop prevention** — per-UID cooldown (10s) prevents the middleware from re-writing a tag triggered by its own state republish. Cooldown claim is released on MQTT failure so retries aren't blocked. Closes #21.
- **Atomic toolhead activation** — if SAVE_VARIABLE fails, Spoolman spool_id and gcode variables are rolled back to prevent orphaned assignments that disappear after reboot. Closes #15.

---

## [1.5.4] - 2026-03-28

### Added

- **Lane field in lane_data** — `lane_data` entries now include a `lane` field with the tool number for Orca Slicer filament sync. Added to both klipper publisher and toolchanger paths. (Thanks @mrsimicsak, PR #38)

### Fixed

- **Toolhead lane_data for slicer integration** — `publish_lane_data: true` now works for toolchanger users. When a spool is assigned to a toolhead (T0, T1, etc.) via `ASSIGN_SPOOL` or direct toolhead activation, the spool data (color, material, weight, temps, spool_id) is written to Moonraker's `lane_data` database. Previously only AFC lanes were populated (by AFC itself). Orca Slicer and other slicers now auto-populate tool info for toolchanger setups. Closes #32.
- **Black spool LED display** — Black spools show as dim white (#333333) on LED since black = LED off looks like no spool is scanned. New `display_spoolcolor()` helper centralizes the normalization logic across all 4 color paths.
- **Broken toolchanger_status tests** — tests updated for current API (string tool names, `_fetch_pending_tool`). Added coverage for white color, black→dim white substitution, and lane_data write gating.

### Added

- **Hybrid toolchanger + AFC support** — users with both direct toolheads (T0-T3) and an AFC unit (Box Turtle, NightOwl) can use a single scanner with `afc_stage` action and `publish_lane_data: true`. Scan a spool once, then either call `ASSIGN_SPOOL TOOL=T{n}` for a toolchanger tool or load filament into an AFC lane — whichever action fires first consumes the staged spool. The ASSIGN_SPOOL macro watcher now starts alongside the AFC watcher when `publish_lane_data` is enabled.

---

## [1.5.3] - 2026-03-26

### Added
- **ASSIGN_SPOOL macro** — replaces tool-pickup detection for `toolhead_stage`. Scan a tag, run `ASSIGN_SPOOL TOOL=T5` in Klipper console, spool auto-assigns. Faster and works for any number of tools without physical pickup. Includes `klipper/spoolsense.cfg` macro file — add `[include spoolsense.cfg]` to printer.cfg.
- **UID-only LED color** — scanner LED now shows the filament color from Spoolman when scanning plain NFC tags. Previously LED stayed default since UID-only tags have no color data on the tag.

### Fixed
- **UID-only tags with toolhead_stage** — plain NFC tags now cache correctly for macro assignment instead of trying to activate directly

---

## [1.5.2] - 2026-03-25

### Added
- **Slicer integration** — publish spool data (color, material, weight, nozzle/bed temps) to Moonraker's `lane_data` database. Orca Slicer and other slicers auto-populate tool info. Opt-in via `publish_lane_data: true`. For users without AFC or Happy Hare.
- **Publisher architecture** — spool activation output decoupled from Klipper/Moonraker. New `publishers/` module with SpoolEvent, Publisher protocol, and fan-out dispatcher. Adding new output targets (Prusa, Bambu, etc.) requires one new file.
  - `publishers/base.py` — SpoolEvent dataclass, Publisher ABC, Action enum
  - `publishers/klipper.py` — existing gcode logic extracted from activation.py
  - `publisher_manager.py` — registry, fan-out with error isolation

### Changed
- **activation.py refactored** — builds platform-neutral SpoolEvent and routes through publisher_manager. Still owns lock decisions, staging logic, and active spool tracking.

### Fixed
- **Temp field name mismatch** — parser read `nozzle_temp_min` instead of `nozzle_temp_min_c`, causing temps to always be None in lane_data

---

## [1.5.1] - 2026-03-24

### Fixed
- **SAVE_VARIABLE uppercase rejection** — Klipper requires lowercase variable names. Toolhead names now lowercased (e.g. T0 → t0_spool_id).

### Added
- **Tag-only activation for toolhead action** — scans without Spoolman now send color via SET_GCODE_VARIABLE instead of silently doing nothing.
- **Failed-activation guard** — if Spoolman activation fails (e.g. Moonraker timeout), scanner is not locked, allowing rescan.

---

## [1.5.0] - 2026-03-24

### Added
- **Per-scanner action routing** — replace `toolhead_mode` with a unified `scanners` config. Each scanner declares an action that determines how scans are routed:
  - `afc_stage` — shared scanner for AFC. Scan a spool, load into any lane. AFC assigns automatically. One scanner for all lanes.
  - `afc_lane` — dedicated scanner per AFC lane. Locks scanner until lane is cleared. (Existing behavior, new config format.)
  - `toolhead` — dedicated scanner per toolhead. Sets active spool and saves to Klipper variables.
  - `toolhead_stage` — shared scanner for toolchanger printers using klipper-toolchanger. Scan a spool, pick up any tool, spool auto-assigns. One scanner for all toolheads.
- **AFC status sync via Moonraker API** — replaced the `watchdog` file watcher on `AFC.var.unit` with HTTP polling of Moonraker's `/printer/afc/status` endpoint. No filesystem dependency — middleware can run on a different machine. Removes the `time.sleep(0.5)` race condition workaround.
- **Toolchanger status sync** — polls Moonraker's `toolchanger` object to detect tool pickups for `toolhead_stage` scanners.
- **Pending spool cache** — for shared scanner modes (`afc_stage` and `toolhead_stage`), tag data is cached on scan and automatically pushed when a lane loads or a tool is picked up. Works with and without Spoolman.
- **Legacy config migration** — old `toolhead_mode` + `scanner_lane_map` configs are auto-converted to the new `scanners` format with a deprecation warning.
- **Thread-safe state access** — added `threading.Lock` for lane state mutations across MQTT and polling threads.

### Changed
- **`toolhead_mode` removed** — behavior is now derived from scanner actions. Mixed configs (e.g. `afc_stage` + `toolhead` scanners on the same printer) are supported.
- **`scanner_lane_map` removed** — replaced by `scanners` config section.
- **`afc_var_path` removed** — no longer needed with Moonraker API sync.
- **`watchdog` dependency removed for AFC mode** — Klipper vars file watcher still used for single/toolchanger setups (see #18 for planned API replacement).
- **Config examples updated** — all three example configs (`afc`, `single`, `toolchanger`) rewritten for the new `scanners` format.

### Migration
If you have an existing `config.yaml` with `toolhead_mode` and `scanner_lane_map`, the middleware will auto-convert it on startup and log a deprecation warning. Update your config to the new format when convenient — see `config.example.afc.yaml` for examples.

---

## [1.4.2] - 2026-03-23

### Changed
- **MQTT topics migrated to `spoolsense/` namespace** — all MQTT topics have moved from `nfc/toolhead/...` to `spoolsense/...`. If you have Home Assistant automations, Node-RED flows, or anything else subscribing to the old `nfc/toolhead/` topics, update them to use `spoolsense/` instead. The old topics are no longer published.
- **Middleware refactored into modules** — `spoolsense.py` has been split into focused modules (`config.py`, `activation.py`, `mqtt_handler.py`, `spoolman_cache.py`, `var_watcher.py`, `app_state.py`). No behavior changes — the entry point is still `spoolsense.py`. If you update via `git pull`, no action needed.

### Removed
- **PN532/ESPHome dead code** — removed all legacy PN532 topic parsing, subscriptions, and publishes. The SpoolSense scanner firmware replaced the PN532 setup and these code paths were no longer reachable.

### Fixed
- **Gcode input validation** — color and material values from NFC tags are now validated before being sent as gcode commands, preventing potential command injection via crafted tag data
- **Config file validation** — malformed YAML config files (e.g. a list instead of key-value pairs) now fail with a clear error instead of crashing

### Added
- Type hints on all middleware function signatures

---

## [1.4.1] - 2026-03-21

### Fixed
- **SET_MATERIAL gcode spaces** — material names with spaces (e.g. "Blood red PLA") broke Klipper's gcode parser; spaces are now replaced with underscores

---

## [Unreleased] - 2026-03-13

### Added
- **OpenPrintTag support via spoolsense_scanner** — the PN5180 is a dead end with ESPHome: the available community components only expose the tag UID, not the full CBOR payload that OpenPrintTag requires. [spoolsense/spoolsense_scanner](https://github.com/spoolsense/spoolsense_scanner) reads the full tag data and publishes decoded JSON directly to MQTT — the same pattern SpoolSense already uses with ESPHome + PN532. SpoolSense subscribes to the scanner's MQTT topic and picks up the payload in the middleware, no custom ESPHome component needed.
  - `middleware/openprinttag/scanner_parser.py` — parses the scanner's flattened JSON schema into a normalized `SpoolInfo`. Maps `manufacturer`→`brand`, `color`→`color_hex`, `remaining_g`→`remaining_weight_g`, `initial_weight_g`→`full_weight_g`. Ignores `spoolman_id: -1` (unlinked).
  - `middleware/adapters/dispatcher.py` updated — detects scanner payloads via `present` + `tag_data_valid` keys, guards against `present=False` and `tag_data_valid=False` before parsing, routes to `scanner_parser`. OpenTag3D detection tightened to `opentag_version`/`spool_weight_nominal` to avoid collision with the scanner's `manufacturer` field.
  - `middleware/test_dispatcher.py` updated — three new test cases: valid scanner scan, `present=False`, and `tag_data_valid=False`.

---

## [1.4.0] - 2026-03-12

### Added
- **AFC per-lane ESPHome config** (`integrations/afc/esphome/lane-pn532.yaml`) — standalone ESP32-S3-Zero + PN532 config for BoxTurtle AFC users. Flash one copy per lane, changing `lane_id` and WiFi/IP settings each time. Includes the scan-lock mechanism with a corrected `if:` condition block — the previous config used `return` inside a lambda which did not actually prevent the MQTT publish from firing when a lane was locked.
- **ESPHome directory README** (`esphome/README.md`) — documents which config file to use for each setup, what to edit before flashing, secrets file format, step-by-step first-flash instructions, and a hardware reference table.

---

## [Unreleased] - 2026-03-12

### Added
- **OpenPrintTag and OpenTag3D middleware support (early stages)** — groundwork laid for supporting NFC tags written in the [OpenPrintTag](https://specs.openprinttag.org/) and [OpenTag3D](https://opentag3d.info/spec.html) open standards. This is very early stages — no real scanner, MQTT, or Klipper is involved yet. Development is being done by feeding fake tag payloads directly into the parsers to verify the data pipeline end-to-end before any hardware is wired up.
  - `middleware/state/models.py` — `SpoolInfo` dataclass that normalizes filament data from any tag source into a single common structure (brand, material, color, temps, weights, diameter, lot info, etc.). `SpoolAssignment` dataclass tracks what spool is loaded where (single tool, toolchanger, or AFC lane).
  - `middleware/state/moonraker_db.py` — `MoonrakerDB` class that persists `SpoolInfo` and `SpoolAssignment` objects into Moonraker's key-value database under the `nfc_spoolman` namespace.
  - `middleware/openprinttag/parser.py` — parses decoded OpenPrintTag CBOR payloads into `SpoolInfo`. Handles packed RGBA color conversion to hex and calculates `remaining_weight` from `actual_netto_full_weight - consumed_weight` (remaining weight is not stored directly on the tag).
  - `middleware/opentag3d/parser.py` — parses OpenTag3D Web API JSON payloads into `SpoolInfo`. Field names differ significantly from OpenPrintTag (`manufacturer`, `extruder_temp_min/max`, `spool_weight_nominal`, etc.).
  - `middleware/spoolman/client.py` — `SpoolmanClient` with NFC UID lookup, TTL-based cache (1hr, with forced refresh on miss), tag/Spoolman merge logic (`prefer_tag` flag), weight sync via Spoolman's `used_weight` API field (`used = nominal - remaining`), and NFC UID write-back to Spoolman's `extra.nfc_id` so future scans find the spool without a create attempt.
  - `middleware/config.example.afc.yaml`, `middleware/config.example.single.yaml`, `middleware/config.example.toolchanger.yaml` — split the original `config.example.yaml` into three separate files, one per supported toolhead mode. The original file was renamed to reflect it is AFC-specific.
  - `middleware/test_db.py` — isolated test that saves a fake `SpoolInfo` and `SpoolAssignment` to Moonraker DB to verify the write path (requires a running Moonraker instance).
  - `middleware/test_parsers.py` — fully isolated parser test, no hardware required. Feeds fake tag payloads into both parsers and prints the resulting `SpoolInfo` JSON for inspection.

### Changed
- **Repository restructured for multi-integration support** — the repo is being reorganized to support multiple hardware and firmware ecosystems under a single project. Integration-specific files (ESPHome configs, middleware variants, Klipper macros, docs) are moving into an `integrations/` directory. AFC/Box Turtle support is the first integration landing under this structure (`integrations/afc/`), with [OpenPrintTag](https://github.com/OpenPrintTag) and [OpenTag3D](https://github.com/OpenTag3D) support planned to follow. The core middleware and standard toolchanger/single toolhead setups are not affected. Some paths and doc links may shift during this reorganization — check the README if something looks broken.

---

## [Unreleased] - 2026-03-11

### Added
- **AFC-specific version (Box Turtle)** — a new `afc/` directory contains a variant of SpoolSense for Box Turtle users. This version is not yet functional as it depends on [AFC-Klipper-Add-On PR #671](https://github.com/ArmoredTurtle/AFC-Klipper-Add-On/pull/671), which adds LED lane color support. Once that PR is merged, SpoolSense will be updated to take full advantage of it. Feel free to explore the `afc/` directory in the meantime — a full update will be posted once those changes land.

### Changed
- **Project renamed to SpoolSense** — the repository, middleware script, and service have been renamed from `nfc-toolchanger-spoolman` / `nfc_listener.py` / `nfc-spoolman.service` to `SpoolSense` / `spoolsense.py` / `spoolsense.service`. The install directory is now `~/SpoolSense/`. No functional changes.

---

## [1.3.2] - 2026-03-08

### Fixed
- **ESPHome scan/response race condition** — moved the `mqtt.publish` block to the top of `on_tag` in `base.yaml`, before the white flash animation. Previously the middleware couldn't start its Spoolman lookup until after the ~650ms of white flashes finished, so fast responses from the middleware would collide with the still-running animation and cancel the error/color LED update. Now the UID publishes immediately and the lookup runs in parallel with the flash sequence.
- **Low spool breathing overriding error flash** — when an unknown tag was scanned on a toolhead that previously had a low spool, the `low_spool` topic stayed `true` and the breathing effect would override the red error flash. The middleware now publishes `low_spool: false` on unknown tag scans to clear that state before sending the error color.

### Changed
- **Spoolman spool cache** — middleware now caches all spools locally with a 1-hour TTL instead of querying the Spoolman API on every scan. On cache miss (e.g. newly registered tag), it does a forced refresh. Reduces network overhead for frequent scans.
- **QoS 1 on color and low_spool publishes** — bumped from QoS 0 to QoS 1 to ensure LED state commands are delivered reliably, especially over flaky wifi.
- **Conditional MQTT auth** — `username_pw_set` is now only called when credentials are provided in config, allowing unauthenticated broker setups.

---

## [1.3.1] - 2026-03-07

### Added
- **`RESTORE_SPOOL` macro for single toolhead mode** — `spoolman_macros.cfg` now includes a delayed gcode that re-activates the last scanned spool after a Klipper restart. Previously, single toolhead users lost their active spool assignment on reboot even though the middleware was saving it to disk.
- **Retained MQTT messages for LED persistence** — colour and low spool topics are now published with `retain=True`, so the MQTT broker remembers the last state per toolhead. The ESP32 LED restores to the correct colour automatically after a Klipper restart, ESP32 reboot, or wifi reconnect — no rescan needed.
- **Shared ESPHome base config** — all 4 toolhead YAML files refactored into a single `base.yaml` with shared logic and thin per-toolhead wrappers using ESPHome substitutions. Changes to LED effects, MQTT handlers, or NFC behavior now only need to be made in one place. Dead no-op lambda in the colour handler also removed.
- **Moonraker `update_manager` support** — instructions added to README and middleware-setup.md for automatic updates via Fluidd/Mainsail. The external config file (v1.3.0) makes this possible since `git pull` no longer overwrites user settings.

### Changed
- **ESPHome configs refactored** — `toolhead-t0.yaml` through `toolhead-t3.yaml` are now thin wrappers that include `base.yaml` via `packages: !include`. MQTT broker IP moved from a hardcoded placeholder to `!secret mqtt_broker` — add `mqtt_broker` to your ESPHome secrets file.
- **`SET_GCODE_VARIABLE` gated behind toolchanger mode** — single toolhead printers don't have T0-T3 gcode macros, so the `SET_GCODE_VARIABLE` call now only runs in toolchanger mode. Prevents spurious errors in Moonraker logs for single toolhead users.

---

## [1.3.0] - 2026-03-05

### Added
- **External config file** — all middleware settings now live in `~/SpoolSense/config.yaml` instead of being hardcoded in the Python source. This means `spoolsense.py` is safe to overwrite on updates (`git pull`, Moonraker `update_manager`, etc.) without losing your configuration. The middleware validates the config on startup and exits with clear error messages if required fields are missing or still have placeholder values.
- **`config.example.yaml`** — documented template with all available options and sensible defaults. Copy to `~/SpoolSense/config.yaml` and fill in your values.
- **PyYAML dependency** — `pyyaml` added to required Python packages for config file parsing.
- **Startup config logging** — middleware now logs the loaded config summary (toolhead mode, toolheads, Spoolman/Moonraker URLs, threshold) at startup for easier debugging via `journalctl`.

### Changed
- **Config no longer lives in `spoolsense.py`** — the hardcoded configuration block at the top of the file has been replaced with a `load_config()` function that reads from the external YAML file. Existing users should copy their current values into a new `config.yaml` before updating.
- **`.gitignore`** — `config.yaml` is now ignored so user config is never overwritten by `git pull`.
- **`docs/middleware-setup.md`** — rewritten for the new config file workflow.
- **`scripts/install-beta.sh`** (beta) — updated to write `config.yaml` instead of sed-patching the Python source, and added `pyyaml` to dependency checks.

### Migration from v1.2.x
1. Create your config file: `cp middleware/config.example.yaml ~/SpoolSense/config.yaml`
2. Copy your existing values (MQTT, Spoolman URL, Moonraker URL, etc.) into `config.yaml`
3. Copy the new `spoolsense.py`: `cp middleware/spoolsense.py ~/SpoolSense/`
4. Install pyyaml: `pip3 install pyyaml --break-system-packages`
5. Restart the service: `sudo systemctl restart spoolsense`

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
- **Python middleware** (`spoolsense.py`) running on Raspberry Pi — subscribes to MQTT, queries Spoolman by NFC UID, sets active spool in Moonraker, publishes filament colour back to ESP32
- **Klipper macros** for spool tracking and filament usage
- **Spoolman integration** — uses `nfc_id` extra field to map NFC tags to spools
- **LED feedback** — onboard WS2812 RGB LED flashes white 3x on successful scan, then holds the filament's colour from Spoolman
- **Per-toolhead spool display** — supported in both Fluidd and Mainsail via variable_spool_id in toolchange macros
- **MQTT broker** via Home Assistant Mosquitto addon
- **3D printed case** — custom case for Waveshare ESP32-S3-Zero + PN532, modified from MakerWorld model with toolhead labels (T0–T3) and scan target area
