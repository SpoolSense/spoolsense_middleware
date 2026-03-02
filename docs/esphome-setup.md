# ESPHome Setup Guide

## Prerequisites

- Home Assistant with ESPHome addon installed
- Mosquitto MQTT broker addon installed in Home Assistant
- Chrome or Edge browser (required for USB flashing)

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

1. Click **Edit** on the device in ESPHome dashboard
2. Replace the config with the appropriate YAML from the `esphome/` folder
3. Update placeholder values:
   - `YOUR_HOME_ASSISTANT_IP` → your HA server IP
   - `static_ip` → your desired static IP for this device
   - `gateway` → your router IP
4. Update the **Secrets** file in ESPHome with:
   ```yaml
   wifi_ssid: "YourNetworkName"
   wifi_password: "YourWiFiPassword"
   mqtt_username: "your_ha_username"
   mqtt_password: "your_ha_password"
   ```
5. Click **Save** then **Install → Wirelessly**

## Repeat for T1, T2, T3

Repeat the entire process for each ESP32-S3, using the corresponding YAML config file.

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
