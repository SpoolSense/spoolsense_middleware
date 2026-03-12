#!/usr/bin/env python3
"""
NFC Spoolman Middleware — Unified Edition with AFC Sync & LED Color Override
=============================================================================
Listens for NFC tag scans via MQTT and updates Klipper/Spoolman.
Includes automatic lock/clear logic by watching AFC's variable file,
and overrides BoxTurtle LEDs with actual filament colors from Spoolman.

Supports three toolhead modes (set toolhead_mode in config.yaml):

  single      — Calls SET_ACTIVE_SPOOL directly on every scan.
                Use for single-toolhead printers (one ESP32, one PN532).

  toolchanger — Saves the spool ID per toolhead and publishes the LED color,
                but does NOT call SET_ACTIVE_SPOOL. klipper-toolchanger handles
                activation at each toolchange. Tested on MadMax T0–T3.

  ams         — Calls AFC's SET_SPOOL_ID to register the spool in the correct
                lane. AFC auto-pulls color, material, and weight from Spoolman.
                After a successful scan, locks the scanner on that lane.
                Watches AFC.var.unit for lane changes (eject → clear scanner).
                Overrides BoxTurtle LEDs with filament color via a Klipper macro.
                Designed for BoxTurtle, NightOwl, and other AFC-based units.

LED Override Strategy (AMS mode):
  AFC sets lane LEDs to hardcoded colors (green=ready, blue=loaded, etc.).
  This middleware overrides led_ready and led_tool_loaded with the actual
  filament color from Spoolman, so the BoxTurtle LEDs show what color
  filament is in each lane. Critical states are never overridden:
    - led_fault (red)     — never override, indicates a real problem
    - led_loading (white) — never override, animation in progress
    - led_not_ready (red) — never override, lane needs attention
  The override is re-asserted on every AFC.var.unit file change, since AFC
  resets LEDs to defaults on state transitions. Our gcode command runs after
  AFC's internal LED set, so we reliably "win" the race.

Configuration is loaded from ~/SpoolSense/config.yaml — see config.example.yaml.
"""

import paho.mqtt.client as mqtt
import requests
import json
import logging
import signal
import sys
import os
import yaml
import time
import configparser
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

# ============================================================
# Configuration
# ============================================================

CONFIG_PATH = os.path.expanduser("~/SpoolSense/config.yaml")

DEFAULTS = {
    "toolhead_mode": "ams",
    "toolheads": ["lane1", "lane2", "lane3", "lane4"],
    "mqtt": {
        "broker": None,
        "port": 1883,
        "username": None,
        "password": None,
    },
    "spoolman_url": None,
    "moonraker_url": None,
    "low_spool_threshold": 100,
    "afc_var_path": "~/printer_data/config/AFC/AFC.var.unit",
    "klipper_var_path": None,
    "ams_led_macro": "_SET_LANE_LED",
}

VALID_MODES = ("single", "toolchanger", "ams")

# AFC LED states that must never be overridden
AFC_PROTECTED_STATES = {"led_fault", "led_loading", "led_not_ready"}
# AFC LED states where we override with filament color
AFC_COLORABLE_STATES = {"led_ready", "led_tool_loaded", "led_buffer_advancing",
                        "led_buffer_trailing"}

# Global state
spool_cache = {}
last_cache_refresh = 0
CACHE_TTL = 3600
lane_locks = {}        # lane_name -> bool
active_spools = {}     # toolhead -> spool_id
lane_statuses = {}     # lane_name -> status string (cached from AFC var file)
last_led_state = {}    # lane -> (color_hex, is_low)
mqtt_client = None
watcher = None


def load_config():
    """Load and validate configuration from ~/SpoolSense/config.yaml."""
    if not os.path.exists(CONFIG_PATH):
        logging.error(f"Config file not found: {CONFIG_PATH}")
        logging.error("Copy the template:  cp config.example.yaml ~/SpoolSense/config.yaml")
        sys.exit(1)

    try:
        with open(CONFIG_PATH, "r") as f:
            user_config = yaml.safe_load(f) or {}
    except Exception as e:
        logging.error(f"Failed to read/parse {CONFIG_PATH}: {e}")
        sys.exit(1)

    mqtt_cfg = {**DEFAULTS["mqtt"], **user_config.get("mqtt", {})}
    config = {**DEFAULTS, **user_config}
    config["mqtt"] = mqtt_cfg
    config["afc_var_path"] = os.path.expanduser(config.get("afc_var_path", DEFAULTS["afc_var_path"]))
    if config.get("klipper_var_path"):
        config["klipper_var_path"] = os.path.expanduser(config["klipper_var_path"])

    # Validate required fields
    missing = []
    if not config["mqtt"]["broker"]: missing.append("mqtt.broker")
    if not config["spoolman_url"]: missing.append("spoolman_url")
    if not config["moonraker_url"]: missing.append("moonraker_url")

    if missing:
        logging.error(f"Missing required values in {CONFIG_PATH}: {', '.join(missing)}")
        sys.exit(1)

    if config["toolhead_mode"] not in VALID_MODES:
        logging.error(f"Invalid toolhead_mode: '{config['toolhead_mode']}' — must be one of: {', '.join(VALID_MODES)}")
        sys.exit(1)

    config["spoolman_url"] = config["spoolman_url"].rstrip("/")
    config["moonraker_url"] = config["moonraker_url"].rstrip("/")

    return config


cfg = load_config()

# ============================================================
# Discovery Helpers
# ============================================================


def discover_klipper_var_path():
    """Query Moonraker to find the actual save_variables file path."""
    if cfg.get("klipper_var_path"):
        return cfg["klipper_var_path"]

    try:
        logging.info("Discovering Klipper save_variables path...")
        response = requests.get(f"{cfg['moonraker_url']}/printer/configfile/settings", timeout=5)
        response.raise_for_status()
        settings = response.json().get("result", {}).get("settings", {})
        filename = settings.get("save_variables", {}).get("filename")

        if not filename:
            logging.warning("No [save_variables] in Klipper config. Klipper sync disabled.")
            return None

        if not filename.startswith("/"):
            filename = os.path.join(os.path.expanduser("~/printer_data/config"), filename)

        logging.info(f"Discovered Klipper variables file: {filename}")
        return filename
    except Exception as e:
        logging.error(f"Failed to discover Klipper variables path: {e}")
        return None


# ============================================================
# Spoolman Helpers
# ============================================================


def get_spool_by_id(spool_id):
    """Fetch a single spool by ID from Spoolman."""
    try:
        response = requests.get(f"{cfg['spoolman_url']}/api/v1/spool/{spool_id}", timeout=5)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        logging.error(f"Failed to fetch spool {spool_id}: {e}")
        return None


def refresh_spool_cache():
    """Fetch all spools from Spoolman and update the local NFC UID cache."""
    global spool_cache, last_cache_refresh
    try:
        logging.info("Refreshing Spoolman cache...")
        response = requests.get(f"{cfg['spoolman_url']}/api/v1/spool", timeout=5)
        response.raise_for_status()
        spools = response.json()

        new_cache = {}
        for spool in spools:
            extra = spool.get("extra", {})
            nfc_id = extra.get("nfc_id", "").strip('"').lower()
            if nfc_id:
                new_cache[nfc_id] = spool

        spool_cache = new_cache
        last_cache_refresh = time.time()
        logging.info(f"Cache updated: {len(spool_cache)} spools indexed.")
        return True
    except Exception as e:
        logging.error(f"Failed to refresh Spoolman cache: {e}")
        return False


def find_spool_by_nfc(uid):
    """Look up a spool in the local cache by NFC UID, with auto-refresh."""
    uid_lower = uid.lower()
    if time.time() - last_cache_refresh > CACHE_TTL:
        refresh_spool_cache()
    if uid_lower in spool_cache:
        return spool_cache[uid_lower]
    logging.info(f"UID {uid} not in cache, performing forced refresh...")
    if refresh_spool_cache():
        return spool_cache.get(uid_lower)
    return None


# ============================================================
# Klipper/Moonraker Actions
# ============================================================


def hex_to_rgb(hex_str):
    """Convert hex color string to (r, g, b) floats 0.0-1.0 for Klipper."""
    hex_str = hex_str.lstrip('#')
    if len(hex_str) != 6:
        return (1.0, 1.0, 1.0)
    return tuple(int(hex_str[i:i+2], 16) / 255.0 for i in (0, 2, 4))


def update_klipper_led(lane, color_hex, is_low=False, force=False):
    """
    Call the Klipper macro to update physical LEDs on the BoxTurtle.

    Only active in AMS mode. Checks lane_statuses cache to skip protected
    AFC states (fault, loading, not_ready). Uses last_led_state to avoid
    redundant gcode calls unless force=True.

    Args:
        lane: AFC lane name (e.g. 'lane1').
        color_hex: Hex color string (e.g. 'FF0000').
        is_low: If True, the macro dims the LED to 20% as a low spool warning.
        force: If True, send the command even if state hasn't changed.
    """
    if cfg["toolhead_mode"] != "ams":
        return

    # Check cached lane status — never override protected AFC states
    status = lane_statuses.get(lane)
    if status in AFC_PROTECTED_STATES:
        logging.debug(f"LED: Skipping {lane} — AFC state is {status}")
        return

    # Dedup: skip if state hasn't changed
    current_state = (color_hex, is_low)
    if not force and last_led_state.get(lane) == current_state:
        return

    r, g, b = hex_to_rgb(color_hex)
    breath = 1 if is_low else 0

    script = f"{cfg['ams_led_macro']} LANE={lane} R={r:.3f} G={g:.3f} B={b:.3f} BREATH={breath}"
    try:
        requests.post(
            f"{cfg['moonraker_url']}/printer/gcode/script",
            json={"script": script},
            timeout=2
        )
        last_led_state[lane] = current_state
        logging.info(f"LED: {lane} -> #{color_hex} (low={is_low})")
    except Exception as e:
        logging.error(f"Failed to update LED on {lane}: {e}")


def activate_spool(spool_id, toolhead):
    """Route spool activation to the correct mode handler."""
    mode = cfg["toolhead_mode"]
    try:
        if mode == "single":
            requests.post(f"{cfg['moonraker_url']}/server/spoolman/spool_id",
                          json={"spool_id": spool_id}, timeout=5).raise_for_status()
            requests.post(f"{cfg['moonraker_url']}/printer/gcode/script",
                          json={"script": f"SAVE_VARIABLE VARIABLE=t0_spool_id VALUE={spool_id}"},
                          timeout=5).raise_for_status()
            logging.info(f"[single] Activated spool {spool_id}")

        elif mode == "toolchanger":
            macro = f"T{toolhead[-1]}"
            requests.post(f"{cfg['moonraker_url']}/printer/gcode/script",
                          json={"script": f"SET_GCODE_VARIABLE MACRO={macro} VARIABLE=spool_id VALUE={spool_id}"},
                          timeout=5).raise_for_status()
            requests.post(f"{cfg['moonraker_url']}/printer/gcode/script",
                          json={"script": f"SAVE_VARIABLE VARIABLE=t{toolhead[-1]}_spool_id VALUE={spool_id}"},
                          timeout=5).raise_for_status()
            logging.info(f"[toolchanger] Updated {macro} with spool {spool_id}")

        elif mode == "ams":
            requests.post(f"{cfg['moonraker_url']}/printer/gcode/script",
                          json={"script": f"SET_SPOOL_ID LANE={toolhead} SPOOL_ID={spool_id}"},
                          timeout=5).raise_for_status()
            logging.info(f"[ams] Set spool {spool_id} on {toolhead} via AFC")

        return True
    except Exception as e:
        logging.error(f"Activation failed: {e}")
        return False


# ============================================================
# MQTT Logic
# ============================================================


def publish_lock(lane, state):
    """Publish lock/clear command to ESP32 scanner. state: 'lock' or 'clear'."""
    if not mqtt_client:
        return
    topic = f"nfc/toolhead/{lane}/lock"
    mqtt_client.publish(topic, state, retain=True)
    lane_locks[lane] = (state == "lock")
    logging.info(f"MQTT: {lane} -> {state}")


def on_connect(client, userdata, flags, rc):
    """Callback for MQTT connection."""
    if rc == 0:
        logging.info(f"Connected to MQTT broker (Mode: {cfg['toolhead_mode']})")
        client.publish("nfc/middleware/online", "true", qos=1, retain=True)
        for t in cfg["toolheads"]:
            client.subscribe(f"nfc/toolhead/{t}")
        logging.info(f"Subscribed to: {', '.join(cfg['toolheads'])}")
        refresh_spool_cache()

        # Initial sync
        if cfg["toolhead_mode"] == "ams":
            sync_from_afc_file()
        else:
            cfg["klipper_var_path"] = discover_klipper_var_path()
            sync_from_klipper_vars()
            # Restart watcher with discovered path
            global watcher
            if watcher:
                watcher.stop()
                watcher.join(timeout=2)
            watcher = start_watcher()
    else:
        logging.error(f"MQTT connection failed: {rc}")


def on_message(client, userdata, msg):
    """Callback for received NFC scan messages."""
    try:
        payload = json.loads(msg.payload.decode())
        uid = payload.get("uid")
        toolhead = payload.get("toolhead")

        if lane_locks.get(toolhead):
            logging.info(f"Ignoring scan on {toolhead} (locked)")
            return

        spool = find_spool_by_nfc(uid)
        if spool:
            spool_id = spool["id"]
            filament = spool.get("filament", {})
            name = filament.get("name", "Unknown")
            color_hex = filament.get("color_hex", "FFFFFF") or "FFFFFF"
            logging.info(f"Found spool: {name} (ID: {spool_id})")

            if activate_spool(spool_id, toolhead):
                active_spools[toolhead] = spool_id

                if cfg["toolhead_mode"] == "ams":
                    publish_lock(toolhead, "lock")
                    # Override BoxTurtle LED with filament color
                    # (checks lane_statuses internally, skips protected states)
                    remaining = spool.get("remaining_weight")
                    is_low = (remaining is not None and remaining <= cfg["low_spool_threshold"])
                    update_klipper_led(toolhead, color_hex, is_low)
                else:
                    # Single/toolchanger: publish color to ESP32 LED
                    client.publish(f"nfc/toolhead/{toolhead}/color",
                                   color_hex.lstrip("#").upper(), qos=1, retain=True)

                # Low spool check
                remaining = spool.get("remaining_weight")
                if cfg["toolhead_mode"] == "ams":
                    if remaining is not None and remaining <= cfg["low_spool_threshold"]:
                        logging.warning(f"Low spool: {name} ({remaining:.1f}g) on {toolhead}")
                else:
                    topic_low = f"nfc/toolhead/{toolhead}/low_spool"
                    if remaining is not None and remaining <= cfg["low_spool_threshold"]:
                        logging.warning(f"Low spool: {name} ({remaining:.1f}g) on {toolhead}")
                        client.publish(topic_low, "true", qos=1, retain=True)
                    else:
                        client.publish(topic_low, "false", qos=1, retain=True)
        else:
            logging.warning(f"No spool found for UID: {uid}")
            if cfg["toolhead_mode"] != "ams":
                # Clear low spool state and flash red
                client.publish(f"nfc/toolhead/{toolhead}/low_spool", "false", qos=1, retain=True)
                client.publish(f"nfc/toolhead/{toolhead}/color", "error", qos=1, retain=True)

    except Exception as e:
        logging.error(f"Message error: {e}")


# ============================================================
# Variable File Watchers (AFC & Klipper)
# ============================================================


def sync_from_klipper_vars():
    """Read Klipper's save_variables file and update LEDs for single/toolchanger modes."""
    path = cfg.get("klipper_var_path")
    if not path or not os.path.exists(path):
        return

    try:
        cp = configparser.ConfigParser()
        cp.read(path)
        if 'variables' not in cp:
            return

        for t in cfg["toolheads"]:
            var_name = f"t{t[-1]}_spool_id"
            spool_id_str = cp['variables'].get(var_name)

            if spool_id_str:
                try:
                    spool_id = int(spool_id_str)
                    if active_spools.get(t) != spool_id:
                        logging.info(f"Klipper Sync: {t} -> spool {spool_id}")
                        spool = get_spool_by_id(spool_id)
                        if spool:
                            color = spool.get("filament", {}).get("color_hex", "FFFFFF") or "FFFFFF"
                            mqtt_client.publish(f"nfc/toolhead/{t}/color",
                                                color.lstrip("#").upper(), qos=1, retain=True)
                            active_spools[t] = spool_id
                except ValueError:
                    pass
            elif active_spools.get(t):
                logging.info(f"Klipper Sync: {t} cleared")
                mqtt_client.publish(f"nfc/toolhead/{t}/color", "000000", qos=1, retain=True)
                active_spools[t] = None
    except Exception as e:
        logging.error(f"Klipper Sync failed: {e}")


def sync_from_afc_file():
    """
    Read AFC.var.unit and sync lock/clear + LED color state.

    This runs on startup and on every AFC.var.unit file change. It:
      1. Caches lane statuses for get_lane_status() lookups.
      2. Locks scanners for lanes with spools, clears empty lanes.
      3. Re-asserts filament color on BoxTurtle LEDs (force=True) to
         override AFC's default colors after state transitions.
      4. Skips LED override for protected AFC states (fault, loading).
      5. Only fetches spool data from Spoolman when the spool_id has
         changed, avoiding unnecessary API calls on every file tick.
    """
    path = cfg["afc_var_path"]
    if not os.path.exists(path):
        logging.warning(f"AFC var file not found: {path}")
        return

    try:
        with open(path, "r") as f:
            data = json.load(f)

        for unit_name, unit_data in data.items():
            if unit_name == "system":
                continue
            for lane_name, lane_data in unit_data.items():
                spool_id = lane_data.get("spool_id")
                status = lane_data.get("status")
                is_locked = lane_locks.get(lane_name, False)

                # Cache lane status for on_message lookups
                lane_statuses[lane_name] = status

                if spool_id:
                    # Lock scanner if not already locked
                    if not is_locked:
                        logging.info(f"AFC Sync: {lane_name} has spool {spool_id}, locking")
                        publish_lock(lane_name, "lock")

                    # Skip LED override for protected states
                    if status in AFC_PROTECTED_STATES:
                        continue

                    # Only fetch from Spoolman if spool changed
                    # (avoids 4 API calls per file tick when nothing changed)
                    if active_spools.get(lane_name) == spool_id and not last_led_state.get(lane_name) is None:
                        # Spool hasn't changed — still re-assert LED with force
                        # to win the race against AFC's default color reset,
                        # but use cached state instead of hitting Spoolman
                        cached = last_led_state.get(lane_name)
                        if cached:
                            update_klipper_led(lane_name, cached[0], cached[1], force=True)
                        continue

                    spool = get_spool_by_id(spool_id)
                    if spool:
                        color = spool.get("filament", {}).get("color_hex", "FFFFFF") or "FFFFFF"
                        remaining = spool.get("remaining_weight")
                        is_low = (remaining is not None and remaining <= cfg["low_spool_threshold"])
                        active_spools[lane_name] = spool_id
                        update_klipper_led(lane_name, color, is_low, force=True)
                else:
                    # Lane is empty
                    if is_locked:
                        logging.info(f"AFC Sync: {lane_name} empty, clearing")
                        publish_lock(lane_name, "clear")
                    if active_spools.get(lane_name):
                        update_klipper_led(lane_name, "000000", False, force=True)
                        active_spools[lane_name] = None
    except Exception as e:
        logging.error(f"AFC Sync failed: {e}")


class VarFileHandler(FileSystemEventHandler):
    """Watchdog handler for AFC and Klipper variable file changes."""
    def on_modified(self, event):
        # Small delay to ensure the file write is complete
        time.sleep(0.5)
        if event.src_path == cfg["afc_var_path"]:
            sync_from_afc_file()
        elif event.src_path == cfg.get("klipper_var_path"):
            sync_from_klipper_vars()


def start_watcher():
    """Start file system watchers for AFC and/or Klipper variable files."""
    observer = Observer()
    handler = VarFileHandler()

    if cfg["toolhead_mode"] == "ams":
        afc_dir = os.path.dirname(cfg["afc_var_path"])
        if os.path.exists(afc_dir):
            observer.schedule(handler, afc_dir, recursive=False)
            logging.info(f"Watching AFC var file in {afc_dir}")
    else:
        klipper_path = cfg.get("klipper_var_path")
        if klipper_path:
            klipper_dir = os.path.dirname(klipper_path)
            if os.path.exists(klipper_dir):
                observer.schedule(handler, klipper_dir, recursive=False)
                logging.info(f"Watching Klipper var file in {klipper_dir}")

    observer.start()
    return observer


# ============================================================
# Main
# ============================================================


def on_shutdown(signum, frame):
    """Clean shutdown: publish offline, clear AMS locks, stop watcher."""
    logging.info("Shutting down...")
    if mqtt_client:
        mqtt_client.publish("nfc/middleware/online", "false", qos=1, retain=True)
        if cfg["toolhead_mode"] == "ams":
            for lane in cfg["toolheads"]:
                publish_lock(lane, "clear")
        mqtt_client.disconnect()
    if watcher:
        watcher.stop()
    sys.exit(0)


signal.signal(signal.SIGTERM, on_shutdown)
signal.signal(signal.SIGINT, on_shutdown)

mqtt_client = mqtt.Client()
if cfg["mqtt"].get("username"):
    mqtt_client.username_pw_set(cfg["mqtt"]["username"], cfg["mqtt"].get("password"))

mqtt_client.on_connect = on_connect
mqtt_client.on_message = on_message
mqtt_client.will_set("nfc/middleware/online", "false", qos=1, retain=True)

logging.info(f"Starting NFC Middleware (Mode: {cfg['toolhead_mode']})")
logging.info(f"Spoolman: {cfg['spoolman_url']}")
logging.info(f"Moonraker: {cfg['moonraker_url']}")
if cfg["toolhead_mode"] == "ams":
    logging.info(f"Lanes: {', '.join(cfg['toolheads'])}")
    logging.info(f"AFC var file: {cfg['afc_var_path']}")
    logging.info(f"LED macro: {cfg['ams_led_macro']}")
else:
    logging.info(f"Toolheads: {', '.join(cfg['toolheads'])}")
    logging.info(f"Low spool threshold: {cfg['low_spool_threshold']}g")

try:
    mqtt_client.connect(cfg["mqtt"]["broker"], cfg["mqtt"]["port"], 60)
    watcher = start_watcher()
    mqtt_client.loop_forever()
except Exception as e:
    logging.error(f"Fatal error: {e}")
    sys.exit(1)
