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
    if config["toolhead_mode"] not in VALID_MODES:
        logging.error(f"Invalid toolhead_mode: '{config['toolhead_mode']}' — must be one of: {', '.join(VALID_MODES)}")
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

# Track spool assignments per lane for AMS lock/clear management
lane_spools = {}  # lane_name → spool_id

# ============================================================


def find_spool_by_nfc(uid):
    """
    Look up a spool in Spoolman by its NFC tag UID.

    Spoolman stores the NFC UID in a custom extra field called 'nfc_id'.
    Note: Spoolman internally wraps the value in extra quotes, so we strip
    them before comparing (e.g. '"04-67-EE-A9"' becomes '04-67-EE-A9').

    Args:
        uid (str): The NFC tag UID as scanned by the ESP32, e.g. '04-67-EE-A9-8F-61-80'

    Returns:
        dict: The matching spool object from Spoolman, or None if not found.
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


# ============================================================
# Mode-specific spool activation functions
# ============================================================


def activate_spool_single(spool_id, toolhead):
    """
    Single mode — set the globally active spool in Moonraker immediately,
    then persist the spool ID to disk for RESTORE_SPOOL after reboots.

    Does NOT call SET_GCODE_VARIABLE — single toolhead printers don't have
    T0-T3 gcode macros, so that call would error.

    Args:
        spool_id (int): The Spoolman spool ID.
        toolhead (str): The toolhead identifier, e.g. 'T0'.

    Returns:
        bool: True if successful, False on error.
    """
    try:
        # Set globally active spool in Moonraker
        response = requests.post(
            f"{MOONRAKER_URL}/server/spoolman/spool_id",
            json={"spool_id": spool_id},
            timeout=5
        )
        response.raise_for_status()
        logging.info(f"[single] Set spool {spool_id} as active via Moonraker")

        # Persist spool ID to disk for RESTORE_SPOOL macro
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

    Does NOT call SET_ACTIVE_SPOOL — klipper-toolchanger handles that
    automatically at each toolchange. Updates SET_GCODE_VARIABLE so
    Fluidd/Mainsail can display the correct spool per toolhead.

    Args:
        spool_id (int): The Spoolman spool ID.
        toolhead (str): The toolhead identifier, e.g. 'T0'.

    Returns:
        bool: True if successful, False on error.
    """
    try:
        # Update the toolhead macro variable for Fluidd/Mainsail display
        macro = f"T{toolhead[-1]}"
        response = requests.post(
            f"{MOONRAKER_URL}/printer/gcode/script",
            json={"script": f"SET_GCODE_VARIABLE MACRO={macro} VARIABLE=spool_id VALUE={spool_id}"},
            timeout=5
        )
        response.raise_for_status()
        logging.info(f"[toolchanger] Updated {macro} spool_id variable to {spool_id}")

        # Persist spool ID to disk for RESTORE_SPOOL_IDS after reboot
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

    AFC automatically pulls color, material, and weight from Spoolman
    when SET_SPOOL_ID is called. One call does everything — no need to
    separately set color, material, or weight.

    Args:
        spool_id (int): The Spoolman spool ID.
        lane (str): The AFC lane name, e.g. 'lane1'.

    Returns:
        bool: True if successful, False on error.
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
    """
    Route spool activation to the correct mode handler.

    Args:
        spool_id (int): The Spoolman spool ID.
        toolhead (str): The toolhead/lane identifier.

    Returns:
        bool: True if successful, False on error.
    """
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
    """
    Publish the filament color to MQTT so the ESPHome LED can display it.

    Published with retain=True so the broker remembers the last color per
    toolhead. If the ESP32 reconnects (power cycle, wifi drop) or Klipper
    restarts, the LED will restore to the correct color automatically.

    Used in single and toolchanger modes. AMS mode skips this — AFC manages
    LED colors via its own led_ready/led_tool_loaded states.

    Args:
        client: The MQTT client instance.
        toolhead (str): The toolhead identifier, e.g. 'T0'.
        color_hex (str): Hex color string without '#', e.g. 'FF0000'.
                         Pass 'error' to trigger the red error flash on the ESP32.
    """
    topic = f"nfc/toolhead/{toolhead}/color"
    if color_hex != "error":
        color_hex = color_hex.lstrip("#").upper()
    client.publish(topic, color_hex, retain=True)
    logging.info(f"Published color #{color_hex} to {topic} (retained)")


def publish_lock(client, lane):
    """
    Publish a lock command to stop the ESP32 from scanning on this lane.
    Called after a successful AMS spool registration.

    Args:
        client: The MQTT client instance.
        lane (str): The AFC lane name, e.g. 'lane1'.
    """
    topic = f"nfc/toolhead/{lane}/lock"
    client.publish(topic, "lock", retain=True)
    logging.info(f"Published lock to {topic}")


def publish_clear(client, lane):
    """
    Publish a clear command to resume scanning on this lane.
    Called when a spool is ejected or on middleware shutdown.

    Args:
        client: The MQTT client instance.
        lane (str): The AFC lane name, e.g. 'lane1'.
    """
    topic = f"nfc/toolhead/{lane}/lock"
    client.publish(topic, "clear", retain=True)
    logging.info(f"Published clear to {topic}")


# ============================================================
# MQTT callbacks
# ============================================================


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
        client.publish("nfc/middleware/online", "true", qos=1, retain=True)
        for t in TOOLHEADS:
            client.subscribe(f"nfc/toolhead/{t}")
        logging.info(f"Subscribed to nfc/toolhead/ for {', '.join(TOOLHEADS)}")
    else:
        logging.error(f"MQTT connection failed with code {rc}")


def on_message(client, userdata, msg):
    """
    Callback fired when an MQTT message is received on a subscribed topic.

    Expected payload format (JSON):
        {"uid": "04-67-EE-A9-8F-61-80", "toolhead": "T0"}

    The "toolhead" field is the toolhead name (T0-T3) or lane name (lane1-lane4)
    depending on the mode.

    Process:
        1. Parse the JSON payload to extract UID and toolhead/lane.
        2. Look up the UID in Spoolman.
        3. Activate the spool via the mode-specific handler.
        4. Single/toolchanger: publish filament color to MQTT for ESP32 LED.
           AMS: publish lock command to stop scanning on that lane.
        5. Check remaining weight and publish low_spool warning if needed.
        6. If not found, publish error (single/toolchanger) or log warning (AMS).

    Args:
        client: The MQTT client instance.
        userdata: User-defined data (unused).
        msg: The received MQTT message containing topic and payload.
    """
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

            # Activate the spool via mode-specific handler
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
                        logging.warning(f"Low spool warning: {name} has {remaining:.1f}g remaining on {toolhead} (threshold: {LOW_SPOOL_THRESHOLD}g)")
                        client.publish(topic_low, "true", retain=True)
                    else:
                        client.publish(topic_low, "false", retain=True)
        else:
            logging.warning(f"No spool found in Spoolman for UID: {uid}")
            logging.warning("Go to Spoolman and add this UID to a spool's nfc_id field.")
            # Single/toolchanger: publish error so ESP32 flashes red
            if TOOLHEAD_MODE != "ams":
                publish_color(client, toolhead, "error")

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
    # AMS mode: clear all lane locks so scanners resume on next startup
    if TOOLHEAD_MODE == "ams":
        for lane in TOOLHEADS:
            publish_clear(client, lane)
    client.disconnect()
    sys.exit(0)

signal.signal(signal.SIGTERM, on_shutdown)
signal.signal(signal.SIGINT, on_shutdown)

# Log active config at startup so it's visible in systemd journal
logging.info(f"Starting NFC Spoolman Middleware (TOOLHEAD_MODE: {TOOLHEAD_MODE})")
logging.info(f"Config loaded from {CONFIG_PATH}")
if TOOLHEAD_MODE == "ams":
    logging.info(f"Lanes: {', '.join(TOOLHEADS)}")
else:
    logging.info(f"Toolheads: {', '.join(TOOLHEADS)}")
logging.info(f"Spoolman: {SPOOLMAN_URL}")
logging.info(f"Moonraker: {MOONRAKER_URL}")
if TOOLHEAD_MODE != "ams":
    logging.info(f"Low spool threshold: {LOW_SPOOL_THRESHOLD}g")

# Connect to the MQTT broker
logging.info(f"Connecting to MQTT broker at {MQTT_BROKER}:{MQTT_PORT}...")
client.connect(MQTT_BROKER, MQTT_PORT, 60)

# Start the blocking network loop — runs forever, processing messages
client.loop_forever()
