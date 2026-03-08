#!/usr/bin/env python3
"""
NFC Spoolman Middleware — Unified Edition with AFC & Klipper Sync
=================================================================
Listens for NFC tag scans via MQTT and updates Klipper/Spoolman.
Includes automatic "Sync/Clear" logic by watching variable files.
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
import threading
import configparser
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

# ============================================================
CONFIG_PATH = os.path.expanduser("~/nfc_spoolman/config.yaml")

DEFAULTS = {
    "toolhead_mode": "ams",
    "toolheads": ["lane1", "lane2", "lane3", "lane4"],
    "mqtt": {
        "broker": "localhost",
        "port": 1883,
        "username": None,
        "password": None,
    },
    "spoolman_url": "http://localhost:7912",
    "moonraker_url": "http://localhost:7125",
    "low_spool_threshold": 100,
    "afc_var_path": "~/printer_data/config/AFC/AFC.var.unit",
    "klipper_var_path": None, # Will be auto-discovered
}

VALID_MODES = ("single", "toolchanger", "ams")

# Global state
spool_cache = {}
last_cache_refresh = 0
CACHE_TTL = 3600
lane_locks = {} # lane_name -> bool (True if locked)
active_spools = {} # toolhead -> spool_id (for single/toolchanger sync)
mqtt_client = None
watcher = None

def load_config():
    """Load and validate configuration."""
    if not os.path.exists(CONFIG_PATH):
        logging.warning(f"Config not found at {CONFIG_PATH}, using defaults")
        return DEFAULTS

    try:
        with open(CONFIG_PATH, "r") as f:
            user_config = yaml.safe_load(f) or {}
    except Exception as e:
        logging.error(f"Failed to parse config: {e}")
        return DEFAULTS

    mqtt_cfg = {**DEFAULTS["mqtt"], **user_config.get("mqtt", {})}
    config = {**DEFAULTS, **user_config}
    config["mqtt"] = mqtt_cfg
    config["afc_var_path"] = os.path.expanduser(config["afc_var_path"])
    if config["klipper_var_path"]:
        config["klipper_var_path"] = os.path.expanduser(config["klipper_var_path"])
    
    return config

cfg = load_config()

# ============================================================
# Discovery Helpers
# ============================================================

def discover_klipper_var_path():
    """Query Moonraker to find the actual save_variables file path."""
    if cfg["klipper_var_path"]:
        return cfg["klipper_var_path"]

    try:
        logging.info("Discovering Klipper save_variables path...")
        response = requests.get(f"{cfg['moonraker_url']}/printer/configfile/settings", timeout=5)
        response.raise_for_status()
        settings = response.json().get("result", {}).get("settings", {})
        
        save_vars = settings.get("save_variables", {})
        filename = save_vars.get("filename")
        
        if not filename:
            logging.warning("No [save_variables] section found in Klipper config. Klipper sync disabled.")
            return None

        # If it's a relative path, it's usually in the config folder
        if not filename.startswith("/"):
            config_dir = os.path.expanduser("~/printer_data/config")
            filename = os.path.join(config_dir, filename)
        
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
    uid_lower = uid.lower()
    if time.time() - last_cache_refresh > CACHE_TTL:
        refresh_spool_cache()
    if uid_lower in spool_cache:
        return spool_cache[uid_lower]
    if refresh_spool_cache():
        return spool_cache.get(uid_lower)
    return None

# ============================================================
# Klipper/Moonraker Actions
# ============================================================

def activate_spool(spool_id, toolhead):
    mode = cfg["toolhead_mode"]
    try:
        if mode == "single":
            requests.post(f"{cfg['moonraker_url']}/server/spoolman/spool_id", json={"spool_id": spool_id}, timeout=5)
            requests.post(f"{cfg['moonraker_url']}/printer/gcode/script", json={"script": f"SAVE_VARIABLE VARIABLE=t0_spool_id VALUE={spool_id}"}, timeout=5)
            logging.info(f"[single] Activated spool {spool_id}")
        elif mode == "toolchanger":
            macro = f"T{toolhead[-1]}"
            requests.post(f"{cfg['moonraker_url']}/printer/gcode/script", json={"script": f"SET_GCODE_VARIABLE MACRO={macro} VARIABLE=spool_id VALUE={spool_id}"}, timeout=5)
            requests.post(f"{cfg['moonraker_url']}/printer/gcode/script", json={"script": f"SAVE_VARIABLE VARIABLE=t{toolhead[-1]}_spool_id VALUE={spool_id}"}, timeout=5)
            logging.info(f"[toolchanger] Updated {macro} with spool {spool_id}")
        elif mode == "ams":
            requests.post(f"{cfg['moonraker_url']}/printer/gcode/script", json={"script": f"SET_SPOOL_ID LANE={toolhead} SPOOL_ID={spool_id}"}, timeout=5)
            logging.info(f"[ams] Set spool {spool_id} on {toolhead} via AFC")
        return True
    except Exception as e:
        logging.error(f"Activation failed: {e}")
        return False

# ============================================================
# MQTT Logic
# ============================================================

def publish_lock(lane, state):
    """state: 'lock' or 'clear'"""
    if not mqtt_client: return
    topic = f"nfc/toolhead/{lane}/lock"
    mqtt_client.publish(topic, state, retain=True)
    lane_locks[lane] = (state == "lock")
    logging.info(f"MQTT: {lane} -> {state}")

def on_connect(client, userdata, flags, rc):
    if rc == 0:
        logging.info(f"Connected to MQTT broker (Mode: {cfg['toolhead_mode']})")
        client.publish("nfc/middleware/online", "true", retain=True)
        for t in cfg["toolheads"]:
            client.subscribe(f"nfc/toolhead/{t}")
        refresh_spool_cache()
        
        # Discovery and Initial sync
        if cfg["toolhead_mode"] == "ams":
            sync_from_afc_file()
        else:
            cfg["klipper_var_path"] = discover_klipper_var_path()
            sync_from_klipper_vars()
            # Restart watcher if path was discovered after startup
            global watcher
            if watcher:
                watcher.stop()
                watcher.join()
            watcher = start_watcher()
    else:
        logging.error(f"MQTT Connection failed: {rc}")

def on_message(client, userdata, msg):
    try:
        payload = json.loads(msg.payload.decode())
        uid = payload.get("uid")
        toolhead = payload.get("toolhead")
        
        if lane_locks.get(toolhead):
            logging.info(f"Ignoring scan on {toolhead} (lane is locked)")
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
                
                # Mode-specific feedback
                if cfg["toolhead_mode"] == "ams":
                    publish_lock(toolhead, "lock")
                else:
                    client.publish(f"nfc/toolhead/{toolhead}/color", color_hex.lstrip("#").upper(), retain=True)

                # Low spool check
                remaining = spool.get("remaining_weight")
                if cfg["toolhead_mode"] == "ams":
                    # AMS: log only — AFC manages its own low spool behavior
                    if remaining is not None and remaining <= cfg["low_spool_threshold"]:
                        logging.warning(f"Low spool: {name} has {remaining:.1f}g remaining on {toolhead}")
                else:
                    # Single/toolchanger: publish low_spool status to MQTT
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
                client.publish(f"nfc/toolhead/{toolhead}/color", "error", retain=True)
    except Exception as e:
        logging.error(f"Message error: {e}")

# ============================================================
# Variable Watchers (AFC & Klipper)
# ============================================================

def sync_from_klipper_vars():
    """Read Klipper's variables file and update LEDs for single/toolchanger modes."""
    path = cfg["klipper_var_path"]
    if not path or not os.path.exists(path):
        return

    try:
        config = configparser.ConfigParser()
        config.read(path)
        if 'variables' not in config: return

        for t in cfg["toolheads"]:
            var_name = f"t{t[-1]}_spool_id"
            spool_id_str = config['variables'].get(var_name, None)
            
            if spool_id_str:
                try:
                    spool_id = int(spool_id_str)
                    if active_spools.get(t) != spool_id:
                        logging.info(f"Klipper Sync: {t} has spool {spool_id}, updating LED.")
                        spool = get_spool_by_id(spool_id)
                        if spool:
                            color = spool.get("filament", {}).get("color_hex", "FFFFFF") or "FFFFFF"
                            mqtt_client.publish(f"nfc/toolhead/{t}/color", color.lstrip("#").upper(), retain=True)
                            active_spools[t] = spool_id
                except ValueError:
                    pass
            elif active_spools.get(t):
                logging.info(f"Klipper Sync: {t} spool cleared, resetting LED.")
                mqtt_client.publish(f"nfc/toolhead/{t}/color", "000000", retain=True)
                active_spools[t] = None
    except Exception as e:
        logging.error(f"Klipper Sync failed: {e}")

def sync_from_afc_file():
    """Read the AFC variable file and update locks based on spool presence."""
    path = cfg["afc_var_path"]
    if not os.path.exists(path):
        logging.warning(f"AFC var file not found at {path}")
        return

    try:
        with open(path, "r") as f:
            data = json.load(f)
        for unit_name, unit_data in data.items():
            if unit_name == "system": continue
            for lane_name, lane_data in unit_data.items():
                spool_id = lane_data.get("spool_id")
                is_locked = lane_locks.get(lane_name, False)
                if spool_id and not is_locked:
                    logging.info(f"AFC Sync: {lane_name} has spool {spool_id}, locking scanner.")
                    publish_lock(lane_name, "lock")
                elif not spool_id and is_locked:
                    logging.info(f"AFC Sync: {lane_name} is empty, clearing scanner.")
                    publish_lock(lane_name, "clear")
    except Exception as e:
        logging.error(f"AFC Sync failed: {e}")

class VarFileHandler(FileSystemEventHandler):
    def on_modified(self, event):
        time.sleep(0.5)
        if event.src_path == cfg["afc_var_path"]:
            sync_from_afc_file()
        elif event.src_path == cfg["klipper_var_path"]:
            sync_from_klipper_vars()

def start_watcher():
    afc_dir = os.path.dirname(cfg["afc_var_path"])
    observer = Observer()
    handler = VarFileHandler()
    
    if os.path.exists(afc_dir) and cfg["toolhead_mode"] == "ams":
        observer.schedule(handler, afc_dir, recursive=False)
        logging.info(f"Watching for AFC changes in {afc_dir}")
    
    if cfg["klipper_var_path"] and cfg["toolhead_mode"] != "ams":
        klipper_dir = os.path.dirname(cfg["klipper_var_path"])
        if os.path.exists(klipper_dir):
            observer.schedule(handler, klipper_dir, recursive=False)
            logging.info(f"Watching for Klipper changes in {klipper_dir}")
    
    observer.start()
    return observer

# ============================================================
# Main Loop
# ============================================================

def on_shutdown(signum, frame):
    logging.info("Shutting down...")
    if mqtt_client:
        mqtt_client.publish("nfc/middleware/online", "false", retain=True)
        # AMS mode: clear all lane locks so scanners are in a known state
        if cfg["toolhead_mode"] == "ams":
            for lane in cfg["toolheads"]:
                publish_lock(lane, "clear")
        mqtt_client.disconnect()
    
    if watcher:
        watcher.stop()
        # No join() here to avoid blocking shutdown if the thread is stuck, 
        # but stop() signals the thread to exit.
    
    sys.exit(0)

signal.signal(signal.SIGTERM, on_shutdown)
signal.signal(signal.SIGINT, on_shutdown)

mqtt_client = mqtt.Client()
if cfg["mqtt"]["username"]:
    mqtt_client.username_pw_set(cfg["mqtt"]["username"], cfg["mqtt"]["password"])

mqtt_client.on_connect = on_connect
mqtt_client.on_message = on_message
mqtt_client.will_set("nfc/middleware/online", "false", retain=True)

try:
    mqtt_client.connect(cfg["mqtt"]["broker"], cfg["mqtt"]["port"], 60)
    watcher = start_watcher()
    mqtt_client.loop_forever()
except Exception as e:
    logging.error(f"Main loop error: {e}")
    sys.exit(1)