# SpoolSense Documentation

## Setup Guides

| Guide | What it covers |
|-------|----------------|
| [middleware-setup.md](middleware-setup.md) | Installing and configuring the SpoolSense middleware service |
| [klipper-setup.md](klipper-setup.md) | Klipper macros, spool ID persistence across reboots |
| [spoolman-setup.md](spoolman-setup.md) | Spoolman extra fields, NFC tag registration, low spool warnings |

## AFC / BoxTurtle

| Guide | What it covers |
|-------|----------------|
| [afc-readme.md](afc-readme.md) | Overview of AFC NFC integration |
| [afc-setup.md](afc-setup.md) | AFC setup — scan-lock-clear lifecycle, lane LEDs, middleware config |

> **AFC LED filament color** requires `led_use_filament_color` support in AFC-Klipper-Add-On. See [PR #681](https://github.com/ArmoredTurtle/AFC-Klipper-Add-On/pull/681). Without it, AFC uses its default configured LED colors.

## Scanner

Scanner hardware setup, wiring, and firmware docs are in the [spoolsense_scanner](https://github.com/SpoolSense/spoolsense_scanner) repo.
