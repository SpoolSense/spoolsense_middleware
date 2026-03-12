# scripts/

> ⚠️ **BETA** — The install script is currently under active testing. Use with caution and please report any issues on GitHub.

## install-beta.sh

Interactive install script for SpoolSense. Handles:

- Installing Python dependencies
- Writing a configured `spoolsense.py` to `~/SpoolSense/`
- Installing and enabling the systemd service
- Connectivity checks against MQTT and Spoolman
- Reconfigure or uninstall on re-run

**Usage — run from the repo root:**

```bash
bash scripts/install-beta.sh
```

Once testing is complete this will be promoted to `install.sh` and documented in the main README.
