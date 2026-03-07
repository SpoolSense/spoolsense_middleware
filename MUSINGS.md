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

## BoxTurtle AMS Integration

The [BoxTurtle](https://github.com/ArmoredTurtle/BoxTurtle) by Armored Turtle is an open-source AMS-style automated filament changer for Klipper. It's a lane-based system — each of the 4 lanes has its own extruder motor that feeds filament to the toolhead independently, with electric respoolers to manage the spools. It uses the [AFC-Klipper Add-On](https://github.com/ArmoredTurtle/AFC-Klipper-Add-On) for control, and multiple BoxTurtle units can be chained (up to 16 units / 64 lanes).

This is a natural fit for NFC spool tracking. Instead of mounting a scanner at each toolhead (like the current toolchanger setup), you'd mount a PN532 reader at each lane inside the BoxTurtle — one per spool slot. When a spool is loaded into a lane, the NFC tag gets scanned and that lane is now associated with a specific spool in Spoolman.

### How it would work

The key difference from the current toolchanger approach is **when** scanning happens. On a toolchanger, the scanner runs continuously and the tag is always near the reader. On a BoxTurtle, the spool sits on a respooler above the lane — the NFC tag would only be in range when the spool is physically loaded or seated.

Rather than the ESP32 constantly polling for tags, the scan could be triggered by the AFC load macro. When BoxTurtle's AFC add-on runs its filament load sequence for a lane, it could fire an MQTT message to the corresponding ESP32 telling it to scan now. The ESP32 reads the tag, publishes the UID, and the middleware associates that spool with that lane — just like the existing flow, but triggered on-demand instead of always-on.

This also means the scan happens at the right moment in the workflow — when filament is actually being loaded — rather than relying on the tag being permanently in range of the reader.

### Hardware considerations

- **ESP32 board** — the Waveshare ESP32-S3-Zero works but is physically small and has limited mounting options. A different ESP32 form factor might make more sense inside a BoxTurtle enclosure — something like a standard ESP32-S3 DevKit or even an ESP32-C3 if we don't need the extra GPIO. The main requirements are I2C for the PN532 and WiFi/MQTT.
- **PN532 mounting** — needs a custom mount to position the NFC reader where it can read a tag on the spool as it sits on the respooler. The read distance on the PN532 is about 5cm, so the tag placement on the spool and the reader position in the lane need to be close enough for a reliable read.
- **One reader per lane** — 4 lanes means 4 ESP32 + PN532 units, same as a 4-toolhead setup. Power draw would be similar (~0.3-0.5W each).
- **Could potentially share one ESP32** — since scans are triggered on-demand rather than continuous, a single ESP32 with multiplexed I2C to 4 PN532 modules might work. This would reduce cost and complexity but adds firmware complexity.

### Software changes needed

- **MQTT trigger topic** — new topic like `nfc/lane/L0/scan` that the AFC macro publishes to, telling the ESP32 to perform a scan. The ESP32 subscribes to this and only reads the NFC tag when triggered.
- **ESPHome firmware** — new variant that listens for scan triggers instead of using `on_tag` continuous polling. The LED feedback and MQTT publish flow stays the same.
- **Middleware** — would need to understand lane-based spool assignments in addition to (or instead of) toolhead-based assignments. The AFC add-on already knows which lane maps to which position in the print, so the middleware just needs to track lane → spool ID.
- **AFC macro integration** — hook into the AFC filament load sequence to trigger the NFC scan and pass the spool info back to AFC/Spoolman.

### Why this matters

BoxTurtle users currently have to manually tell Spoolman which spool is in which lane. With NFC integration, you load a spool, it auto-identifies, and the whole chain — AFC, Spoolman, Moonraker — knows what's loaded without any manual input. That's the AMS experience but with open-source spool tracking.

---
