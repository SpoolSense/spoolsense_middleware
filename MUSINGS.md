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
