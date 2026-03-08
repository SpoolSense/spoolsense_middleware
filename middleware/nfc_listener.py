#!/usr/bin/env python3
"""
NFC Spoolman Middleware
================================================
Listens for NFC tag scans published via MQTT by ESPHome-flashed ESP32-S3 devices.
When a tag is scanned, it looks up the spool in Spoolman by NFC UID, then updates
Klipper and Moonraker so filament usage is tracked per toolhead.
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

# Configure logging to show timestamps and log level
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

# ============================================================
CONFIG_PATH = os.path.expanduser("~/nfc_spoolman/config.yaml")

# Required fields default to None and are validated after loading.
DEFAULTS = {
    "toolhead_mode": "toolchanger",
    "toolheads": ["T0", "T1", "T2", "T3"],
    "mqtt": {
        "broker": None,
        "port": 1883,
        "username": None,
        "password": None,
    },
    "spoolman_url": None,
    "moonraker_url": None,
    "low_spool_threshold": 100,
}

# Global cache for Spoolman data
spool_cache = {}
last_cache_refresh = 0
CACHE_TTL = 3600  # Refresh cache every hour

def load_config():
    """Load and validate configuration from ~/nfc_spoolman/config.yaml."""
    if not os.path.exists(CONFIG_PATH):
        logging.error(f"Config file not found: {CONFIG_PATH}")
        sys.exit(1)

    try:
        with open(CONFIG_PATH, "r") as f:
            user_config = yaml.safe_load(f) or {}
    except Exception as e:
        logging.error(f"Failed to read/parse {CONFIG_PATH}: {e}")
        sys.exit(1)

    mqtt_defaults = DEFAULTS["mqtt"].copy()
    mqtt_user = user_config.get("mqtt", {}) or {}
    mqtt_config = {**mqtt_defaults, **mqtt_user}

    config = {
        "toolhead_mode": user_config.get("toolhead_mode", DEFAULTS["toolhead_mode"]),
        "toolheads": user_config.get("toolheads", DEFAULTS["toolheads"]),
        "mqtt": mqtt_config,
        "spoolman_url": user_config.get("spoolman_url", DEFAULTS["spoolman_url"]),
        "moonraker_url": user_config.get("moonraker_url", DEFAULTS["moonraker_url"]),
        "low_spool_threshold": user_config.get("low_spool_threshold", DEFAULTS["low_spool_threshold"]),
    }

    # Basic validation
    missing = []
    if not config["mqtt"]["broker"]: missing.append("mqtt.broker")
    if not config["spoolman_url"]: missing.append("spoolman_url")
    if not config["moonraker_url"]: missing.append("moonraker_url")

    if missing:
        logging.error(f"Missing required values in {CONFIG_PATH}: {', '.join(missing)}")
        sys.exit(1)

    config["spoolman_url"] = config["spoolman_url"].rstrip("/")
    config["moonraker_url"] = config["moonraker_url"].rstrip("/")

    return config

# Load config
cfg = load_config()
TOOLHEAD_MODE = cfg["toolhead_mode"]
TOOLHEADS = cfg["toolheads"]
MQTT_BROKER = cfg["mqtt"]["broker"]
MQTT_PORT = cfg["mqtt"]["port"]
MQTT_USERNAME = cfg["mqtt"]["username"]
MQTT_PASSWORD = cfg["mqtt"]["password"]
SPOOLMAN_URL = cfg["spoolman_url"]
MOONRAKER_URL = cfg["moonraker_url"]
LOW_SPOOL_THRESHOLD = cfg["low_spool_threshold"]

# ============================================================

def refresh_spool_cache():
    """Fetch all spools from Spoolman and update the local cache."""
    global spool_cache, last_cache_refresh
    try:
        logging.info("Refreshing Spoolman cache...")
        response = requests.get(f"{SPOOLMAN_URL}/api/v1/spool", timeout=5)
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
    """Look up a spool in the local cache by its NFC tag UID."""
    uid_lower = uid.lower()
    now = time.time()

    if now - last_cache_refresh > CACHE_TTL:
        refresh_spool_cache()

    if uid_lower in spool_cache:
        return spool_cache[uid_lower]

    logging.info(f"UID {uid} not in cache, performing forced refresh...")
    if refresh_spool_cache():
        return spool_cache.get(uid_lower)

    return None

def set_active_spool(spool_id, toolhead):
    """Update Klipper/Moonraker with the scanned spool ID."""
    try:
        if TOOLHEAD_MODE == "single":
            response = requests.post(
                f"{MOONRAKER_URL}/server/spoolman/spool_id",
                json={"spool_id": spool_id},
                timeout=5
            )
            response.raise_for_status()
            logging.info(f"[single] Set spool {spool_id} as active via Moonraker")
        
        if TOOLHEAD_MODE == "toolchanger":
            macro = f"T{toolhead[-1]}"
            response2 = requests.post(
                f"{MOONRAKER_URL}/printer/gcode/script",
                json={"script": f"SET_GCODE_VARIABLE MACRO={macro} VARIABLE=spool_id VALUE={spool_id}"},
                timeout=5
            )
            response2.raise_for_status()
            logging.info(f"Updated {macro} spool_id variable to {spool_id}")

        var_name = f"t{toolhead[-1]}_spool_id"
        response3 = requests.post(
            f"{MOONRAKER_URL}/printer/gcode/script",
            json={"script": f"SAVE_VARIABLE VARIABLE={var_name} VALUE={spool_id}"},
            timeout=5
        )
        response3.raise_for_status()
        logging.info(f"Saved {var_name}={spool_id} to disk")
        return True
    except Exception as e:
        logging.error(f"Error setting active spool: {e}")
        return False

def publish_color(client, toolhead, color_hex):
    """Publish the filament color to MQTT."""
    topic = f"nfc/toolhead/{toolhead}/color"
    if color_hex != "error":
        color_hex = color_hex.lstrip("#").upper()
    client.publish(topic, color_hex, qos=1, retain=True)
    logging.info(f"Published color #{color_hex} to {topic}")

def on_connect(client, userdata, flags, rc):
    """Callback for MQTT connection."""
    if rc == 0:
        logging.info(f"Connected to MQTT broker (Mode: {TOOLHEAD_MODE})")
        client.publish("nfc/middleware/online", "true", qos=1, retain=True)
        for t in TOOLHEADS:
            client.subscribe(f"nfc/toolhead/{t}")
        
        logging.info(f"Subscribed to toolheads: {', '.join(TOOLHEADS)}")
        # Initial cache load
        refresh_spool_cache()
    else:
        logging.error(f"MQTT connection failed: {rc}")

def on_message(client, userdata, msg):
    """Callback for received MQTT messages."""
    try:
        # Decode and parse the JSON payload from ESPHome
        payload = json.loads(msg.payload.decode())
        uid = payload.get("uid")
        toolhead = payload.get("toolhead")
        logging.info(f"NFC scan on {toolhead}: UID={uid}")

        spool = find_spool_by_nfc(uid)

        if spool:
            spool_id = spool["id"]
            filament = spool.get("filament", {})
            name = filament.get("name", "Unknown")
            logging.info(f"Found spool: {name} (ID: {spool_id})")
            
            set_active_spool(spool_id, toolhead)

            color_hex = filament.get("color_hex", "FFFFFF") or "FFFFFF"
            publish_color(client, toolhead, color_hex)

            remaining = spool.get("remaining_weight")
            topic_low = f"nfc/toolhead/{toolhead}/low_spool"
            if remaining is not None and remaining <= LOW_SPOOL_THRESHOLD:
                logging.warning(f"Low spool: {name} ({remaining:.1f}g) on {toolhead}")
                client.publish(topic_low, "true", qos=1, retain=True)
            else:
                client.publish(topic_low, "false", qos=1, retain=True)
        else:
            logging.warning(f"No spool found for UID: {uid}")
            # Clear low spool state and flash red
            client.publish(f"nfc/toolhead/{toolhead}/low_spool", "false", qos=1, retain=True)
            publish_color(client, toolhead, "error")

    except Exception as e:
        logging.error(f"Error processing message: {e}")

# MQTT Setup
client = mqtt.Client()
if MQTT_USERNAME and MQTT_PASSWORD:
    client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)

client.will_set("nfc/middleware/online", payload="false", qos=1, retain=True)
client.on_connect = on_connect
client.on_message = on_message

def on_shutdown(signum, frame):
    logging.info("Shutting down...")
    client.publish("nfc/middleware/online", "false", qos=1, retain=True)
    client.disconnect()
    sys.exit(0)

signal.signal(signal.SIGTERM, on_shutdown)
signal.signal(signal.SIGINT, on_shutdown)

logging.info(f"Starting NFC Middleware (Mode: {TOOLHEAD_MODE})")
client.connect(MQTT_BROKER, MQTT_PORT, 60)
client.loop_forever()
