# AFC BoxTurtle NFC Setup Guide

## Overview

This guide covers the per-lane setup: one ESP32-S3-Zero + PN532 per BoxTurtle lane. Each board is independent and communicates with the middleware via MQTT.

When you place a spool on a respooler, the NFC tag is scanned automatically as it rotates into range. The middleware looks up the spool in Spoolman and calls AFC's `SET_SPOOL_ID` to register it in the correct lane. AFC automatically pulls color, material, and weight from Spoolman — one call does everything.

> For an alternative single-ESP32 approach, see [hardware-approaches.md](hardware-approaches.md).

## Prerequisites

- BoxTurtle with AFC-Klipper Add-On installed and working
- Spoolman installed and running
- Klipper + Moonraker on Raspberry Pi
- Home Assistant with Mosquitto MQTT broker
- NFC tags on your spools (with UIDs registered in Spoolman's `nfc_id` field)

## Hardware

- 4x [Waveshare ESP32-S3-Zero](https://www.waveshare.com/esp32-s3-zero.htm) (one per lane)
- 4x PN532 NFC Module (I2C mode)
- NFC tags (one per spool)
- Power from AFC-Lite 5V rail or USB

## Step 1 — Wire the Hardware

See [wiring.md](wiring.md) for the complete wiring guide with pin
assignments and power distribution.

## Step 2 — Flash ESPHome

Repeat this for each ESP32-S3-Zero (one per lane).

1. Go to **https://web.esphome.io** in Chrome or Edge
2. Plug the ESP32-S3-Zero into your PC via USB
3. Click **"Prepare for first use"** → **Connect** → select **USB JTAG** from the popup
4. Flash the base firmware
5. Connect to the ESP32's fallback hotspot (e.g. `lane1-nfc`) and enter your WiFi credentials
6. Adopt the device in Home Assistant's ESPHome dashboard

## Step 3 — Push the Full Config

Repeat this for each device, updating `lane_id` and the static IP for each one.

1. Click **Edit** on the device in ESPHome dashboard
2. Replace the config with the contents of `integrations/afc/esphome/lane-pn532.yaml`
3. Update:
   - `lane_id` in `substitutions` to match this lane (e.g. `lane1`, `lane2`, etc.)
   - `static_ip` and `gateway` for your network
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

1. Clone the repo (if you haven't already):
   ```bash
   cd ~
   git clone https://github.com/sjordan0228/SpoolSense.git
   ```

2. Copy the config template and fill in your values:
   ```bash
   cp ~/SpoolSense/middleware/config.example.yaml ~/SpoolSense/config.yaml
   nano ~/SpoolSense/config.yaml
   ```
   Set your MQTT, Spoolman, and Moonraker details. Make sure `toolhead_mode`
   is set to `"ams"` and the lane names match your AFC config.

3. Install dependencies:
   ```bash
   pip3 install paho-mqtt requests pyyaml watchdog --break-system-packages
   ```

4. Test manually:
   ```bash
   python3 ~/SpoolSense/middleware/spoolsense.py
   ```
   You should see:
   ```
   Starting NFC Spoolman Middleware — AFC Edition (TOOLHEAD_MODE: ams)
   Config loaded from /home/youruser/SpoolSense/config.yaml
   Lanes: lane1, lane2, lane3, lane4
   Connected to MQTT broker (TOOLHEAD_MODE: ams)
   ```

5. Install as a service:
   ```bash
   sudo cp ~/SpoolSense/middleware/spoolsense.service /etc/systemd/system/
   sudo nano /etc/systemd/system/spoolsense.service  # replace YOUR_USERNAME
   sudo systemctl enable spoolsense
   sudo systemctl start spoolsense
   ```

## Step 5 — Lane LED Colors

SpoolSense no longer manages BoxTurtle lane LEDs directly. LED color is handled
natively by AFC-Klipper-Add-On using the filament color set in Spoolman via
`SET_SPOOL_ID`. No custom macros or extra config are required.

> **Requires:** AFC-Klipper-Add-On with `_get_lane_color()` support
> (see [sjordan0228/AFC-Klipper-Add-On PR #671](https://github.com/ArmoredTurtle/AFC-Klipper-Add-On/pull/671)).
> Without this, AFC will use its default configured LED colors (green for ready,
> blue for tool loaded) rather than the Spoolman filament color.

When PR #671 is merged into the AFC `multi_extruder` branch and you are running
that version, lane LEDs will automatically reflect filament colors — no
additional setup needed.

## Step 6 — Configure Spoolman

Each spool needs an NFC tag UID registered in Spoolman:

1. Open Spoolman and ensure you have an `nfc_id` extra field configured
   (Settings → Extra Fields → Spool → add `nfc_id` as a text field)
2. For each spool, edit it and add the NFC tag UID in the `nfc_id` field
3. The UID format should match what your PN532 reads (e.g. `04-67-EE-A9-8F-61-80`)

## Step 7 — Test

1. Place a tagged spool on lane 1's respooler
2. Watch the middleware logs:
   ```bash
   journalctl -u spoolsense -f
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

**Clear** — the middleware watches AFC's variable file (`AFC.var.unit`) for
changes. When a lane is ejected and the spool_id is cleared, the middleware
automatically publishes "clear" to resume scanning on that lane. On shutdown,
all lanes are cleared so scanners resume on next startup.

### AFC Variable File Watcher

The middleware uses `watchdog` to monitor `AFC.var.unit` for changes. When
the file is written (e.g. after a lane load, eject, or state change), the
middleware reads the updated lane data and:
- Locks scanners for lanes with spools, clears empty lanes
- Caches lane statuses so NFC scan handlers can check AFC state instantly

For single/toolchanger modes, the middleware similarly watches Klipper's
`save_variables` file and syncs LED colors when spool assignments change
outside the middleware (e.g. after a reboot).

### Lane LED Colors

Lane LED color is owned entirely by AFC-Klipper-Add-On. When `SET_SPOOL_ID`
is called, AFC stores the Spoolman filament color on the lane object. AFC's
`_get_lane_color()` then uses that color when updating LEDs during load,
unload, and state transitions.

SpoolSense does not call any LED macros in AFC mode. No custom `_SET_LANE_LED`
macro is needed.

> This requires AFC-Klipper-Add-On with `_get_lane_color()` support. Without
> it, AFC uses its default configured colors.

### AFC Integration

The middleware calls `SET_SPOOL_ID LANE=<lane> SPOOL_ID=<id>` via
Moonraker's gcode script API. AFC then:
- Pulls filament color from Spoolman → updates lane color in UI
- Pulls material type from Spoolman → sets lane material
- Pulls remaining weight from Spoolman → sets lane weight
- Manages active spool tracking automatically on lane changes

## Differences from Toolchanger Mode

| Feature | Toolchanger | AFC/AMS |
|---------|-------------|---------|
| Scanner location | Per toolhead | Per lane in BoxTurtle |
| ESP32 count | One per toolhead | One per lane (4 total) |
| Spool registration | SET_ACTIVE_SPOOL / SET_GCODE_VARIABLE | SET_SPOOL_ID (AFC) |
| LED feedback | ESP32 onboard WS2812 | BoxTurtle lane LEDs (via AFC natively) |
| Scan behavior | Always scanning | Scan-lock-clear lifecycle |
| File watcher | Klipper save_variables | AFC.var.unit |
| Klipper macros | spoolman_macros.cfg + toolhead macros | None required for LEDs |
