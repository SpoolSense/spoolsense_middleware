# SpoolSense Documentation

> ⚠️ **AFC/BoxTurtle users:** The AFC integration is partially functional — spool scanning and `SET_SPOOL_ID` work today, but LED lane color override depends on [AFC-Klipper-Add-On PR #671](https://github.com/ArmoredTurtle/AFC-Klipper-Add-On/pull/671) being merged before it is fully operational. See [integrations/afc/docs/setup.md](../integrations/afc/docs/setup.md) for the full AFC setup guide.

---

## Toolchanger / Single Toolhead

These guides cover the standard SpoolSense setup — one ESP32-S3-Zero + PN532 per toolhead, or one for a single toolhead printer.

| Guide | What it covers |
|-------|----------------|
| [wiring.md](wiring.md) | PN532 wiring to ESP32-S3-Zero, DIP switch settings |
| [esphome-setup.md](esphome-setup.md) | Flashing ESPHome firmware, adopting into Home Assistant |
| [middleware-setup.md](middleware-setup.md) | Installing and configuring the `spoolsense` middleware service |
| [klipper-setup.md](klipper-setup.md) | Klipper macros, spool ID persistence across reboots |
| [spoolman-setup.md](spoolman-setup.md) | Spoolman extra fields, NFC tag registration, low spool warnings |

---

## AFC / BoxTurtle

AFC-specific docs live in [integrations/afc/docs/](../integrations/afc/docs/).

| Guide | What it covers |
|-------|----------------|
| [setup.md](../integrations/afc/docs/setup.md) | Full AFC setup guide (ESPHome, middleware, Klipper, Spoolman) |
| [wiring.md](../integrations/afc/docs/wiring.md) | PN532 wiring for BoxTurtle (one ESP32-S3-Zero per lane) |

> ⚠️ **AFC LED color override is not yet functional.** It requires [AFC-Klipper-Add-On PR #671](https://github.com/ArmoredTurtle/AFC-Klipper-Add-On/pull/671) to be merged. Spool scanning and `SET_SPOOL_ID` registration work without it — lanes will identify spools correctly, but LED colors will follow AFC's defaults until the PR lands.
