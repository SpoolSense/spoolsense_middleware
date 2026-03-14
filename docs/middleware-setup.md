# Middleware Setup Guide

> **This guide covers toolchanger and single toolhead setups.** AFC/BoxTurtle users: the middleware is the same (`spoolsense.py`) but configured with `toolhead_mode: "ams"` — see [integrations/afc/docs/setup.md](../integrations/afc/docs/setup.md) for AFC-specific middleware setup. Note that the AFC integration is not yet fully functional — it depends on [AFC-Klipper-Add-On PR #671](https://github.com/ArmoredTurtle/AFC-Klipper-Add-On/pull/671) being merged.

## Prerequisites

- Raspberry Pi running Klipper/Moonraker
- Python 3 installed
- Spoolman running and accessible

## Installation

1. Clone the repo:
```bash
cd ~
git clone https://github.com/sjordan0228/SpoolSense.git
```

2. Copy the config template and fill in your values:
```bash
cp ~/SpoolSense/middleware/config.example.yaml ~/SpoolSense/config.yaml
nano ~/SpoolSense/config.yaml
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

3. Install dependencies:
```bash
pip3 install paho-mqtt requests pyyaml --break-system-packages
```

4. Test manually first:
```bash
python3 ~/SpoolSense/middleware/spoolsense.py
```

You should see:
```
Starting NFC Spoolman Middleware (TOOLHEAD_MODE: toolchanger)
Config loaded from /home/youruser/SpoolSense/config.yaml
Toolheads: T0, T1, T2, T3
Connected to MQTT broker (TOOLHEAD_MODE: toolchanger)
Subscribed to nfc/toolhead/ for T0, T1, T2, T3
```

If the config file is missing or has placeholder values, the middleware will exit with a clear error telling you which fields need to be set.

## Verify Config Before Starting

Use `--check-config` to validate your config and print a summary without connecting to MQTT, Spoolman, or Moonraker:

```bash
python3 ~/SpoolSense/middleware/spoolsense.py --check-config
```

Example output:
```
Config OK: /home/pi/SpoolSense/config.yaml
  toolhead_mode    : afc
  toolheads        : lane1, lane2, lane3, lane4
  spoolman_url     : http://192.168.1.100:7912
  moonraker_url    : http://192.168.1.100
  mqtt.broker      : 192.168.1.100
  scanner_lane_map : {'ab12cd': 'lane1'}
  tag_writeback    : disabled (dry-run)
  dispatcher       : available
```

This is safe to run at any time — it exits immediately after printing the summary. Run it after any config change to catch mistakes before restarting the service.

## Enabling Tag Writeback (OpenPrintTag scanners only)

If you are using PN5180-based scanners running `openprinttag_scanner`, SpoolSense can write updated remaining weight back to tags when the tag is stale.

Writeback is **disabled by default**. When disabled, SpoolSense logs what it _would_ write without publishing anything — this is the dry-run mode.

**Recommended workflow:**
1. Leave `tag_writeback_enabled` unset (or set to `false`) on first deploy
2. Scan a few tags and watch the logs:
   ```bash
   journalctl -u spoolsense -f | grep "would write"
   ```
3. Verify the `device`, `payload`, and `reason` look correct for each scan
4. Once satisfied, enable writeback in `config.yaml`:
   ```yaml
   tag_writeback_enabled: true
   ```
5. Restart the service:
   ```bash
   sudo systemctl restart spoolsense
   ```

## Install as Systemd Service

1. Copy the service file:
```bash
sudo cp ~/SpoolSense/middleware/spoolsense.service /etc/systemd/system/
```

2. Edit the service file to replace `YOUR_USERNAME` with your actual username:
```bash
sudo nano /etc/systemd/system/spoolsense.service
```

3. Enable and start:
```bash
sudo systemctl enable spoolsense
sudo systemctl start spoolsense
sudo systemctl status spoolsense
```

## Verify

Scan an NFC tag and check the logs:
```bash
journalctl -u spoolsense -f
```

You should see:
```
NFC scan on T0: UID=XX-XX-XX-XX
Found spool: Your Filament Name (ID: 1)
Set spool 1 as active on T0 via Moonraker
```

## Updating Configuration

To change settings after install, edit `~/SpoolSense/config.yaml` and restart the service:
```bash
nano ~/SpoolSense/config.yaml
sudo systemctl restart spoolsense
```

The config file is never touched by `git pull`, so your settings are safe across updates.

## Optional: Automatic Updates via Moonraker

Add the following to your `moonraker.conf`:

```ini
[update_manager spoolsense]
type: git_repo
path: ~/SpoolSense
origin: https://github.com/sjordan0228/SpoolSense.git
primary_branch: master
managed_services: spoolsense
```

Restart Moonraker to pick up the new config:

```bash
sudo systemctl restart moonraker
```

Since the service runs `middleware/spoolsense.py` directly from the repo, updates are seamless — Moonraker pulls the latest code and restarts the service automatically. No manual file copying needed.

Your `~/SpoolSense/config.yaml` is never overwritten — it lives outside the repo.

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
