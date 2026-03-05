# Middleware Setup Guide

## Prerequisites

- Raspberry Pi running Klipper/Moonraker
- Python 3 installed
- Spoolman running and accessible

## Installation

1. Create the project directory:
```bash
mkdir -p ~/nfc_spoolman
```

2. Copy `nfc_listener.py` to the directory:
```bash
cp middleware/nfc_listener.py ~/nfc_spoolman/
```

3. Copy the config template and fill in your values:
```bash
cp middleware/config.example.yaml ~/nfc_spoolman/config.yaml
nano ~/nfc_spoolman/config.yaml
```

At minimum you need to set:
- `mqtt.broker` — your Home Assistant / Mosquitto IP
- `mqtt.username` and `mqtt.password` — MQTT credentials
- `spoolman_url` — your Spoolman instance URL
- `moonraker_url` — your Klipper/Moonraker URL

Optional settings have sensible defaults:
- `toolhead_mode` — `"toolchanger"` (default) or `"single"`
- `toolheads` — defaults to `["T0", "T1", "T2", "T3"]`
- `mqtt.port` — defaults to `1883`
- `low_spool_threshold` — defaults to `100` (grams). Controls when the low spool LED warning kicks in. Bump to 200g for an earlier heads-up, or drop to 50g for small 250g spools.

See `config.example.yaml` for full documentation on every option.

4. Install dependencies:
```bash
pip3 install paho-mqtt requests pyyaml --break-system-packages
```

5. Test manually first:
```bash
python3 ~/nfc_spoolman/nfc_listener.py
```

You should see:
```
Starting NFC Spoolman Middleware (TOOLHEAD_MODE: toolchanger)
Config loaded from /home/youruser/nfc_spoolman/config.yaml
Toolheads: T0, T1, T2, T3
Connected to MQTT broker (TOOLHEAD_MODE: toolchanger)
Subscribed to nfc/toolhead/ for T0, T1, T2, T3
```

If the config file is missing or has placeholder values, the middleware will exit with a clear error telling you which fields need to be set.

## Install as Systemd Service

1. Copy the service file:
```bash
sudo cp middleware/nfc-spoolman.service /etc/systemd/system/
```

2. Edit the service file to replace `YOUR_USERNAME` with your actual username:
```bash
sudo nano /etc/systemd/system/nfc-spoolman.service
```

3. Enable and start:
```bash
sudo systemctl enable nfc-spoolman
sudo systemctl start nfc-spoolman
sudo systemctl status nfc-spoolman
```

## Verify

Scan an NFC tag and check the logs:
```bash
journalctl -u nfc-spoolman -f
```

You should see:
```
NFC scan on T0: UID=XX-XX-XX-XX
Found spool: Your Filament Name (ID: 1)
Set spool 1 as active on T0 via Moonraker
```

## Updating Configuration

To change settings after install, edit `~/nfc_spoolman/config.yaml` and restart the service:
```bash
nano ~/nfc_spoolman/config.yaml
sudo systemctl restart nfc-spoolman
```

The config file is never touched by `git pull`, so your settings are safe across updates.

## Optional: Home Assistant Monitoring

The middleware publishes its online/offline status to `nfc/middleware/online` (retained, QoS 1). If the middleware crashes or loses its MQTT connection, the broker automatically publishes `false` via Last Will and Testament. A clean shutdown also publishes `false` before disconnecting.

If you use Home Assistant, you can optionally surface this as a dashboard sensor. Add the following to your `configuration.yaml`:

```yaml
mqtt:
  binary_sensor:
    - name: "NFC Middleware"
      state_topic: "nfc/middleware/online"
      payload_on: "true"
      payload_off: "false"
      device_class: connectivity
```

This gives you a "Connected / Disconnected" indicator in your HA dashboard and makes it easy to build automations — for example, a notification if the middleware goes offline during a print.
