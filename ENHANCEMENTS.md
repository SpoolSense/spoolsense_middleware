# Future Enhancements

Ideas and known improvements for future development. Contributions welcome.

For features actively being designed and prototyped, check the `beta/` folder.

---

## Reliability

**Watchdog timer on ESP32**
If the MQTT connection drops or the NFC reader hangs, the ESP32 currently just sits there. A watchdog timer would auto-restart the device after a set timeout, so it self-recovers without needing a manual power cycle.

**Retry logic in middleware**
If Spoolman or Moonraker is temporarily unreachable (reboot, network blip), the middleware currently just logs an error and moves on. Should implement retry logic with a short backoff so transient failures don't result in a missed scan.

**Debounce NFC reads**
A tag left sitting on the reader will spam repeated scan events continuously. The middleware or ESPHome should debounce this — once a UID has been successfully processed, ignore repeated scans of the same UID for a few seconds before allowing it to trigger again.

---

## Middleware

**Smarter Spoolman lookups**
`find_spool_by_nfc` currently fetches every spool from Spoolman on every scan and loops through them in Python to find a match. Spoolman's API supports filtering — we should query directly by NFC ID instead of pulling the whole list. Not a problem at 10 spools, starts to feel sloppy at 50+.

**MQTT auto-reconnect**
The middleware has no reconnect logic. If the MQTT broker goes down (Home Assistant update, power blip, whatever), the script dies and stays dead until you manually restart it. Needs an `on_disconnect` callback with automatic reconnect so it just heals itself.

~~**Configurable low spool threshold**
The 100g low spool warning is hardcoded. Should be a variable at the top of the config alongside the other settings — people running 250g mini spools have very different needs than someone running 3kg spools.~~ ✅ Done — `LOW_SPOOL_THRESHOLD` added to middleware config.

~~**External config file**
All configuration (MQTT credentials, Spoolman/Moonraker URLs, toolhead mode, toolheads list, low spool threshold) was hardcoded at the top of `spoolsense.py`. This meant every `git pull` or update could overwrite the user's settings, and made it impossible to use Moonraker's `update_manager` for automatic updates. Config should live in a separate file that's never touched by git.~~ ✅ Done — middleware now loads all settings from `~/SpoolSense/config.yaml`. The Python source is safe to overwrite on updates. See `config.example.yaml` for the documented template.

---

## ESPHome

**Fix scan flash vs. MQTT publish order**
Right now the white flash plays out fully before the MQTT publish fires. That means there's a window where the flash is done but the LED hasn't updated to the spool color yet. The publish should fire immediately when the tag is scanned, with the flash happening in parallel while the middleware does its work.

~~**Remove dead lambda in color handler**
There's a leftover no-op lambda at the top of the color MQTT handler that does nothing. Just noise — should be cleaned up.~~ ✅ Done — removed in the base.yaml refactor.

**`on_tag_removed` handling**
The PN532 supports an `on_tag_removed` event that fires when a tag leaves the reader. Right now the LED holds the last color indefinitely after you pull the spool away. Could dim the LED or turn it off when the tag is removed to make it clearer nothing is actively on the reader.

~~**Single shared base config**
All 4 ESPHome YAML files are nearly identical — the only real differences are the toolhead name, static IP, and topic names. Any change to shared logic (like the LED effects we just added) has to be copy-pasted across all 4 files. ESPHome supports `!include` and packages — a single `base.yaml` with all the logic, and each toolhead file just defines its name and IP, would make maintenance much cleaner.~~ ✅ Done — `base.yaml` contains all shared logic, toolhead files are thin wrappers with substitutions. Dead lambda in color handler also removed.

---

## Home Assistant Integration

**Push notifications for low spool and unknown tags**
The middleware already knows when a spool is low or when an unknown tag is scanned — it's just logging it to the console. It could publish to Home Assistant's notification service at the same time so you get a phone alert rather than relying on noticing the LED.

---

## Klipper

~~**`TOOLHEAD_MODE` config variable for single vs. toolchanger setups**
Automatic spool activation via toolchange macros already works correctly for klipper-toolchanger users — tested and confirmed that `SET_ACTIVE_SPOOL` / `CLEAR_ACTIVE_SPOOL` fire on every toolchange and Spoolman tracks filament usage per-spool throughout a multi-toolhead print. No Klipper macro changes needed for toolchanger users. `TOOLHEAD_MODE` config variable added to middleware (`"single"` or `"toolchanger"`). Single mode calls `SET_ACTIVE_SPOOL` directly on scan. Toolchanger mode skips it and lets klipper-toolchanger handle activation at each toolchange.~~ ✅ Done — `TOOLHEAD_MODE` added to middleware in v1.2.2.

## Installation

**Interactive install script**
Right now setup requires manually editing config files, copying files to the right places, and setting up the systemd service — a lot of steps that are easy to get wrong. An interactive install script would walk the user through everything after a `git clone`.

When run, it would prompt for all the values that currently require manual editing:

- Single toolhead or toolchanger mode (`TOOLHEAD_MODE`) — asked first, drives everything else
- Number of toolheads (if toolchanger mode)
- MQTT broker IP, username, and password
- Spoolman IP and port
- Moonraker/Klipper IP
- Low spool threshold

Once the user answers the prompts, the script would write out the configured `spoolsense.py`, install dependencies, copy the systemd service file, and start the service — all in one shot. At the end it could do a quick connectivity check against the MQTT broker and Spoolman to confirm everything is reachable before exiting.

The goal is: `git clone` → `./install.sh` → answer a few questions → done.

## Quality of Life

**Klipper error alerts via LED**
When something goes wrong mid-print — filament jam, runout, pause — Klipper could publish an MQTT message to the affected toolhead's ESP32 to trigger a visual alert on the LED. ESPHome already subscribes to MQTT topics and knows how to blink LEDs, so this fits naturally into the existing architecture.

Potential alert states:
- Slow red blink — filament jam detected on this toolhead (SFS sensor triggered)
- Fast red blink — more urgent error requiring immediate attention
- Yellow pulse — print paused, waiting for user input
- LED returns to spool color automatically when the issue is cleared and print resumes

Since each toolhead has its own LED, alerts would be per-toolhead — if T2 jams, only T2 blinks red. No need to look at a screen to know which toolhead needs attention.

The main thing to validate is whether Moonraker's built-in MQTT client can publish arbitrary messages directly from a gcode macro, or whether the middleware needs to handle the publishing. The ESPHome and LED side of this should be straightforward given what's already built.

## Future Platform Support

**Bondtech INDX compatibility**
A longer term goal is to get this project working with the Bondtech INDX system once it's publicly available (retail sales expected Q2 2026). INDX supports up to 8 toolheads and is firmware-agnostic — it works with Klipper, Marlin, and RRF — making it a natural fit for this project.

For Klipper-based printers running INDX (Voron, custom builds, etc.) the existing stack should work with minimal changes since the architecture is the same — Klipper, Moonraker, and Spoolman are all still in play. The main things to validate would be toolchange macro compatibility with however INDX implements its tool swaps, and scaling the ESPHome configs and NFC readers beyond 4 toolheads up to 8.

Note: The Prusa CORE One is a high-profile INDX target but runs Prusa's proprietary firmware rather than Klipper, which means Moonraker and Spoolman integration would work completely differently there. The primary focus for this project should be INDX on Klipper-based printers where the existing stack applies directly.
