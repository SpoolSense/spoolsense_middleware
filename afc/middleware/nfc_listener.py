#!/usr/bin/env python3
"""
NFC Spoolman Middleware — Unified Edition
==========================================
Listens for NFC tag scans published via MQTT by ESPHome-flashed ESP32 devices.
When a tag is scanned, it looks up the spool in Spoolman by NFC UID, then
updates Klipper/Moonraker so filament usage is tracked automatically.

Supports three toolhead modes (set toolhead_mode in config.yaml):

  single      — Calls SET_ACTIVE_SPOOL directly on every scan.
                Use for single-toolhead printers (one ESP32, one PN532).

  toolchanger — Saves the spool ID per toolhead and publishes the LED color,
                but does NOT call SET_ACTIVE_SPOOL. klipper-toolchanger handles
                activation at each toolchange. Tested on MadMax T0–T3.

  ams         — Calls AFC's SET_SPOOL_ID to register the spool in the correct
                lane. AFC auto-pulls color, material, and weight from Spoolman.
                After a successful scan, locks the scanner on that lane to
                prevent repeated triggers from spool rotation during printing.
                Designed for BoxTurtle, NightOwl, and other AFC-based units.

Configuration is loaded from ~/nfc_spoolman/config.yaml — see config.example.yaml
for a documented template with all available options.
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
# Configuration — loaded from ~/nfc_spoolman/config.yaml
# ============================================================

CONFIG_PATH = os.path.expanduser("~/nfc_spoolman/config.yaml")

# Default values — used when a key is missing from config.yaml.
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

VALID_MODES = ("single", "toolchanger", "ams")

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

    # Validate toolhead_mode
    if config["toolhead_mode"] not in VALID_MODES:
        logging.error(f"Invalid toolhead_mode: '{config['toolhead_mode']}' — must be one of: {', '.join(VALID_MODES)}")
        sys.exit(1)

    config["spoolman_url"] = config["spoolman_url"].rstrip("/")
    config["moonraker_url"] = config["moonraker_url"].rstrip("/")

    return config


# Load config at startup
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

# Track spool assignments per lane for AMS lock/clear management
lane_spools = {}  # lane_name → spool_id

# ============================================================
# Spoolman cache
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


# ============================================================
# Mode-specific spool activation functions
# ============================================================


def activate_spool_single(spool_id, toolhead):
    """
    Single mode — set the globally active spool in Moonraker immediately,
    then persist the spool ID to disk for RESTORE_SPOOL after reboots.
    """
    try:
        response = requests.post(
            f"{MOONRAKER_URL}/server/spoolman/spool_id",
            json={"spool_id": spool_id},
            timeout=5
        )
        response.raise_for_status()
        logging.info(f"[single] Set spool {spool_id} as active via Moonraker")

        var_name = f"t{toolhead[-1]}_spool_id"
        response2 = requests.post(
            f"{MOONRAKER_URL}/printer/gcode/script",
            json={"script": f"SAVE_VARIABLE VARIABLE={var_name} VALUE={spool_id}"},
            timeout=5
        )
        response2.raise_for_status()
        logging.info(f"Saved {var_name}={spool_id} to disk")
        return True

    except Exception as e:
        logging.error(f"[single] Error setting active spool: {e}")
        return False


def activate_spool_toolchanger(spool_id, toolhead):
    """
    Toolchanger mode — update the toolhead macro variable and persist to disk.
    klipper-toolchanger handles SET_ACTIVE_SPOOL at each toolchange.
    """
    try:
        macro = f"T{toolhead[-1]}"
        response = requests.post(
            f"{MOONRAKER_URL}/printer/gcode/script",
            json={"script": f"SET_GCODE_VARIABLE MACRO={macro} VARIABLE=spool_id VALUE={spool_id}"},
            timeout=5
        )
        response.raise_for_status()
        logging.info(f"[toolchanger] Updated {macro} spool_id variable to {spool_id}")

        var_name = f"t{toolhead[-1]}_spool_id"
        response2 = requests.post(
            f"{MOONRAKER_URL}/printer/gcode/script",
            json={"script": f"SAVE_VARIABLE VARIABLE={var_name} VALUE={spool_id}"},
            timeout=5
        )
        response2.raise_for_status()
        logging.info(f"Saved {var_name}={spool_id} to disk")
        return True

    except Exception as e:
        logging.error(f"[toolchanger] Error setting spool: {e}")
        return False


def activate_spool_ams(spool_id, lane):
    """
    AMS mode — register a spool in an AFC lane via SET_SPOOL_ID.
    AFC automatically pulls color, material, and weight from Spoolman.
    """
    try:
        response = requests.post(
            f"{MOONRAKER_URL}/printer/gcode/script",
            json={"script": f"SET_SPOOL_ID LANE={lane} SPOOL_ID={spool_id}"},
            timeout=5
        )
        response.raise_for_status()
        logging.info(f"[ams] Set spool {spool_id} on {lane} via AFC SET_SPOOL_ID")
        return True
    except Exception as e:
        logging.error(f"[ams] Error setting AFC spool: {e}")
        return False


def activate_spool(spool_id, toolhead):
    """Route spool activation to the correct mode handler."""
    if TOOLHEAD_MODE == "single":
        return activate_spool_single(spool_id, toolhead)
    elif TOOLHEAD_MODE == "toolchanger":
        return activate_spool_toolchanger(spool_id, toolhead)
    elif TOOLHEAD_MODE == "ams":
        return activate_spool_ams(spool_id, toolhead)
    else:
        logging.error(f"Unknown toolhead_mode: {TOOLHEAD_MODE}")
        return False


# ============================================================
# MQTT helpers
# ============================================================


def publish_color(client, toolhead, color_hex):
    """Publish the filament color to MQTT so the ESPHome LED can display it."""
    topic = f"nfc/toolhead/{toolhead}/color"
    if color_hex != "error":
        color_hex = color_hex.lstrip("#").upper()
    client.publish(topic, color_hex, qos=1, retain=True)
    logging.info(f"Published color #{color_hex} to {topic}")


def publish_lock(client, lane):
    """Publish a lock command to stop the ESP32 from scanning on this lane."""
    topic = f"nfc/toolhead/{lane}/lock"
    client.publish(topic, "lock", retain=True)
    logging.info(f"Published lock to {topic}")


def publish_clear(client, lane):
    """Publish a clear command to resume scanning on this lane."""
    topic = f"nfc/toolhead/{lane}/lock"
    client.publish(topic, "clear", retain=True)
    logging.info(f"Published clear to {topic}")


# ============================================================
# MQTT callbacks
# ============================================================


def on_connect(client, userdata, flags, rc):
    """Callback for MQTT connection."""
    if rc == 0:
        logging.info(f"Connected to MQTT broker (Mode: {TOOLHEAD_MODE})")
        client.publish("nfc/middleware/online", "true", qos=1, retain=True)
        for t in TOOLHEADS:
            client.subscribe(f"nfc/toolhead/{t}")
        logging.info(f"Subscribed to: {', '.join(TOOLHEADS)}")
        # Initial cache load
        refresh_spool_cache()
    else:
        logging.error(f"MQTT connection failed: {rc}")


def on_message(client, userdata, msg):
    """Callback for received MQTT messages."""
    try:
        payload = json.loads(msg.payload.decode())
        uid = payload.get("uid")
        toolhead = payload.get("toolhead")
        logging.info(f"NFC scan on {toolhead}: UID={uid}")

        spool = find_spool_by_nfc(uid)

        if spool:
            spool_id = spool["id"]
            filament = spool.get("filament", {})
            name = filament.get("name", "Unknown")
            color_hex = filament.get("color_hex", "FFFFFF") or "FFFFFF"
            logging.info(f"Found spool: {name} (ID: {spool_id})")

            success = activate_spool(spool_id, toolhead)

            if success:
                if TOOLHEAD_MODE == "ams":
                    # AMS: track assignment and lock the scanner
                    lane_spools[toolhead] = spool_id
                    publish_lock(client, toolhead)
                else:
                    # Single/toolchanger: publish filament color to ESP32 LED
                    publish_color(client, toolhead, color_hex)

                # Check remaining filament weight — warn if below threshold
                remaining = spool.get("remaining_weight")
                if TOOLHEAD_MODE == "ams":
                    # AMS: log only — AFC manages its own low spool behavior
                    if remaining is not None and remaining <= LOW_SPOOL_THRESHOLD:
                        logging.warning(f"Low spool: {name} has {remaining:.1f}g remaining on {toolhead}")
                else:
                    # Single/toolchanger: publish low_spool status to MQTT
                    topic_low = f"nfc/toolhead/{toolhead}/low_spool"
                    if remaining is not None and remaining <= LOW_SPOOL_THRESHOLD:
                        logging.warning(f"Low spool: {name} ({remaining:.1f}g) on {toolhead}")
                        client.publish(topic_low, "true", qos=1, retain=True)
                    else:
                        client.publish(topic_low, "false", qos=1, retain=True)
        else:
            logging.warning(f"No spool found for UID: {uid}")
            if TOOLHEAD_MODE != "ams":
                # Clear low spool state and flash red
                client.publish(f"nfc/toolhead/{toolhead}/low_spool", "false", qos=1, retain=True)
                publish_color(client, toolhead, "error")

    except Exception as e:
        logging.error(f"Error processing message: {e}")


# ============================================================
# Main — set up MQTT client and start listening
# ============================================================

client = mqtt.Client()
if MQTT_USERNAME and MQTT_PASSWORD:
    client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)

client.will_set("nfc/middleware/online", payload="false", qos=1, retain=True)
client.on_connect = on_connect
client.on_message = on_message


def on_shutdown(signum, frame):
    logging.info("Shutting down...")
    client.publish("nfc/middleware/online", "false", qos=1, retain=True)
    # AMS mode: clear all lane locks so scanners resume on next startup
    if TOOLHEAD_MODE == "ams":
        for lane in TOOLHEADS:
            publish_clear(client, lane)
    client.disconnect()
    sys.exit(0)

signal.signal(signal.SIGTERM, on_shutdown)
signal.signal(signal.SIGINT, on_shutdown)

logging.info(f"Starting NFC Middleware (Mode: {TOOLHEAD_MODE})")
client.connect(MQTT_BROKER, MQTT_PORT, 60)
client.loop_forever()
