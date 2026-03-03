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
cp nfc_listener.py ~/nfc_spoolman/
```

3. Edit the configuration at the top of `nfc_listener.py`:
```python
MQTT_BROKER = "YOUR_HOME_ASSISTANT_IP"
MQTT_USERNAME = "your_mqtt_username"
MQTT_PASSWORD = "your_mqtt_password"
SPOOLMAN_URL = "http://YOUR_SPOOLMAN_IP:7912"
MOONRAKER_URL = "http://YOUR_KLIPPER_IP"
LOW_SPOOL_THRESHOLD = 100  # grams — LED breathes when remaining weight hits this level
```

`LOW_SPOOL_THRESHOLD` controls when the low spool warning kicks in. The default is 100g which works well for standard 1kg spools. Adjust based on your setup — bump it up to 200g if you want an earlier heads-up, or drop it to 50g if you're running small 250g spools and want to squeeze more out before the warning triggers.

4. Install dependencies:
```bash
pip3 install paho-mqtt requests --break-system-packages
```

5. Test manually first:
```bash
python3 ~/nfc_spoolman/nfc_listener.py
```

You should see:
```
Connected to MQTT broker
Subscribed to nfc/toolhead/#
```

## Install as Systemd Service

1. Copy the service file:
```bash
sudo cp nfc-spoolman.service /etc/systemd/system/
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
