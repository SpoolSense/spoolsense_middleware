# Install Script — Design Notes

> ⚠️ **Everything in this file is assumption and theory at this point. None of this has been tested. Treat it as a starting point for implementation, not a finished solution.**

Design notes for building an interactive install script to replace the current manual setup process.

See ENHANCEMENTS.md (Installation section) for the original idea summary.

---

## The Problem

Right now setting up this project requires:
- Manually editing config values in `nfc_listener.py`
- Copying files to the right directories on the Pi
- Setting up the systemd service by hand
- Generating ESPHome YAML files per toolhead

That's a lot of steps that are easy to get wrong, especially for someone new to the project.

---

## The Vision

`git clone` → `./install.sh` → answer a few questions → done.

The script handles everything automatically based on the user's answers. No manual file editing required.

---

## Questions the Script Asks

`TOOLHEAD_MODE` is asked first — it determines what follow-up questions are needed and which Klipper instructions to show at the end.

1. **Single toolhead or toolchanger mode?** — `single` or `toolchanger`
2. **Number of toolheads** — only asked if toolchanger mode, 1–4
3. **Static IP for each toolhead** — asked once per toolhead (e.g. T0 IP, T1 IP...)
4. **MQTT broker IP** — Home Assistant server address
5. **MQTT username and password**
6. **Spoolman IP and port** — default 7912
7. **Moonraker/Klipper IP**
8. **Low spool warning threshold** — default 100g, user can change it

---

## What the Script Does

Once all questions are answered:

**Middleware:**
- Writes configured `nfc_listener.py` with all values substituted, including `TOOLHEAD_MODE`
- Installs Python dependencies (`paho-mqtt`, `requests`)
- Copies systemd service file, replacing `YOUR_USERNAME` with the actual user
- Enables and starts the service
- Runs a connectivity check against MQTT broker and Spoolman to confirm everything is reachable

**ESPHome:**
- This is where the dependency on `shared_base_idea.md` comes in — the base config refactor needs to happen first
- Once `base.yaml` exists, the script generates a thin `toolhead-tX.yaml` wrapper for each toolhead the user configured, with the correct `toolhead_id` substitution and static IP filled in
- `base.yaml` is never touched by the install script

**Klipper:**
- If toolchanger mode, reminds the user to include `toolhead_macros_example.cfg` in their `printer.cfg` and confirms that `SET_ACTIVE_SPOOL` / `CLEAR_ACTIVE_SPOOL` are already handled by klipper-toolchanger — no additional macro changes needed
- If single mode, reminds the user to include `spoolman_macros.cfg` in their `printer.cfg` — toolchanger-specific config is not shown
- Could potentially copy the right `.cfg` files to the Klipper config directory if the path is known

---

## TOOLHEAD_MODE Behavior

**`single`:**
- Middleware subscribes to a single MQTT topic (`nfc/toolhead/T0` or similar)
- On scan: look up spool in Spoolman, call `SET_ACTIVE_SPOOL`, publish LED color
- No `SAVE_VARIABLE` or per-toolhead storage needed
- Klipper config: `spoolman_macros.cfg` only

**`toolchanger`:**
- Middleware subscribes to topics for each configured toolhead (`T0`–`TX`)
- On scan: look up spool in Spoolman, save spool ID via `SAVE_VARIABLE`, publish LED color
- Does NOT call `SET_ACTIVE_SPOOL` directly — this is handled by klipper-toolchanger macros at toolchange time
- Klipper config: `toolhead_macros_example.cfg` included, `RESTORE_SPOOL_IDS` runs on startup to restore assignments after reboot

---

## Dependencies

This script should be built **after**:
1. `shared_base_idea.md` is implemented — the ESPHome base config refactor
2. `TOOLHEAD_MODE` is implemented in the middleware — single vs toolchanger awareness

Without those two pieces in place, the install script can't generate ESPHome configs or configure the middleware correctly for both modes.

---

## End State Confirmation

After the script completes it should print a summary of what was configured and confirm:
- ✅ Middleware service is running (`TOOLHEAD_MODE` shown)
- ✅ MQTT broker reachable
- ✅ Spoolman reachable
- ✅ ESPHome YAML files generated for T0–TX
- Next steps: flash ESPHome configs to each ESP32 (still requires USB first flash)
