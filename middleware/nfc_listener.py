#!/usr/bin/env python3
"""
NFC Spoolman Middleware
=======================
Listens for NFC tag scans published via MQTT by ESPHome-flashed ESP32-S3 devices.
When a tag is scanned, it looks up the spool in Spoolman by NFC UID, then updates
Klipper and Moonraker so filament usage is tracked per toolhead.

Configuration is loaded from ~/nfc_spoolman/config.yaml — see config.example.yaml
for a documented template with all available options.

TOOLHEAD_MODE controls how spool activation works:

  single      — Middleware calls SET_ACTIVE_SPOOL directly on every scan.
                Use this for single-toolhead printers.

  toolchanger — Middleware saves the spool ID per toolhead and publishes the LED
                colour, but does NOT call SET_ACTIVE_SPOOL. klipper-toolchanger
                handles SET_ACTIVE_SPOOL / CLEAR_ACTIVE_SPOOL automatically at
                each toolchange. Tested and confirmed working on MadMax T0–T3.

Flow (toolchanger mode):
  ESP32-S3 scans NFC tag
    → publishes UID to MQTT topic nfc/toolhead/T0 (or T1, T2, T3)
      → this script receives the message
        → looks up UID in Spoolman
          → saves spool ID to Klipper variable + disk
            → publishes filament colour back to ESP32 LED
              → klipper-toolchanger handles SET_ACTIVE_SPOOL at next toolchange

Flow (single mode):
  ESP32-S3 scans NFC tag
    → publishes UID to MQTT topic nfc/toolhead/T0
      → this script receives the message
        → looks up UID in Spoolman
          → calls SET_ACTIVE_SPOOL in Moonraker immediately
            → publishes filament colour back to ESP32 LED
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


def load_config():
    """
    Load and validate configuration from ~/nfc_spoolman/config.yaml.

    Merges user config with DEFAULTS so new config keys added in future
    releases don't break existing installs — only required fields must
    be present.

    Returns:
        dict: The merged configuration.

    Exits:
        If config.yaml is missing, unreadable, or missing required fields.
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

    # Merge MQTT settings — user values override defaults
    mqtt_defaults = DEFAULTS["mqtt"].copy()
    mqtt_user = user_config.get("mqtt", {}) or {}
    mqtt_config = {**mqtt_defaults, **mqtt_user}

    # Build the final merged config
    config = {
        "toolhead_mode": user_config.get("toolhead_mode", DEFAULTS["toolhead_mode"]),
        "toolheads": user_config.get("toolheads", DEFAULTS["toolheads"]),
        "mqtt": mqtt_config,
        "spoolman_url": user_config.get("spoolman_url", DEFAULTS["spoolman_url"]),
        "moonraker_url": user_config.get("moonraker_url", DEFAULTS["moonraker_url"]),
        "low_spool_threshold": user_config.get("low_spool_threshold", DEFAULTS["low_spool_threshold"]),
    }

    # Validate required fields — catch both missing values and unchanged placeholders
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
    if config["toolhead_mode"] not in ("single", "toolchanger"):
        logging.error(f"Invalid toolhead_mode: '{config['toolhead_mode']}' — must be 'single' or 'toolchanger'")
        sys.exit(1)

    # Strip trailing slashes from URLs
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

# ============================================================


def find_spool_by_nfc(uid):
    """
    Look up a spool in Spoolman by its NFC tag UID.

    Spoolman stores the NFC UID in a custom extra field called 'nfc_id'.
    Note: Spoolman internally wraps the value in extra quotes, so we strip
    them before comparing (e.g. '"04-67-EE-A9"' becomes '04-67-EE-A9').

    Args:
        uid (str): The NFC tag UID as scanned by the ESP32-S3, e.g. '04-67-EE-A9-8F-61-80'

    Returns:
        dict: The matching spool object from Spoolman, or None if not found.
    """
    try:
        # Fetch all spools from Spoolman API
        response = requests.get(f"{SPOOLMAN_URL}/api/v1/spool", timeout=5)
        response.raise_for_status()
        spools = response.json()

        # Search through all spools for a matching nfc_id
        for spool in spools:
            extra = spool.get("extra", {})
            # Strip extra quotes Spoolman adds internally, then compare case-insensitively
            nfc_id = extra.get("nfc_id", "").strip('"').lower()
            if nfc_id == uid.lower():
                return spool

        # No matching spool found
        return None
    except Exception as e:
        logging.error(f"Error querying Spoolman: {e}")
        return None


def set_active_spool(spool_id, toolhead):
    """
    Update Klipper with the scanned spool ID and optionally set it as active in Moonraker.

    Behaviour depends on TOOLHEAD_MODE:

    single mode:
        Calls Moonraker's /server/spoolman/spool_id to set the globally active spool,
        then updates the toolhead macro variable and saves to disk.

    toolchanger mode:
        Skips the SET_ACTIVE_SPOOL call — klipper-toolchanger handles
        CLEAR_ACTIVE_SPOOL / SET_ACTIVE_SPOOL automatically at each toolchange.
        Only updates the toolhead macro variable and saves to disk so the correct
        spool ID is available when the next toolchange fires.

    Both modes:
        Updates SET_GCODE_VARIABLE on the toolhead macro so Fluidd/Mainsail can
        display the correct spool per toolhead. Saves the spool ID via SAVE_VARIABLE
        so RESTORE_SPOOL_IDS can restore assignments after a reboot or power cut.

    Args:
        spool_id (int): The Spoolman spool ID to set.
        toolhead (str): The toolhead identifier, e.g. 'T0', 'T1', 'T2', 'T3'.

    Returns:
        bool: True if successful, False if an error occurred.
    """
    try:
        if TOOLHEAD_MODE == "single":
            # Single mode — set the globally active spool in Moonraker immediately
            response = requests.post(
                f"{MOONRAKER_URL}/server/spoolman/spool_id",
                json={"spool_id": spool_id},
                timeout=5
            )
            response.raise_for_status()
            logging.info(f"[single] Set spool {spool_id} as active via Moonraker")
        else:
            # Toolchanger mode — skip SET_ACTIVE_SPOOL, klipper-toolchanger handles it
            logging.info(f"[toolchanger] Skipping SET_ACTIVE_SPOOL — klipper-toolchanger will activate spool {spool_id} at next toolchange")

        # Update the spool_id variable on the specific toolhead macro
        # This is what makes Fluidd/Mainsail show the correct spool per toolhead
        macro = f"T{toolhead[-1]}"  # extracts digit from T0, T1, T2, T3
        response2 = requests.post(
            f"{MOONRAKER_URL}/printer/gcode/script",
            json={"script": f"SET_GCODE_VARIABLE MACRO={macro} VARIABLE=spool_id VALUE={spool_id}"},
            timeout=5
        )
        response2.raise_for_status()
        logging.info(f"Updated {macro} spool_id variable to {spool_id}")

        # Persist the spool ID to disk using Klipper's save_variables system
        # RESTORE_SPOOL_IDS reads these on boot to restore spool assignments
        # after a power cycle without requiring a rescan.
        var_name = f"t{toolhead[-1]}_spool_id"
        response3 = requests.post(
            f"{MOONRAKER_URL}/printer/gcode/script",
            json={"script": f"SAVE_VARIABLE VARIABLE={var_name} VALUE={spool_id}"},
            timeout=5
        )
        response3.raise_for_status()
        logging.info(f"Saved {var_name}={spool_id} to disk for persistence across reboots")
        return True

    except Exception as e:
        logging.error(f"Error setting active spool: {e}")
        return False


def on_connect(client, userdata, flags, rc):
    """
    Callback fired when the MQTT client connects to the broker.

    On successful connection (rc=0), subscribes to the NFC toolhead topics.
    If connection fails, logs the error code for debugging.

    Args:
        client: The MQTT client instance.
        userdata: User-defined data (unused).
        flags: Connection flags from the broker.
        rc (int): Return code — 0 means success, anything else is an error.
    """
    if rc == 0:
        logging.info(f"Connected to MQTT broker (TOOLHEAD_MODE: {TOOLHEAD_MODE})")
        # Announce middleware is online — published here so it only fires once
        # the broker has acknowledged the connection (not just after TCP connect)
        client.publish("nfc/middleware/online", "true", qos=1, retain=True)
        for t in TOOLHEADS:
            client.subscribe(f"nfc/toolhead/{t}")
        logging.info(f"Subscribed to nfc/toolhead/ for {', '.join(TOOLHEADS)}")
    else:
        logging.error(f"MQTT connection failed with code {rc}")


def publish_color(client, toolhead, color_hex):
    """
    Publish the filament colour to MQTT so the ESPHome LED can display it.

    The colour is sourced from Spoolman's filament.color_hex field (e.g. 'FF0000').
    ESPHome subscribes to nfc/toolhead/T0/color and sets the onboard WS2812 LED
    to that colour after the scan flash animation completes.

    Args:
        client: The MQTT client instance.
        toolhead (str): The toolhead identifier, e.g. 'T0'.
        color_hex (str): Hex colour string without '#', e.g. 'FF0000'.
                         Pass 'error' to trigger the red error flash on the ESP32.
    """
    topic = f"nfc/toolhead/{toolhead}/color"
    # Ensure the hex string is clean — no '#' prefix
    if color_hex != "error":
        color_hex = color_hex.lstrip("#").upper()
    client.publish(topic, color_hex)
    logging.info(f"Published colour #{color_hex} to {topic}")


def on_message(client, userdata, msg):
    """
    Callback fired when an MQTT message is received on a subscribed topic.

    Expected payload format (JSON):
        {"uid": "04-67-EE-A9-8F-61-80", "toolhead": "T0"}

    Process:
        1. Parse the JSON payload to extract UID and toolhead.
        2. Look up the UID in Spoolman.
        3. If found, update Klipper spool variable and save to disk.
           In single mode, also calls SET_ACTIVE_SPOOL in Moonraker immediately.
           In toolchanger mode, klipper-toolchanger handles SET_ACTIVE_SPOOL at toolchange.
        4. Publish the filament colour to MQTT so the ESP32 LED updates.
        5. If not found, publish 'error' so the ESP32 flashes red.

    Args:
        client: The MQTT client instance.
        userdata: User-defined data (unused).
        msg: The received MQTT message containing topic and payload.
    """
    try:
        # Decode and parse the JSON payload from ESPHome
        payload = json.loads(msg.payload.decode())
        uid = payload.get("uid")
        toolhead = payload.get("toolhead")
        logging.info(f"NFC scan on {toolhead}: UID={uid}")

        # Look up the scanned UID in Spoolman
        spool = find_spool_by_nfc(uid)

        if spool:
            # Spool found — update Klipper and publish LED colour
            spool_id = spool["id"]
            filament = spool.get("filament", {})
            name = filament.get("name", "Unknown")
            logging.info(f"Found spool: {name} (ID: {spool_id})")
            set_active_spool(spool_id, toolhead)

            # Publish filament colour to MQTT so the ESP32 LED can display it
            # Spoolman stores colour as a hex string in filament.color_hex (e.g. 'FF0000')
            # Fall back to white if no colour is set
            color_hex = filament.get("color_hex", "FFFFFF") or "FFFFFF"
            publish_color(client, toolhead, color_hex)

            # Check remaining filament weight — warn if at or below LOW_SPOOL_THRESHOLD
            remaining = spool.get("remaining_weight")
            topic_low = f"nfc/toolhead/{toolhead}/low_spool"
            if remaining is not None and remaining <= LOW_SPOOL_THRESHOLD:
                logging.warning(f"Low spool warning: {name} has {remaining:.1f}g remaining on {toolhead} (threshold: {LOW_SPOOL_THRESHOLD}g)")
                client.publish(topic_low, "true")
            else:
                client.publish(topic_low, "false")
        else:
            # No spool found — user needs to register this NFC tag in Spoolman
            logging.warning(f"No spool found in Spoolman for UID: {uid}")
            logging.warning("Go to Spoolman and add this UID to a spool's nfc_id field.")
            # Publish error status — ESPHome will flash red to indicate unknown tag
            publish_color(client, toolhead, "error")

    except Exception as e:
        logging.error(f"Error processing message: {e}")


# ============================================================
# Main — set up MQTT client and start listening
# ============================================================

# Create MQTT client instance
client = mqtt.Client()

# Set authentication credentials
client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)

# Register Last Will and Testament — broker publishes this automatically if
# the middleware crashes or loses connection unexpectedly
client.will_set("nfc/middleware/online", payload="false", qos=1, retain=True)

# Register callbacks
client.on_connect = on_connect   # fired on connection
client.on_message = on_message   # fired on each received message


def on_shutdown(signum, frame):
    """Publish offline status cleanly before exiting on SIGTERM or SIGINT."""
    logging.info("Shutting down — publishing offline status")
    client.publish("nfc/middleware/online", "false", qos=1, retain=True)
    client.disconnect()
    sys.exit(0)

signal.signal(signal.SIGTERM, on_shutdown)
signal.signal(signal.SIGINT, on_shutdown)

# Log active config at startup so it's visible in systemd journal
logging.info(f"Starting NFC Spoolman Middleware (TOOLHEAD_MODE: {TOOLHEAD_MODE})")
logging.info(f"Config loaded from {CONFIG_PATH}")
logging.info(f"Toolheads: {', '.join(TOOLHEADS)}")
logging.info(f"Spoolman: {SPOOLMAN_URL}")
logging.info(f"Moonraker: {MOONRAKER_URL}")
logging.info(f"Low spool threshold: {LOW_SPOOL_THRESHOLD}g")

# Connect to the MQTT broker
logging.info(f"Connecting to MQTT broker at {MQTT_BROKER}:{MQTT_PORT}...")
client.connect(MQTT_BROKER, MQTT_PORT, 60)

# Start the blocking network loop — runs forever, processing messages
client.loop_forever()
