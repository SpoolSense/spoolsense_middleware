# Musings

Random ideas, half-baked thoughts, and things worth exploring someday.
No commitment, no timeline — just a place to capture inspiration.

---

## Integrated Filament Feeder per Scanner

Manually feeding filament to each toolhead is a pain. The Snapmaker U1 has a
built-in filament feeder that handles loading automatically — wondering if
something like that could be integrated directly into the scanner case design.

The scanner is already mounted at each toolhead and has an ESP32 with GPIO pins
to spare. A small motorized feeder mechanism built into the case could
potentially handle filament loading on command — triggered by an NFC scan or a
Klipper macro. The LED could even give feedback during the feed sequence.

Things to explore:
- Small stepper or DC gear motor that fits the case footprint
- Filament path routing through or alongside the case
- GPIO control from the ESP32 (already has spare pins)
- Klipper macro integration — `LOAD_FILAMENT T0` triggers the feeder via MQTT
- Whether the Waveshare ESP32-S3-Zero has enough current capacity or needs a
  separate motor driver board

---

## AMS Mode — BoxTurtle / AFC Integration

### The idea

Add a third `toolhead_mode` called `ams` to support lane-based filament changers like [BoxTurtle](https://github.com/ArmoredTurtle/BoxTurtle), [NightOwl](https://github.com/ArmoredTurtle/NightOwl), and any future system built on the [AFC-Klipper Add-On](https://github.com/ArmoredTurtle/AFC-Klipper-Add-On). This isn't a separate project — it fits into the existing architecture as a new mode alongside `single` and `toolchanger`.

The core NFC flow doesn't change: tag scanned → UID published via MQTT → middleware looks up spool in Spoolman → updates Klipper/Moonraker → LED confirms. What changes is where the scanner lives (per lane instead of per toolhead) and how spool activation works (driven by AFC lane changes instead of toolchange macros).

### How the three modes compare

**`single`** — one toolhead, `SET_ACTIVE_SPOOL` called on every NFC scan. Scanner mounted at the toolhead.

**`toolchanger`** — multiple toolheads (MadMax, StealthChanger, etc.), spool IDs stored per toolhead, `SET_ACTIVE_SPOOL` called by klipper-toolchanger at each toolchange. Scanner mounted at each toolhead.

**`ams`** — multiple lanes feeding a single toolhead through a filament changer. Spool IDs stored per lane. AFC handles `SET_ACTIVE_SPOOL` automatically on lane changes. Scanner mounted at each lane inside the BoxTurtle/NightOwl unit.

### AFC-Klipper Add-On — the integration is simpler than expected

After researching AFC's codebase and documentation, the integration turns out to be much cleaner than originally anticipated. AFC was designed with scanner integrations in mind and does most of the heavy lifting for us.

**The key discovery: `SET_SPOOL_ID` does everything.** When you call `SET_SPOOL_ID LANE=lane1 SPOOL_ID=5`, AFC doesn't just store the ID — it automatically queries Spoolman and pulls the material type, color, and weight for that spool. One call from our middleware and AFC has complete spool metadata for the lane. We don't need to separately call `SET_COLOR`, `SET_MATERIAL`, or `SET_WEIGHT`.

**`SET_NEXT_SPOOL_ID SPOOL_ID=<id>`** — AFC's command explicitly designed for scanner integrations. Their docs describe it as intended for "a scanning macro to prepare the AFC for the next spool to be loaded." This is the hook point they built for projects like ours. After an NFC scan, the middleware calls this and AFC takes it from there.

**AFC already handles Spoolman active spool tracking.** Recent PRs (#568/#576) added `spool_id` to lane data and active spool updates on lane changes. Our middleware in `ams` mode doesn't need to call `SET_ACTIVE_SPOOL` at all — AFC manages that automatically when it loads a lane's filament into the toolhead.

**`afc-spool-scan` — now reviewed.** The [afc-spool-scan](https://github.com/kekiefer/afc-spool-scan) project by kekiefer is a simple bash script that reads a USB QR code scanner (HID keyboard device) via `evtest`, extracts the Spoolman spool ID from the scanned code, and calls `SET_NEXT_SPOOL_ID SPOOL_ID=<id>` via Moonraker's gcode script API. That's the entire integration — one curl call to Moonraker. AFC handles everything else (lane assignment, Spoolman metadata pull, active spool tracking).

Key takeaway: `afc-spool-scan` uses `SET_NEXT_SPOOL_ID` (not `SET_SPOOL_ID LANE=...`). It doesn't specify a lane — AFC queues the spool ID and assigns it to whichever lane gets loaded next. The workflow is: scan QR code → load filament into a lane → AFC associates the queued spool ID with that lane automatically.

This reveals two integration patterns for our NFC approach:

**Pattern 1 — lane-agnostic (what afc-spool-scan does):** Call `SET_NEXT_SPOOL_ID` after an NFC scan. No lane mapping needed. AFC assigns the spool to whichever lane gets filament loaded next. This would work with a single handheld NFC scanner rather than per-lane mounted readers — scan the spool, then load it into any lane.

**Pattern 2 — lane-aware (our per-lane reader approach):** Since we have a physical reader mounted at each lane, we always know which lane the spool is in. Call `SET_SPOOL_ID LANE=<lane> SPOOL_ID=<id>` directly for immediate, explicit lane assignment. No queuing, no ambiguity.

Pattern 2 is better for our use case — it's more explicit, works automatically when the spool is placed on the respooler, and doesn't require the user to coordinate scan order with load order. But Pattern 1 could be supported as a simpler fallback for users who only want one NFC reader instead of four.

**AFC state is exposed via Moonraker.** AFC's Mainsail integration (PR #2089 to mainsail-crew) confirms that lane states are queryable through Moonraker's object system. This is important for the scan-lock-clear lifecycle (see below).

### The simplified AMS flow

Given what AFC handles natively, the middleware's job in `ams` mode is minimal:

1. Receive NFC scan from ESP32 via MQTT (same as today)
2. Look up UID in Spoolman → get spool ID (same as today)
3. Call `SET_SPOOL_ID LANE=<lane> SPOOL_ID=<id>` via Moonraker's gcode script API
4. AFC automatically pulls color, material, weight from Spoolman and updates the lane
5. Publish LED color to ESP32 and send lock command (see below)
6. Done — one gcode call and AFC knows everything

### Physical scanning approach — inline with spool rotation

The PN532 reader would be mounted inline with the spool on the respooler — positioned so the NFC tag on the spool passes through the read zone as the spool rotates. When a spool is first loaded, it rotates into range and the tag gets scanned. The ESP32 uses continuous `on_tag` scanning (same as current toolchanger mode) — no AFC macro modifications needed.

This means during printing, the spool is rotating and the tag will pass through the PN532's read zone on every revolution. ESPHome's PN532 component has built-in deduplication — `on_tag` only fires once when a tag enters the field, not on every poll. However, depending on rotation speed and the PN532's poll interval (~1 second default), the tag entering and exiting the read zone on each revolution could trigger repeated scan events.

### Scan-lock-clear lifecycle

Rather than dealing with debounce timers or duplicate scan logic, the ESP32 operates in two explicit states:

**Scanning state** — PN532 is actively polling. LED shows a "waiting for spool" indicator (dim white pulse or off). When a tag is read, the UID is published to MQTT and the middleware processes it.

**Locked state** — after a successful scan, the middleware publishes a lock command to the ESP32. The ESP32 stops polling the PN532 and holds the filament color on the LED. No more scan events fire regardless of spool rotation. The scanner is dormant until explicitly unlocked.

**Clear/unlock** — when a spool is ejected, the ESP32 receives a clear command, resumes PN532 polling, and returns to scanning state. LED goes back to the "waiting" indicator.

The MQTT topic for this could reuse the existing color topic: a hex color value means "lock and show this color," a value of `"clear"` means "unlock and resume scanning." Simple, one topic, no extra state management.

### How the middleware detects lane ejection (for the clear command)

This is the one piece that requires further research. The middleware needs to know when a spool is ejected from a lane so it can send the clear command to that lane's ESP32. Options explored:

**Moonraker websocket subscription (most promising)** — AFC's lane states are exposed via Moonraker's object system (confirmed by the Mainsail AFC integration PR). The middleware could open a persistent websocket connection to Moonraker and subscribe to AFC lane object updates. When a lane state transitions to empty/ejected, the middleware publishes the clear command to that lane's ESP32.

This is the biggest middleware change — going from a simple MQTT listener to a dual-protocol system (MQTT for ESP32 communication, websocket for Moonraker/AFC state). But it's the right architecture and opens the door for future features (print start/end events, error state monitoring, etc.).

**What still needs research:**
- The exact Moonraker object path and state values for AFC lanes (e.g. `AFC_stepper lane1` → what fields indicate loaded/empty?)
- Whether AFC publishes state change events via Moonraker's notification system or if we need to poll/subscribe to objects
- How the `afc-spool-scan` QR scanner project handles this same problem (if the repo becomes available)
- Whether the ArmoredTurtle team would be open to adding an MQTT publish on eject events as a first-class feature in AFC, which would eliminate the websocket requirement entirely

### Hardware considerations

- **ESP32 board** — the Waveshare ESP32-S3-Zero works but a different form factor might mount better inside a BoxTurtle enclosure. Main requirements are just I2C for the PN532 and WiFi/MQTT.
- **PN532 mounting** — custom mount to position the reader inline with the spool rotation path on the respooler. The PN532 reads at about 5cm, so the tag needs to pass within range during rotation. Mount design would need to account for different spool sizes.
- **One reader per lane** — 4 lanes = 4 ESP32 + PN532 units, same cost as a 4-toolhead setup.
- **Possible single ESP32 with multiplexed I2C** — since scans are per-lane and the lock state means only one lane scans at a time (during spool loading), a single ESP32 with an I2C multiplexer driving 4 PN532 modules could work. Reduces cost and wiring but adds firmware complexity. Nice optimization for later.

### Config

```yaml
toolhead_mode: "ams"
toolheads:      # these become lane names in AMS mode
  - "lane1"
  - "lane2"
  - "lane3"
  - "lane4"
```

MQTT topics follow the same pattern: `nfc/toolhead/lane1`, `nfc/toolhead/lane1/color`, etc.

### Why this fits in one project

The NFC scanning, MQTT transport, Spoolman lookup, LED feedback, and ESPHome firmware are all identical between modes. The only differences are:
- Where the scanner is physically mounted
- Which Klipper/Moonraker API calls the middleware makes after a successful scan (one `SET_SPOOL_ID` call vs `SET_ACTIVE_SPOOL` / `SET_GCODE_VARIABLE`)
- The scan-lock-clear lifecycle (new for AMS mode, not needed for toolchanger where the tag is always in range)
- How the config names positions (toolheads vs lanes)

All three modes share the same middleware, same ESPHome base.yaml, same Spoolman integration, and same hardware. Keeping it in one project avoids duplicating everything for what amounts to a mode-switching branch in the middleware.

### Next steps

1. Research AFC's Moonraker object namespace to confirm lane state is subscribable
2. Look at `afc-spool-scan` source code when available to see how they handle the scanner → AFC pipeline
3. Consider reaching out to the ArmoredTurtle team on Discord about potential collaboration or MQTT event publishing
4. Prototype a PN532 mount for the BoxTurtle respooler to validate read geometry with rotating spools
5. Implement the Moonraker websocket client in the middleware as a foundation for AMS mode

---
