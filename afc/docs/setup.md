# AFC BoxTurtle NFC Setup Guide

## Overview

This guide covers setting up NFC spool scanning for BoxTurtle AFC systems.
One ESP32 drives 4 PN532 NFC readers (one per lane) and communicates with
the AFC-Klipper Add-On via the middleware.

When you place a spool on a respooler, the NFC tag is scanned automatically
as it rotates into range. The middleware looks up the spool in Spoolman and
calls AFC's `SET_SPOOL_ID` to register it in the correct lane. AFC
automatically pulls color, material, and weight from Spoolman — one call
does everything.

## Prerequisites

- BoxTurtle with AFC-Klipper Add-On installed and working
- Spoolman installed and running
- Klipper + Moonraker on Raspberry Pi
- Home Assistant with Mosquitto MQTT broker
- NFC tags on your spools (with UIDs registered in Spoolman's `nfc_id` field)

## Hardware

- 1x ESP32-WROOM DevKit (e.g. Freenove ESP32, or any esp32dev board)
- 4x PN532 NFC Module (I2C mode)
- NFC tags (one per spool)
- Hookup wire for I2C and power connections
- Power from AFC-Lite 5V rail

## Step 1 — Wire the Hardware

See [wiring.md](wiring.md) for the complete wiring guide with pin
assignments and power distribution.

## Step 2 — Flash ESPHome

1. Go to **https://web.esphome.io** in Chrome or Edge
2. Plug the ESP32 into your PC via USB
3. Click **"Prepare for first use"** → **Connect** → select the serial port
4. Flash the base firmware
5. Connect to the ESP32's fallback hotspot and enter your WiFi credentials
6. Adopt the device in Home Assistant's ESPHome dashboard

## Step 3 — Push the Full Config

1. Click **Edit** on the device in ESPHome dashboard
2. Replace the config with the contents of `afc/esphome/boxturtle-nfc.yaml`
3. Update:
   - `static_ip` and `gateway` for your network
   - Lane names in `substitutions` if yours differ from lane1–lane4
4. Add to your ESPHome **Secrets** file:
   ```yaml
   wifi_ssid: "YourNetworkName"
   wifi_password: "YourWiFiPassword"
   mqtt_broker: "192.168.1.100"
   mqtt_username: "your_ha_username"
   mqtt_password: "your_ha_password"
   ```
5. Click **Save** then **Install → Wirelessly**

## Step 4 — Deploy the Middleware

1. Create the directory (if it doesn't exist):
   ```bash
   mkdir -p ~/nfc_spoolman
   ```

2. Copy the AFC middleware and config:
   ```bash
   cp afc/middleware/nfc_listener.py ~/nfc_spoolman/
   cp afc/middleware/config.example.yaml ~/nfc_spoolman/config.yaml
   ```

3. Edit the config:
   ```bash
   nano ~/nfc_spoolman/config.yaml
   ```
   Set your MQTT, Spoolman, and Moonraker details. Make sure `toolhead_mode`
   is set to `"ams"` and the lane names match your AFC config.

4. Install dependencies:
   ```bash
   pip3 install paho-mqtt requests pyyaml --break-system-packages
   ```

5. Test manually:
   ```bash
   python3 ~/nfc_spoolman/nfc_listener.py
   ```
   You should see:
   ```
   Starting NFC Spoolman Middleware — AFC Edition (TOOLHEAD_MODE: ams)
   Config loaded from /home/youruser/nfc_spoolman/config.yaml
   Lanes: lane1, lane2, lane3, lane4
   Connected to MQTT broker (TOOLHEAD_MODE: ams)
   ```

6. Install as a service:
   ```bash
   sudo cp afc/middleware/nfc-spoolman.service /etc/systemd/system/
   sudo nano /etc/systemd/system/nfc-spoolman.service  # replace YOUR_USERNAME
   sudo systemctl enable nfc-spoolman
   sudo systemctl start nfc-spoolman
   ```

## Step 5 — Configure Spoolman

Each spool needs an NFC tag UID registered in Spoolman:

1. Open Spoolman and ensure you have an `nfc_id` extra field configured
   (Settings → Extra Fields → Spool → add `nfc_id` as a text field)
2. For each spool, edit it and add the NFC tag UID in the `nfc_id` field
3. The UID format should match what your PN532 reads (e.g. `04-67-EE-A9-8F-61-80`)

## Step 6 — Test

1. Place a tagged spool on lane 1's respooler
2. Watch the middleware logs:
   ```bash
   journalctl -u nfc-spoolman -f
   ```
3. You should see:
   ```
   NFC scan on lane1: UID=04-67-EE-A9-8F-61-80
   Found spool: Your Filament Name (ID: 5)
   [ams] Set spool 5 on lane1 via AFC SET_SPOOL_ID
   Published lock to nfc/toolhead/lane1/lock
   ```
4. AFC should now show the spool info for that lane in Mainsail/Fluidd
5. Subsequent rotations of the spool will be ignored (lane is locked)

## How It Works

### Scan-Lock-Clear Lifecycle

**Scanning** — when no spool is registered on a lane, the PN532 reader
is actively polling. Any NFC tag that enters the read zone triggers a scan.

**Locked** — after a successful scan and spool registration, the middleware
publishes a "lock" command. The ESP32 stops polling the PN532 on that lane.
The spool can rotate freely during printing without triggering more scans.

**Clear** — when the middleware shuts down, it publishes "clear" to all
lanes to resume scanning on next startup. Future versions will detect
AFC lane ejection events to clear individual lanes automatically.

### AFC Integration

The middleware calls `SET_SPOOL_ID LANE=<lane> SPOOL_ID=<id>` via
Moonraker's gcode script API. AFC then:
- Pulls filament color from Spoolman → updates lane color in UI
- Pulls material type from Spoolman → sets lane material
- Pulls remaining weight from Spoolman → sets lane weight
- Manages active spool tracking automatically on lane changes

No additional Klipper macros are needed — AFC handles everything.

## Differences from Toolchanger Mode

| Feature | Toolchanger | AFC/AMS |
|---------|-------------|---------|
| Scanner location | Per toolhead | Per lane in BoxTurtle |
| ESP32 count | One per toolhead | One for all 4 lanes |
| Spool registration | SET_ACTIVE_SPOOL / SET_GCODE_VARIABLE | SET_SPOOL_ID (AFC) |
| LED feedback | ESP32 onboard WS2812 | BoxTurtle lane LEDs (via AFC) |
| Scan behavior | Always scanning | Scan-lock-clear lifecycle |
| Klipper macros | spoolman_macros.cfg + toolhead macros | None — AFC handles everything |
