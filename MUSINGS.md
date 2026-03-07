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

**`ams`** — multiple lanes feeding a single toolhead through a filament changer. Spool IDs stored per lane. `SET_ACTIVE_SPOOL` called on every lane change. Scanner mounted at each lane inside the BoxTurtle/NightOwl unit.

### AFC-Klipper Add-On — what we can hook into

AFC already has deep Spoolman integration and several features we can leverage directly:

**`SET_SPOOL_ID LANE=<lane> SPOOL_ID=<id>`** — AFC's existing command to assign a Spoolman spool ID to a lane. Our middleware could call this directly after an NFC scan instead of (or in addition to) calling `SET_ACTIVE_SPOOL`. This would let AFC manage the spool-to-lane mapping natively, and AFC would handle setting the active spool in Spoolman whenever it loads that lane.

**`SET_NEXT_SPOOL_ID SPOOL_ID=<id>`** — AFC's command designed specifically for scanner integrations. The docs say "this can be used in a scanning macro to prepare the spool to be loaded next into the AFC." This is essentially the hook point built for us. After an NFC scan, the middleware calls `SET_NEXT_SPOOL_ID` with the Spoolman spool ID, and AFC takes it from there.

**`SET_COLOR LANE=<lane> COLOR=<hex>`** — AFC can set the color per lane. Our middleware already knows the filament color from Spoolman's `color_hex` field — after a scan, we could push the color to both the ESP32 LED and to AFC's lane color so the Mainsail/Fluidd UI shows the correct color per lane.

**`SET_MATERIAL LANE=<lane> MATERIAL=<type>`** — AFC can set material type per lane. Spoolman knows the filament material. After a scan, the middleware could push material info to AFC automatically.

**`SET_WEIGHT LANE=<lane> WEIGHT=<grams>`** — AFC tracks remaining weight per lane. Spoolman has `remaining_weight`. After a scan, the middleware could sync weight data so AFC's print assist and spool management features have accurate data.

**Spoolman active spool tracking** — AFC already calls Moonraker's Spoolman API to set the active spool on lane changes (recent PR #568/#576 added `spool_id` to lane data). If AFC is handling the active spool, our middleware in `ams` mode might not need to call `SET_ACTIVE_SPOOL` at all — just do the NFC lookup and push the spool ID into AFC, and let AFC manage everything downstream.

**`afc-spool-scan`** — AFC already has a USB QR code scanner implementation. Our NFC approach would be a parallel scanning method — the middleware integration points would be the same (`SET_NEXT_SPOOL_ID` or `SET_SPOOL_ID`).

### Scanning approach

Two options for when scanning happens:

**Option A — Scan on spool load (preferred).** Mount the PN532 where it can read the NFC tag as the spool is placed on the respooler. The ESP32 uses continuous `on_tag` scanning (same as current toolchanger mode). When you drop a spool in, the tag is scanned immediately, the middleware looks it up, and pushes the spool ID into AFC via `SET_SPOOL_ID` or `SET_NEXT_SPOOL_ID`. No macro integration needed on the AFC side — the scan happens at the physical moment of loading.

**Option B — Triggered scan on AFC load.** AFC's lane load macro fires an MQTT message telling the ESP32 to scan. The ESP32 reads the tag on demand. This requires AFC macro modifications and is more complex, but would work if the NFC reader can't be positioned for continuous scanning.

Option A is simpler and keeps the AFC add-on unmodified. The NFC system operates independently — AFC doesn't need to know about NFC at all, it just receives spool IDs through its existing commands.

### Middleware changes for AMS mode

The middleware in `ams` mode would:

1. Receive NFC scan from ESP32 via MQTT (same as today)
2. Look up spool in Spoolman (same as today)
3. Instead of calling `SET_ACTIVE_SPOOL` or `SET_GCODE_VARIABLE`, call AFC's `SET_SPOOL_ID` for the lane via Moonraker's gcode script API
4. Optionally push color, material, and weight to AFC via `SET_COLOR`, `SET_MATERIAL`, `SET_WEIGHT`
5. Publish LED color to the ESP32 (same as today)

The config would look like:

```yaml
toolhead_mode: "ams"
toolheads:      # these become lane names in AMS mode
  - "lane1"
  - "lane2"
  - "lane3"
  - "lane4"
```

MQTT topics would follow the same pattern: `nfc/toolhead/lane1`, `nfc/toolhead/lane1/color`, etc.

### Hardware considerations

- **ESP32 board** — the Waveshare ESP32-S3-Zero works but a different form factor might mount better inside a BoxTurtle enclosure. Main requirements are just I2C for the PN532 and WiFi/MQTT.
- **PN532 mounting** — custom mount to position the reader where it can read a tag on the spool sitting on the respooler. The PN532 reads at about 5cm, so reader and tag placement need to be within range.
- **One reader per lane** — 4 lanes = 4 ESP32 + PN532 units, same cost as a 4-toolhead setup.
- **Possible single ESP32 with multiplexed I2C** — since scans are per-lane and not simultaneous, one ESP32 with an I2C multiplexer driving 4 PN532 modules could work. Reduces cost and wiring but adds firmware complexity. Nice optimization for later.

### Why this fits in one project

The NFC scanning, MQTT transport, Spoolman lookup, LED feedback, and ESPHome firmware are all identical between modes. The only differences are:
- Where the scanner is physically mounted
- Which Klipper/Moonraker API calls the middleware makes after a successful scan
- How the config names positions (toolheads vs lanes)

All three modes share the same middleware, same ESPHome base.yaml, same Spoolman integration, and same hardware. Keeping it in one project avoids duplicating everything for what amounts to a few lines of mode-switching logic.

---
