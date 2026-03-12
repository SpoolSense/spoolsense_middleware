# ESPHome Setup Guide

> **This guide covers toolchanger and single toolhead setups.** AFC/BoxTurtle users: see [integrations/afc/docs/setup.md](../integrations/afc/docs/setup.md) for the AFC setup guide, and `integrations/afc/esphome/lane-pn532.yaml` for the ESPHome config. Note that the AFC integration is not yet fully functional — it depends on [AFC-Klipper-Add-On PR #671](https://github.com/ArmoredTurtle/AFC-Klipper-Add-On/pull/671) being merged.

## Prerequisites

- Home Assistant with ESPHome addon installed
- Mosquitto MQTT broker addon installed and configured in Home Assistant
- Chrome or Edge browser (required for USB flashing)

### Setting up Mosquitto MQTT Broker

If you haven't set up Mosquitto yet, this guide walks you through the full process — installing the addon, creating a user, and connecting it to Home Assistant:

👉 **[Setup MQTT & Mosquitto Broker on Home Assistant — HAProfs](https://haprofs.com/setting-up-mqtt-mosquitto-broker-home-assistant/)**

For a deeper dive into MQTT configuration options, see the official Home Assistant documentation:

👉 **[MQTT Integration — Home Assistant](https://www.home-assistant.io/integrations/mqtt/)**

Once Mosquitto is running and you have a username/password configured, come back here to continue.

## Config Structure

The ESPHome configs use a shared base + thin wrapper approach:

- **`base-pn532.yaml`** — all shared logic (LED, NFC, MQTT handlers, I2C). Never edit directly.
- **`toolhead-t0.yaml` through `toolhead-t3.yaml`** — one per toolhead. Only contains the device name, static IP, and substitution variables that identify the toolhead.

Any changes to shared logic (LED effects, MQTT topics, scan behavior) only need to be made in `base-pn532.yaml` — all toolheads inherit the changes automatically.

## First Flash (USB Required)

The first flash must be done via USB. After that, all updates are wireless (OTA).

1. Go to **https://web.esphome.io** in Chrome or Edge
2. Click **"Prepare for first use"**
3. Plug ESP32-S3 into your PC via USB
4. Click **Connect** and select **USB JTAG** from the popup
5. Flash the basic firmware
6. Once flashed, connect your PC to the ESP32's fallback hotspot (e.g. `toolhead-t0`)
7. Enter your WiFi credentials in the captive portal at **192.168.4.1**
8. The ESP32 will reboot and connect to your WiFi

## Adopt into Home Assistant ESPHome

1. Go to Home Assistant → ESPHome dashboard
2. The device should appear as **Discovered** — click **Adopt**
3. Or click **Take Control** if it appears in the device list

## Push Full Config

1. In the ESPHome dashboard, click **+ New Device** (or **Edit** if you adopted the device)
2. Replace the config with the contents of the appropriate toolhead YAML (e.g. `toolhead-t0.yaml`)
3. Update the static IP and gateway in the config for your network
4. Upload `base-pn532.yaml` to your ESPHome config directory — the toolhead files pull it in via `packages: !include base-pn532.yaml`. In the ESPHome dashboard, click the three-dot menu → **Upload file**, then select `esphome/base-pn532.yaml` from the repo
5. Update the **Secrets** file in ESPHome with:
   ```yaml
   wifi_ssid: "YourNetworkName"
   wifi_password: "YourWiFiPassword"
   mqtt_broker: "192.168.1.100"
   mqtt_username: "your_ha_username"
   mqtt_password: "your_ha_password"
   ```
   Note: `mqtt_broker` is your Home Assistant / Mosquitto server IP.
6. Click **Save** then **Install → Wirelessly**

## Repeat for T1, T2, T3

Repeat the entire process for each ESP32-S3, using the corresponding toolhead YAML config file. Each toolhead file only differs in its name, toolhead ID, and static IP — all shared logic comes from `base-pn532.yaml`.

## Verify

In ESPHome logs you should see:
```
Results from bus scan:
Found i2c device at address 0x24
```

Wave an NFC tag at the reader and you should see:
```
Tag scanned on T0: XX-XX-XX-XX
```
