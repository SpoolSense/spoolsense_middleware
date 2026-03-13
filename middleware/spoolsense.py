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

  afc         — Calls AFC's SET_SPOOL_ID to register the spool in the correct
                lane. AFC auto-pulls color, material, and weight from Spoolman.
                After a successful scan, locks the scanner on that lane.
                Watches AFC.var.unit for lane changes (eject → clear scanner).
                Overrides BoxTurtle LEDs with filament color via a Klipper macro.
                Designed for BoxTurtle, NightOwl, and other AFC-based units.

LED Override Strategy (AFC mode):
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

# Dispatcher for rich-data NFC tag formats (OpenTag3D, openprinttag_scanner)
try:
    from adapters.dispatcher import detect_and_parse, detect_format
    from state.models import ScanEvent
    from spoolman.client import SpoolmanClient
    DISPATCHER_AVAILABLE = True
except ImportError:
    DISPATCHER_AVAILABLE = False

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

# ============================================================
# Configuration & Global State
# ============================================================

CONFIG_PATH = os.path.expanduser("~/SpoolSense/config.yaml")

DEFAULTS = {
    "toolhead_mode": "afc",
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
    "afc_led_macro": "_SET_LANE_LED",
    # openprinttag_scanner settings (optional — only needed for PN5180 setups)
    # Maps scanner MQTT device IDs to lane/toolhead names.
    # Each ESP32 running openprinttag_scanner publishes to:
    #   openprinttag/<device_id>/tag/state
    # This mapping tells the middleware which lane each scanner belongs to.
    "scanner_topic_prefix": "openprinttag",
    "scanner_lane_map": {},  # e.g. {"scanner-lane1": "lane1", "scanner-lane2": "lane2"}
}

VALID_MODES = ("single", "toolchanger", "afc")

# NOTE: AFC likes to take control of LEDs. We define which states we are allowed 
# to overwrite with our custom filament colors, and which ones mean "danger/busy" 
# and should be left alone.
AFC_PROTECTED_STATES = {"led_fault", "led_loading", "led_not_ready"}
AFC_COLORABLE_STATES = {"led_ready", "led_tool_loaded", "led_buffer_advancing",
                        "led_buffer_trailing"}

# --- Global State Caches ---
# We cache data in memory so we don't hammer the Spoolman or Moonraker APIs every second.
spool_cache = {}
last_cache_refresh = 0
CACHE_TTL = 3600       # How long (in seconds) before we force a full Spoolman re-sync

lane_locks = {}        # Tracks if a lane's NFC reader is locked (prevents duplicate scans)
active_spools = {}     # Maps toolhead/lane to the currently loaded spool_id
lane_statuses = {}     # Caches the last known AFC status (e.g., 'led_ready')
last_led_state = {}    # Caches the last color we sent to Klipper so we don't spam G-code
mqtt_client = None
watcher = None

# SpoolmanClient for rich-data tag sync (OpenTag3D, openprinttag_scanner)
spoolman_client = None
if DISPATCHER_AVAILABLE:
    spoolman_client = SpoolmanClient(cfg["spoolman_url"])


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
    """
    Queries Moonraker to find exactly where Klipper is saving its variables.
    This is better than hardcoding it, because users put save_variables.cfg in different places.
    """
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
    """Fetch a single spool directly from Spoolman."""
    try:
        response = requests.get(f"{cfg['spoolman_url']}/api/v1/spool/{spool_id}", timeout=5)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        logging.error(f"Failed to fetch spool {spool_id}: {e}")
        return None

def refresh_spool_cache():
    """
    Pulls ALL spools from Spoolman and builds a local dictionary mapping NFC UIDs to Spoolman data.
    We do this so when a tag is scanned, the lookup is instant instead of waiting on a network request.
    """
    global spool_cache, last_cache_refresh
    try:
        logging.info("Refreshing Spoolman cache...")
        response = requests.get(f"{cfg['spoolman_url']}/api/v1/spool", timeout=5)
        response.raise_for_status()
        spools = response.json()

        new_cache = {}
        for spool in spools:
            # Look for the nfc_id inside Spoolman's "extra" fields
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
    """
    Looks up a scanned NFC UID in our local memory cache.
    If it's not there, or the cache is too old, it forces a refresh.
    """
    uid_lower = uid.lower()
    if time.time() - last_cache_refresh > CACHE_TTL:
        refresh_spool_cache()
        
    if uid_lower in spool_cache:
        return spool_cache[uid_lower]
        
    # If we didn't find it, maybe it was just added to Spoolman 5 seconds ago. Force a refresh.
    logging.info(f"UID {uid} not in cache, performing forced refresh...")
    if refresh_spool_cache():
        return spool_cache.get(uid_lower)
    return None

# ============================================================
# Klipper/Moonraker Actions
# ============================================================

def hex_to_rgb(hex_str):
    """Converts standard HTML hex colors (#FF0000) to Klipper's 0.0-1.0 RGB format."""
    hex_str = hex_str.lstrip('#')
    if len(hex_str) != 6:
        return (1.0, 1.0, 1.0) # Default to white if invalid
    return tuple(int(hex_str[i:i+2], 16) / 255.0 for i in (0, 2, 4))

def update_klipper_led(lane, color_hex, is_low=False, force=False):
    """
    Sends the G-code to change the physical LED color.
    NOTE: This includes "debounce" logic so we don't spam Klipper with the exact 
    same LED command 10 times a second.
    """
    if cfg["toolhead_mode"] != "afc":
        return

    # Don't overwrite AFC if it's currently showing an error or loading animation
    status = lane_statuses.get(lane)
    if status in AFC_PROTECTED_STATES:
        logging.debug(f"LED: Skipping {lane} — AFC state is {status}")
        return

    # Debounce: If the LED is already this color, do nothing (unless forced)
    current_state = (color_hex, is_low)
    if not force and last_led_state.get(lane) == current_state:
        return

    r, g, b = hex_to_rgb(color_hex)
    breath = 1 if is_low else 0

    script = f"{cfg['afc_led_macro']} LANE={lane} R={r:.3f} G={g:.3f} B={b:.3f} BREATH={breath}"
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
    """
    Routes the spool activation to the correct Klipper logic based on your setup.
    """
    mode = cfg["toolhead_mode"]
    try:
        if mode == "single":
            # Tell Moonraker/Spoolman directly
            requests.post(f"{cfg['moonraker_url']}/server/spoolman/spool_id",
                          json={"spool_id": spool_id}, timeout=5).raise_for_status()
            # Save it to Klipper variables so macros survive a restart
            requests.post(f"{cfg['moonraker_url']}/printer/gcode/script",
                          json={"script": f"SAVE_VARIABLE VARIABLE=t0_spool_id VALUE={spool_id}"},
                          timeout=5).raise_for_status()
            logging.info(f"[single] Activated spool {spool_id}")

        elif mode == "toolchanger":
            macro = f"T{toolhead[-1]}"
            # Update the specific tool's macro variable
            requests.post(f"{cfg['moonraker_url']}/printer/gcode/script",
                          json={"script": f"SET_GCODE_VARIABLE MACRO={macro} VARIABLE=spool_id VALUE={spool_id}"},
                          timeout=5).raise_for_status()
            # Save to disk
            requests.post(f"{cfg['moonraker_url']}/printer/gcode/script",
                          json={"script": f"SAVE_VARIABLE VARIABLE=t{toolhead[-1]}_spool_id VALUE={spool_id}"},
                          timeout=5).raise_for_status()
            logging.info(f"[toolchanger] Updated {macro} with spool {spool_id}")

        elif mode == "afc":
            # Let AFC handle the actual assignment logic
            requests.post(f"{cfg['moonraker_url']}/printer/gcode/script",
                          json={"script": f"SET_SPOOL_ID LANE={toolhead} SPOOL_ID={spool_id}"},
                          timeout=5).raise_for_status()
            logging.info(f"[afc] Set spool {spool_id} on {toolhead} via AFC")

        return True
    except Exception as e:
        logging.error(f"Activation failed: {e}")
        return False

# ============================================================
# MQTT Logic
# ============================================================

def publish_lock(lane, state):
    """Tells the ESP32 NFC reader to ignore further scans (lock) or accept them (clear)."""
    if not mqtt_client:
        return
    topic = f"nfc/toolhead/{lane}/lock"
    mqtt_client.publish(topic, state, retain=True)
    lane_locks[lane] = (state == "lock")
    logging.info(f"MQTT: {lane} -> {state}")

def on_connect(client, userdata, flags, rc):
    """Fires when we successfully connect to the MQTT broker."""
    if rc == 0:
        logging.info(f"Connected to MQTT broker (Mode: {cfg['toolhead_mode']})")
        client.publish("nfc/middleware/online", "true", qos=1, retain=True)
        
        # Subscribe to all our configured lanes/toolheads (PN532/ESPHome path)
        for t in cfg["toolheads"]:
            client.subscribe(f"nfc/toolhead/{t}")
        logging.info(f"Subscribed to: {', '.join(cfg['toolheads'])}")

        # Subscribe to openprinttag_scanner topics (PN5180 path)
        # Each scanner publishes to: openprinttag/<device_id>/tag/state
        scanner_map = cfg.get("scanner_lane_map", {})
        if scanner_map:
            prefix = cfg.get("scanner_topic_prefix", "openprinttag")
            for device_id in scanner_map:
                topic = f"{prefix}/{device_id}/tag/state"
                client.subscribe(topic)
            logging.info(f"Subscribed to {len(scanner_map)} openprinttag_scanner(s): {', '.join(scanner_map.keys())}")
            
        refresh_spool_cache()

        # Kick off the initial state sync based on our mode
        if cfg["toolhead_mode"] == "afc":
            sync_from_afc_file()
        else:
            cfg["klipper_var_path"] = discover_klipper_var_path()
            sync_from_klipper_vars()
            
            # Restart the file watcher now that we know the path
            global watcher
            if watcher:
                watcher.stop()
                watcher.join(timeout=2)
            watcher = start_watcher()
    else:
        logging.error(f"MQTT connection failed: {rc}")

def _resolve_lane_from_topic(topic):
    """
    Determines the lane/toolhead name from an MQTT topic.
    
    For PN532/ESPHome topics like 'nfc/toolhead/lane1', returns 'lane1'.
    For openprinttag_scanner topics like 'openprinttag/scanner-lane1/tag/state',
    looks up the device ID in scanner_lane_map to find the lane name.
    Returns None if the topic can't be mapped to a lane.
    """
    prefix = cfg.get("scanner_topic_prefix", "openprinttag")
    scanner_map = cfg.get("scanner_lane_map", {})

    # Check if it's a scanner topic: openprinttag/<device_id>/tag/state
    if topic.startswith(f"{prefix}/") and topic.endswith("/tag/state"):
        parts = topic.split("/")
        if len(parts) >= 4:
            device_id = parts[1]
            lane = scanner_map.get(device_id)
            if lane:
                return lane
            logging.warning(f"Scanner device '{device_id}' not found in scanner_lane_map")
            return None

    # Otherwise it's a PN532 topic: nfc/toolhead/<lane>
    parts = topic.split("/")
    if len(parts) >= 3 and parts[0] == "nfc" and parts[1] == "toolhead":
        return parts[2]

    return None


def _handle_rich_tag(client, toolhead, payload, topic):
    """
    Handles a rich-data NFC tag (OpenTag3D or openprinttag_scanner).
    
    Routes through the dispatcher to parse the tag data into a ScanEvent,
    syncs with Spoolman (creating the spool if needed), then activates it
    in Klipper/AFC the same way as a plain UID scan.
    """
    try:
        scan = detect_and_parse(payload, toolhead, topic)
        logging.info(f"Rich tag parsed: {scan.source} — {scan.brand_name} {scan.material_type} (UID: {scan.uid})")

        # Guard: scanner may report present=False or invalid data
        if not scan.present:
            logging.debug(f"Scanner reported no tag present on {toolhead}")
            return
        if not scan.tag_data_valid:
            logging.warning(f"Scanner reported invalid tag data on {toolhead}")
            return

        # Sync with Spoolman — creates the spool if it doesn't exist yet,
        # or merges data if it does. Returns SpoolInfo with spoolman_id set.
        spool_info = spoolman_client.sync_spool_from_scan(scan, prefer_tag=True)

        if spool_info and spool_info.spoolman_id:
            if activate_spool(spool_info.spoolman_id, toolhead):
                active_spools[toolhead] = spool_info.spoolman_id

                color_hex = spool_info.color_hex or scan.color_hex or "FFFFFF"
                remaining = spool_info.remaining_weight_g or scan.remaining_weight_g
                is_low = (remaining is not None and remaining <= cfg["low_spool_threshold"])

                if cfg["toolhead_mode"] == "afc":
                    publish_lock(toolhead, "lock")
                    update_klipper_led(toolhead, color_hex, is_low)
                else:
                    client.publish(f"nfc/toolhead/{toolhead}/color",
                                   color_hex.lstrip("#").upper(), qos=1, retain=True)

                # Low spool handling
                if cfg["toolhead_mode"] == "afc":
                    if is_low:
                        logging.warning(f"Low spool: {scan.material_name or scan.material_type or 'Unknown'} ({remaining:.1f}g) on {toolhead}")
                else:
                    topic_low = f"nfc/toolhead/{toolhead}/low_spool"
                    if is_low:
                        logging.warning(f"Low spool: {scan.material_name or scan.material_type or 'Unknown'} ({remaining:.1f}g) on {toolhead}")
                        client.publish(topic_low, "true", qos=1, retain=True)
                    else:
                        client.publish(topic_low, "false", qos=1, retain=True)
        else:
            logging.warning(f"Rich tag parsed but no Spoolman ID assigned for UID: {scan.uid}")
            # For single/toolchanger, flash error on the ESP32 LED
            if cfg["toolhead_mode"] != "afc":
                client.publish(f"nfc/toolhead/{toolhead}/low_spool", "false", qos=1, retain=True)
                client.publish(f"nfc/toolhead/{toolhead}/color", "error", qos=1, retain=True)

    except NotImplementedError as e:
        logging.warning(f"Tag format not yet supported: {e}")
    except ValueError as e:
        logging.debug(f"Dispatcher rejected payload: {e}")
        # For single/toolchanger, flash error if it was a real scan attempt
        if cfg["toolhead_mode"] != "afc":
            client.publish(f"nfc/toolhead/{toolhead}/low_spool", "false", qos=1, retain=True)
            client.publish(f"nfc/toolhead/{toolhead}/color", "error", qos=1, retain=True)
    except Exception as e:
        logging.error(f"Rich tag processing error: {e}")


def on_message(client, userdata, msg):
    """
    Fires every time an MQTT message arrives on a subscribed topic.
    
    Handles two payload types:
    
    1. Plain UID (PN532/ESPHome) — {"uid": "xx-xx", "toolhead": "T0"}
       Looks up the UID in Spoolman's nfc_id field. This is the original flow.
    
    2. Rich data (openprinttag_scanner / OpenTag3D) — large JSON with tag data.
       Routes through the dispatcher to parse, syncs with Spoolman, then activates.
       The lane is resolved from the MQTT topic via scanner_lane_map.
    """
    try:
        payload = json.loads(msg.payload.decode())
        topic = msg.topic

        # Resolve which lane/toolhead this message belongs to
        toolhead = payload.get("toolhead") or _resolve_lane_from_topic(topic)
        if not toolhead:
            logging.warning(f"Could not resolve lane from topic: {topic}")
            return

        uid = payload.get("uid", "")

        # If the lane is locked (already has a spool), ignore the scan
        if lane_locks.get(toolhead):
            logging.info(f"Ignoring scan on {toolhead} (locked)")
            return

        # Decide which processing path to use:
        # - Rich data path: dispatcher available AND payload has rich-data keys
        # - Plain UID path: just a UID, look it up in Spoolman
        is_rich = False
        if DISPATCHER_AVAILABLE and len(payload) > 3:
            # More than just uid/toolhead/format — likely rich data
            tag_data = {k: v for k, v in payload.items() if k not in ("uid", "toolhead")}
            try:
                fmt = detect_format(tag_data)
                if fmt != "unknown":
                    is_rich = True
            except Exception:
                pass

        if is_rich:
            _handle_rich_tag(client, toolhead, payload, topic)
        else:
            # Original plain-UID path — look up in Spoolman by nfc_id
            if not uid:
                logging.warning(f"Empty UID in payload on {toolhead}")
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

                    if cfg["toolhead_mode"] == "afc":
                        publish_lock(toolhead, "lock")
                        remaining = spool.get("remaining_weight")
                        is_low = (remaining is not None and remaining <= cfg["low_spool_threshold"])
                        update_klipper_led(toolhead, color_hex, is_low)
                    else:
                        client.publish(f"nfc/toolhead/{toolhead}/color",
                                       color_hex.lstrip("#").upper(), qos=1, retain=True)

                    # Low spool warnings
                    remaining = spool.get("remaining_weight")
                    if cfg["toolhead_mode"] == "afc":
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
                if cfg["toolhead_mode"] != "afc":
                    client.publish(f"nfc/toolhead/{toolhead}/low_spool", "false", qos=1, retain=True)
                    client.publish(f"nfc/toolhead/{toolhead}/color", "error", qos=1, retain=True)

    except Exception as e:
        logging.error(f"Message error: {e}")

# ============================================================
# Variable File Watchers (AFC & Klipper)
# ============================================================

def sync_from_klipper_vars():
    """
    Reads Klipper's save_variables.cfg.
    If a user manually changes a spool in the UI, this catches it and updates the ESP32 LEDs.
    """
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
                    # Only update if it actually changed
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
                # Spool was removed in Klipper UI
                logging.info(f"Klipper Sync: {t} cleared")
                mqtt_client.publish(f"nfc/toolhead/{t}/color", "000000", qos=1, retain=True)
                active_spools[t] = None
    except Exception as e:
        logging.error(f"Klipper Sync failed: {e}")

def sync_from_afc_file():
    """
    The core logic for keeping AFC in sync.
    AFC writes its state to a JSON file (AFC.var.unit). We watch that file.
    When AFC changes state (e.g., finishes loading, or user ejects a spool), this runs.
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

                # 1. Save the AFC status so our LED logic knows if it's safe to override
                lane_statuses[lane_name] = status

                if spool_id:
                    # 2. If AFC has a spool, lock our NFC reader so it ignores new scans
                    if not is_locked:
                        logging.info(f"AFC Sync: {lane_name} has spool {spool_id}, locking")
                        publish_lock(lane_name, "lock")

                    if status in AFC_PROTECTED_STATES:
                        continue

                    # 3. LED Override Logic
                    # AFC resets LEDs to default colors on every state change.
                    # We use force=True to re-apply our filament color over top of AFC's default.
                    
                    # Optimization: If the spool hasn't changed, use our cached color 
                    # instead of asking Spoolman for the color again.
                    if active_spools.get(lane_name) == spool_id and not last_led_state.get(lane_name) is None:
                        cached = last_led_state.get(lane_name)
                        if cached:
                            update_klipper_led(lane_name, cached[0], cached[1], force=True)
                        continue

                    # If it's a new spool, fetch the color from Spoolman and apply it
                    spool = get_spool_by_id(spool_id)
                    if spool:
                        color = spool.get("filament", {}).get("color_hex", "FFFFFF") or "FFFFFF"
                        remaining = spool.get("remaining_weight")
                        is_low = (remaining is not None and remaining <= cfg["low_spool_threshold"])
                        active_spools[lane_name] = spool_id
                        update_klipper_led(lane_name, color, is_low, force=True)
                else:
                    # 4. If AFC says the lane is empty, unlock the NFC reader so it can scan again
                    if is_locked:
                        logging.info(f"AFC Sync: {lane_name} empty, clearing")
                        publish_lock(lane_name, "clear")
                    
                    # Turn off the LED
                    if active_spools.get(lane_name):
                        update_klipper_led(lane_name, "000000", False, force=True)
                        active_spools[lane_name] = None
    except Exception as e:
        logging.error(f"AFC Sync failed: {e}")

class VarFileHandler(FileSystemEventHandler):
    """Watches the file system. When Klipper or AFC modifies their save file, it triggers our sync functions."""
    def on_modified(self, event):
        time.sleep(0.5) # Give the OS a half-second to finish writing the file before we read it
        if event.src_path == cfg["afc_var_path"]:
            sync_from_afc_file()
        elif event.src_path == cfg.get("klipper_var_path"):
            sync_from_klipper_vars()

def start_watcher():
    """Hooks the VarFileHandler into the operating system's file watcher."""
    observer = Observer()
    handler = VarFileHandler()

    if cfg["toolhead_mode"] == "afc":
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
# Main Execution Loop
# ============================================================

def on_shutdown(signum, frame):
    """Runs when you hit Ctrl+C or stop the service. Cleans up locks and disconnects."""
    logging.info("Shutting down...")
    if mqtt_client:
        mqtt_client.publish("nfc/middleware/online", "false", qos=1, retain=True)
        if cfg["toolhead_mode"] == "afc":
            for lane in cfg["toolheads"]:
                publish_lock(lane, "clear")
        mqtt_client.disconnect()
    if watcher:
        watcher.stop()
    sys.exit(0)

# Hook up the shutdown signals
signal.signal(signal.SIGTERM, on_shutdown)
signal.signal(signal.SIGINT, on_shutdown)

# Setup MQTT
mqtt_client = mqtt.Client()
if cfg["mqtt"].get("username"):
    mqtt_client.username_pw_set(cfg["mqtt"]["username"], cfg["mqtt"].get("password"))

mqtt_client.on_connect = on_connect
mqtt_client.on_message = on_message
mqtt_client.will_set("nfc/middleware/online", "false", qos=1, retain=True)

logging.info(f"Starting SpoolSense Middleware (Mode: {cfg['toolhead_mode']})")
logging.info(f"Spoolman: {cfg['spoolman_url']}")
logging.info(f"Moonraker: {cfg['moonraker_url']}")
if DISPATCHER_AVAILABLE:
    logging.info("Rich tag dispatcher: enabled (OpenTag3D, openprinttag_scanner)")
else:
    logging.info("Rich tag dispatcher: disabled (adapters/ not found, UID-only mode)")
if cfg["toolhead_mode"] == "afc":
    logging.info(f"Lanes: {', '.join(cfg['toolheads'])}")
    logging.info(f"AFC var file: {cfg['afc_var_path']}")
    logging.info(f"LED macro: {cfg['afc_led_macro']}")
else:
    logging.info(f"Toolheads: {', '.join(cfg['toolheads'])}")
    logging.info(f"Low spool threshold: {cfg['low_spool_threshold']}g")
scanner_map = cfg.get("scanner_lane_map", {})
if scanner_map:
    logging.info(f"Scanner lane map: {json.dumps(scanner_map)}")

# Start the infinite loop
try:
    mqtt_client.connect(cfg["mqtt"]["broker"], cfg["mqtt"]["port"], 60)
    watcher = start_watcher()
    mqtt_client.loop_forever()
except Exception as e:
    logging.error(f"Fatal error: {e}")
    sys.exit(1)