# Future Enhancements

Ideas and known improvements for future development. Contributions welcome.

---

## Middleware

**Smarter Spoolman lookups**
Spoolman's API supports filtering by extra fields — we should query directly by NFC ID instead of pulling the full spool list and searching in Python. Not a problem at small scale, but becomes inefficient as spool counts grow.

---

## Home Assistant

**Low spool push notification**
The middleware already detects when a spool is below the low threshold. It could publish to Home Assistant's notification service so users get a phone alert when filament is running low, rather than relying on noticing the LED.

---

## Quality of Life

**Klipper error alerts via LED**
When something goes wrong mid-print — filament jam, runout, pause — Klipper could publish an MQTT message to the affected toolhead's scanner to trigger a visual alert on the LED.

Potential alert states:
- Slow red blink — filament jam detected on this toolhead
- Fast red blink — more urgent error requiring immediate attention
- Yellow pulse — print paused, waiting for user input
- LED returns to spool color automatically when the issue is cleared and print resumes

Since each toolhead has its own scanner and LED, alerts would be per-toolhead — if one jams, only that one blinks red. No need to look at a screen to know which toolhead needs attention.

---

## Future Platform Support

**Bondtech INDX compatibility**
A longer term goal is to get this project working with the Bondtech INDX system once it's publicly available (retail sales expected Q2 2026). INDX supports up to 8 toolheads and is firmware-agnostic — it works with Klipper, Marlin, and RRF — making it a natural fit for this project.

For Klipper-based printers running INDX (Voron, custom builds, etc.) the existing stack should work with minimal changes since the architecture is the same — Klipper, Moonraker, and Spoolman are all still in play. The main things to validate would be toolchange macro compatibility with however INDX implements its tool swaps, and scaling the scanner configs beyond 4 toolheads up to 8.
