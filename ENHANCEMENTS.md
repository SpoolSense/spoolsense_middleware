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

---

## ESPHome

**Fix scan flash vs. MQTT publish order**
Right now the white flash plays out fully before the MQTT publish fires. That means there's a window where the flash is done but the LED hasn't updated to the spool color yet. The publish should fire immediately when the tag is scanned, with the flash happening in parallel while the middleware does its work.

**Remove dead lambda in color handler**
There's a leftover no-op lambda at the top of the color MQTT handler that does nothing. Just noise — should be cleaned up.

**`on_tag_removed` handling**
The PN532 supports an `on_tag_removed` event that fires when a tag leaves the reader. Right now the LED holds the last color indefinitely after you pull the spool away. Could dim the LED or turn it off when the tag is removed to make it clearer nothing is actively on the reader.

**Single shared base config**
All 4 ESPHome YAML files are nearly identical — the only real differences are the toolhead name, static IP, and topic names. Any change to shared logic (like the LED effects we just added) has to be copy-pasted across all 4 files. ESPHome supports `!include` and packages — a single `base.yaml` with all the logic, and each toolhead file just defines its name and IP, would make maintenance much cleaner.

---

## Home Assistant Integration

**Push notifications for low spool and unknown tags**
The middleware already knows when a spool is low or when an unknown tag is scanned — it's just logging it to the console. It could publish to Home Assistant's notification service at the same time so you get a phone alert rather than relying on noticing the LED.

---

## Klipper

**Automatic spool activation via toolchange macros**
Currently Fluidd is relied on to display and manage per-toolhead spool assignments. Jim's ID from the #madmax-toolchanger Discord channel pointed out that since the T0–T3 macros already store `variable_spool_id`, you could call `SET_ACTIVE_SPOOL` and `CLEAR_ACTIVE_SPOOL` directly inside the toolchange logic instead. The active spool would swap automatically every time the toolhead changes — no front end involvement needed. This would make the whole system front-end agnostic, meaning it works identically in Mainsail, Fluidd, or anything else. The only thing lost is the visual dashboard showing all 4 spools at once, which matters less if tracking is fully automatic.

*Implementation note: When building this, add a `TOOLHEAD_MODE` config variable to the middleware (`"single"` or `"toolchanger"`) so users who aren't running klipper-toolchanger aren't affected. Single mode works exactly as today — scan a tag, set the active spool, done. Toolchanger mode adds the automatic spool swap on toolchange. The Klipper macros should be similarly split so single toolhead users have a clear stopping point and don't need to add any toolchanger-specific config.*

## Installation

**Interactive install script**
Right now setup requires manually editing config files, copying files to the right places, and setting up the systemd service — a lot of steps that are easy to get wrong. An interactive install script would walk the user through everything after a `git clone`.

When run, it would prompt for all the values that currently require manual editing:

- MQTT broker IP
- MQTT username and password
- Spoolman IP
- Moonraker/Klipper IP
- Single toolhead or toolchanger mode (ties into the `TOOLHEAD_MODE` work above)
- Number of toolheads (if toolchanger mode)
- Low spool threshold

Once the user answers the prompts, the script would write out the configured `nfc_listener.py`, install dependencies, copy the systemd service file, and start the service — all in one shot. At the end it could do a quick connectivity check against the MQTT broker and Spoolman to confirm everything is reachable before exiting.

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
