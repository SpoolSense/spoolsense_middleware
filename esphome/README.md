# ESPHome Configs

This directory contains ESPHome firmware configs for SpoolSense NFC scanners.

---

## Which file do I use?

### Single toolhead or multi-toolhead (toolchanger)

Use `toolhead-t0.yaml` through `toolhead-t3.yaml` — one per physical toolhead.

Each file is a thin wrapper that sets the toolhead's ID and WiFi/IP config, then pulls all shared logic from `base-pn532.yaml`. You only need to edit the wrapper file before flashing.

| File | Toolhead ID |
|------|-------------|
| `toolhead-t0.yaml` | T0 |
| `toolhead-t1.yaml` | T1 |
| `toolhead-t2.yaml` | T2 |
| `toolhead-t3.yaml` | T3 |

For a **single toolhead**, just flash `toolhead-t0.yaml` (or rename `T0` to whatever your setup uses).

For a **custom toolhead name** (e.g. `tool_carriage_0`), duplicate one of the wrapper files and change `toolhead_id` in the substitutions block.

---

### AFC / BoxTurtle (per-lane scanner)

Use `integrations/afc/esphome/lane-pn532.yaml`.

This is a standalone file — flash one copy per lane, changing `lane_id` and the WiFi/IP settings each time. See the [AFC setup guide](../integrations/afc/docs/setup.md) for full instructions.

| Lane | `lane_id` value |
|------|-----------------|
| Lane 1 | `lane1` |
| Lane 2 | `lane2` |
| Lane 3 | `lane3` |
| Lane 4 | `lane4` |

Lane names must match your AFC config (`[AFC_stepper lane1]` etc.).

---

## What to edit before flashing

### Toolchanger wrapper files (`toolhead-t0.yaml` etc.)

```yaml
substitutions:
  toolhead_id: "T0"          # change if needed
  friendly_name: "Toolhead T0 NFC"

wifi:
  manual_ip:
    static_ip: 192.168.X.X   # ← set this
    gateway: 192.168.X.1     # ← set this
  ap:
    password: "CHANGE_ME"    # ← set this
```

### AFC lane file (`lane-pn532.yaml`)

```yaml
substitutions:
  lane_id: "lane1"                      # ← change per lane
  friendly_name: "BoxTurtle Lane 1 NFC" # ← change per lane

wifi:
  manual_ip:
    static_ip: 192.168.X.X   # ← set this (different IP per lane)
    gateway: 192.168.X.1     # ← set this
  ap:
    password: "CHANGE_ME"    # ← set this
```

---

## Secrets file

All configs read WiFi and MQTT credentials from your ESPHome secrets file. In Home Assistant, go to **ESPHome → Secrets** and add:

```yaml
wifi_ssid: "YourNetworkName"
wifi_password: "YourWiFiPassword"
mqtt_broker: "192.168.1.100"    # Your Home Assistant / MQTT broker IP
mqtt_username: "your_username"
mqtt_password: "your_password"
```

---

## How to flash (first time)

1. Go to **https://web.esphome.io** in Chrome or Edge
2. Plug the ESP32-S3-Zero into your PC via USB
3. Click **"Prepare for first use"** → **Connect** → select the serial port
4. After the base flash completes, the ESP32 will broadcast a fallback WiFi hotspot
5. Connect to the hotspot and enter your WiFi credentials
6. Adopt the device in your Home Assistant ESPHome dashboard
7. Click **Edit** on the device, paste in your config file contents, click **Save → Install → Wirelessly**

After the first flash, all future updates are wireless (OTA) directly from the ESPHome dashboard.

---

## Hardware reference

| Component | Toolchanger / Single | AFC per-lane |
|-----------|---------------------|--------------|
| Board | ESP32-S3-Zero | ESP32-S3-Zero |
| Scanner | PN532 (I2C) | PN532 (I2C) |
| I2C SDA | GPIO1 | GPIO1 |
| I2C SCL | GPIO2 | GPIO2 |
| Status LED | GPIO21 (WS2812) | not used |
| Lock mechanism | no | yes |

PN532 DIP switch settings for I2C mode: **Switch 1 ON, Switch 2 OFF**.

---

## Directory structure

```
esphome/
├── base-pn532.yaml              # Shared logic for toolchanger/single (do not flash directly)
├── toolhead-t0.yaml       # Flash this for T0
├── toolhead-t1.yaml       # Flash this for T1
├── toolhead-t2.yaml       # Flash this for T2
├── toolhead-t3.yaml       # Flash this for T3
└── README.md              # This file

integrations/afc/esphome/
└── lane-pn532.yaml        # Flash this per AFC lane (edit substitutions each time)
```
