#!/usr/bin/env python3
"""
NFC Spoolman Middleware — AFC/BoxTurtle Edition
================================================
Listens for NFC tag scans published via MQTT by an ESP32 driving PN532
readers inside a BoxTurtle AFC unit. When a tag is scanned, it looks up
the spool in Spoolman by NFC UID, then calls AFC's SET_SPOOL_ID to
register the spool in the correct lane. AFC automatically pulls color,
material, and weight from Spoolman — one call does everything.

After a successful scan, the middleware publishes a "lock" command to
the ESP32 to stop scanning on that lane (prevents repeated triggers
from spool rotation during printing). When a lane is ejected, the
middleware publishes "clear" to resume scanning.

Flow:
  Spool placed on respooler → rotates into PN532 read zone
    → ESP32 publishes UID to MQTT topic nfc/toolhead/lane1
      → this script receives the message
        → looks up UID in Spoolman
          → calls SET_SPOOL_ID LANE=lane1 SPOOL_ID=<id> via Moonraker
            → AFC pulls color, material, weight from Spoolman
              → middleware publishes "lock" to ESP32 for that lane
                → ESP32 stops scanning, lane is registered

Configuration is loaded from ~/nfc_spoolman/config.yaml
"""

import paho.mqtt.client as mqtt
import requests
import json
import logging
import signal
import sys
import os
import yaml

# Configure logging to show timestamps and log level
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

# ============================================================
# Configuration — loaded from ~/nfc_spoolman/config.yaml
# ============================================================

CONFIG_PATH = os.path.expanduser("~/nfc_spoolman/config.yaml")

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
}


def load_config():
    """
    Load and validate configuration from ~/nfc_spoolman/config.yaml.
    """
    if not os.path.exists(CONFIG_PATH):
        logging.error(f"Config file not found: {CONFIG_PATH}")
        logging.error("Copy the template to get started:")
        logging.error("  cp config.example.yaml ~/nfc_spoolman/config.yaml")
        sys.exit(1)

    try:
        with open(CONFIG_PATH, "r") as f:
            user_config = yaml.safe_load(f) or {}
    except yaml.YAMLError as e:
        logging.error(f"Failed to parse {CONFIG_PATH}: {e}")
        sys.exit(1)
    except OSError as e:
        logging.error(f"Failed to read {CONFIG_PATH}: {e}")
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

    # Validate required fields
    missing = []
    if not config["mqtt"]["broker"] or config["mqtt"]["broker"] == "YOUR_HOME_ASSISTANT_IP":
        missing.append("mqtt.broker")
    if not config["mqtt"]["username"] or config["mqtt"]["username"] == "your_mqtt_username":
        missing.append("mqtt.username")
    if not config["mqtt"]["password"] or config["mqtt"]["password"] == "your_mqtt_password":
        missing.append("mqtt.password")
    if not config["spoolman_url"] or "YOUR_SPOOLMAN_IP" in str(config["spoolman_url"]):
        missing.append("spoolman_url")
    if not config["moonraker_url"] or "YOUR_KLIPPER_IP" in str(config["moonraker_url"]):
        missing.append("moonraker_url")

    if missing:
        logging.error(f"Missing or unconfigured values in {CONFIG_PATH}:")
        for field in missing:
            logging.error(f"  - {field}")
        logging.error(f"Edit {CONFIG_PATH} and fill in your values.")
        sys.exit(1)

    # Validate toolhead_mode
    if config["toolhead_mode"] != "ams":
        logging.warning(f"toolhead_mode is '{config['toolhead_mode']}' — this middleware is designed for 'ams' mode")
        logging.warning("For single/toolchanger modes, use the main middleware/nfc_listener.py instead")

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

# Track spool assignments per lane for lock/clear management
lane_spools = {}  # lane_name → spool_id

# ============================================================


def find_spool_by_nfc(uid):
    """
    Look up a spool in Spoolman by its NFC tag UID.
    """
    try:
        response = requests.get(f"{SPOOLMAN_URL}/api/v1/spool", timeout=5)
        response.raise_for_status()
        spools = response.json()

        for spool in spools:
            extra = spool.get("extra", {})
            nfc_id = extra.get("nfc_id", "").strip('"').lower()
            if nfc_id == uid.lower():
                return spool

        return None
    except Exception as e:
        logging.error(f"Error querying Spoolman: {e}")
        return None


def set_afc_spool(spool_id, lane):
    """
    Register a spool in an AFC lane via SET_SPOOL_ID.

    AFC automatically pulls color, material, and weight from Spoolman
    when SET_SPOOL_ID is called. One call does everything — no need to
    separately set color, material, or weight.

    Args:
        spool_id (int): The Spoolman spool ID.
        lane (str): The AFC lane name, e.g. 'lane1'.

    Returns:
        bool: True if successful, False if an error occurred.
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
        logging.error(f"Error setting AFC spool: {e}")
        return False


def publish_lock(client, lane):
    """
    Publish a lock command to stop the ESP32 from scanning on this lane.
    Called after a successful spool registration.
    """
    topic = f"nfc/toolhead/{lane}/lock"
    client.publish(topic, "lock", retain=True)
    logging.info(f"Published lock to {topic}")


def publish_clear(client, lane):
    """
    Publish a clear command to resume scanning on this lane.
    Called when a spool is ejected.
    """
    topic = f"nfc/toolhead/{lane}/lock"
    client.publish(topic, "clear", retain=True)
    logging.info(f"Published clear to {topic}")


def on_connect(client, userdata, flags, rc):
    """
    Callback fired when the MQTT client connects to the broker.
    """
    if rc == 0:
        logging.info(f"Connected to MQTT broker (TOOLHEAD_MODE: {TOOLHEAD_MODE})")
        client.publish("nfc/middleware/online", "true", qos=1, retain=True)
        for lane in TOOLHEADS:
            client.subscribe(f"nfc/toolhead/{lane}")
        logging.info(f"Subscribed to nfc/toolhead/ for {', '.join(TOOLHEADS)}")
    else:
        logging.error(f"MQTT connection failed with code {rc}")


def on_message(client, userdata, msg):
    """
    Callback fired when an NFC scan is received from the ESP32.

    Expected payload format (JSON):
        {"uid": "04-67-EE-A9-8F-61-80", "toolhead": "lane1"}

    Process:
        1. Parse the JSON payload to extract UID and lane.
        2. Look up the UID in Spoolman.
        3. If found, call SET_SPOOL_ID in AFC for the lane.
        4. Publish lock command to ESP32 to stop scanning on that lane.
        5. If not found, log a warning.
    """
    try:
        payload = json.loads(msg.payload.decode())
        uid = payload.get("uid")
        lane = payload.get("toolhead")
        logging.info(f"NFC scan on {lane}: UID={uid}")

        spool = find_spool_by_nfc(uid)

        if spool:
            spool_id = spool["id"]
            filament = spool.get("filament", {})
            name = filament.get("name", "Unknown")
            color_hex = filament.get("color_hex", "FFFFFF") or "FFFFFF"
            logging.info(f"Found spool: {name} (ID: {spool_id})")

            # Register spool in AFC — AFC pulls all metadata from Spoolman
            if set_afc_spool(spool_id, lane):
                # Track the assignment
                lane_spools[lane] = spool_id

                # Lock the scanner on this lane — spool is registered
                publish_lock(client, lane)

                # Log remaining weight for awareness
                remaining = spool.get("remaining_weight")
                if remaining is not None and remaining <= LOW_SPOOL_THRESHOLD:
                    logging.warning(f"Low spool: {name} has {remaining:.1f}g remaining on {lane}")
        else:
            logging.warning(f"No spool found in Spoolman for UID: {uid}")
            logging.warning("Go to Spoolman and add this UID to a spool's nfc_id field.")

    except Exception as e:
        logging.error(f"Error processing message: {e}")


# ============================================================
# Main — set up MQTT client and start listening
# ============================================================

client = mqtt.Client()
client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
client.will_set("nfc/middleware/online", payload="false", qos=1, retain=True)

client.on_connect = on_connect
client.on_message = on_message


def on_shutdown(signum, frame):
    """Publish offline status cleanly before exiting on SIGTERM or SIGINT."""
    logging.info("Shutting down — publishing offline status")
    client.publish("nfc/middleware/online", "false", qos=1, retain=True)
    # Clear all lane locks on shutdown so scanners resume on next startup
    for lane in TOOLHEADS:
        publish_clear(client, lane)
    client.disconnect()
    sys.exit(0)

signal.signal(signal.SIGTERM, on_shutdown)
signal.signal(signal.SIGINT, on_shutdown)

# Log active config at startup
logging.info(f"Starting NFC Spoolman Middleware — AFC Edition (TOOLHEAD_MODE: {TOOLHEAD_MODE})")
logging.info(f"Config loaded from {CONFIG_PATH}")
logging.info(f"Lanes: {', '.join(TOOLHEADS)}")
logging.info(f"Spoolman: {SPOOLMAN_URL}")
logging.info(f"Moonraker: {MOONRAKER_URL}")

logging.info(f"Connecting to MQTT broker at {MQTT_BROKER}:{MQTT_PORT}...")
client.connect(MQTT_BROKER, MQTT_PORT, 60)

client.loop_forever()
