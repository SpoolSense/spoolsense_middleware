#!/usr/bin/env python3
"""
NFC Spoolman Middleware
=======================
Listens for NFC tag scans published via MQTT by ESPHome-flashed ESP32-S3 devices.
When a tag is scanned, it looks up the spool in Spoolman by NFC UID, then sets
the active spool in Moonraker so Klipper and Fluidd can track filament usage
per toolhead.

Flow:
  ESP32-S3 scans NFC tag
    → publishes UID to MQTT topic nfc/toolhead/T0 (or T1, T2, T3)
      → this script receives the message
        → looks up UID in Spoolman
          → sets active spool in Moonraker
            → updates Klipper toolhead macro variable
              → Fluidd displays correct spool per toolhead
"""

import paho.mqtt.client as mqtt
import requests
import json
import logging

# Configure logging to show timestamps and log level
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

# ============================================================
# Configuration — update these values for your setup
# ============================================================

# IP address of your Home Assistant server (running Mosquitto MQTT broker)
MQTT_BROKER = "YOUR_HOME_ASSISTANT_IP"       # e.g. "192.168.1.100"

# Default MQTT port — change only if you've configured a custom port
MQTT_PORT = 1883

# Your Home Assistant username and password (used for MQTT authentication)
MQTT_USERNAME = "your_mqtt_username"
MQTT_PASSWORD = "your_mqtt_password"

# MQTT topic to subscribe to — the # wildcard matches T0, T1, T2, T3
MQTT_TOPIC = "nfc/toolhead/#"

# URL of your Spoolman instance (default port is 7912)
SPOOLMAN_URL = "http://YOUR_SPOOLMAN_IP:7912"  # e.g. "http://192.168.1.101:7912"

# URL of your Klipper/Moonraker instance
MOONRAKER_URL = "http://YOUR_KLIPPER_IP"       # e.g. "http://192.168.1.102"

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
    Set the active spool in Moonraker and update the Klipper toolhead macro variable.

    This function does two things:
    1. Calls Moonraker's Spoolman API to set the globally active spool,
       which enables filament usage tracking.
    2. Updates the spool_id variable on the specific toolhead's Klipper macro
       (e.g. T0, T1, T2, T3) so Fluidd can display the correct spool per toolhead.

    Args:
        spool_id (int): The Spoolman spool ID to set as active.
        toolhead (str): The toolhead identifier, e.g. 'T0', 'T1', 'T2', 'T3'.

    Returns:
        bool: True if successful, False if an error occurred.
    """
    try:
        # Step 1: Tell Moonraker which spool is globally active
        # This enables Spoolman filament usage tracking
        response = requests.post(
            f"{MOONRAKER_URL}/server/spoolman/spool_id",
            json={"spool_id": spool_id},
            timeout=5
        )
        response.raise_for_status()
        logging.info(f"Set spool {spool_id} as active on {toolhead} via Moonraker")

        # Step 2: Update the spool_id variable on the specific toolhead macro
        # This is what makes Fluidd show the correct spool per toolhead
        # e.g. toolhead "T0" → macro name "T0"
        macro = f"T{toolhead[-1]}"  # extracts digit from T0, T1, T2, T3
        response2 = requests.post(
            f"{MOONRAKER_URL}/printer/gcode/script",
            json={"script": f"SET_GCODE_VARIABLE MACRO={macro} VARIABLE=spool_id VALUE={spool_id}"},
            timeout=5
        )
        response2.raise_for_status()
        logging.info(f"Updated {macro} spool_id variable to {spool_id}")

        # Step 3: Persist the spool ID to disk using Klipper's save_variables system
        # This ensures the spool ID survives Klipper restarts and power cuts.
        # The RESTORE_SPOOL_IDS delayed_gcode macro reads these values on boot.
        # Variable name: t0_spool_id, t1_spool_id, t2_spool_id, t3_spool_id
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
        logging.info("Connected to MQTT broker")
        # Subscribe to toolhead NFC scan topics only (T0, T1, T2, T3)
        # We use a specific pattern to avoid receiving our own colour messages
        # which are published to nfc/toolhead/T0/color etc.
        for t in ["T0", "T1", "T2", "T3"]:
            client.subscribe(f"nfc/toolhead/{t}")
        logging.info("Subscribed to nfc/toolhead/T0-T3")
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
    """
    topic = f"nfc/toolhead/{toolhead}/color"
    # Ensure the hex string is clean — no '#' prefix
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
        3. If found, set it as the active spool in Moonraker/Klipper.
        4. Publish the filament colour to MQTT so the ESP32 LED updates.
        5. If not found, log a warning — the spool needs to be registered in Spoolman.

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
            # Spool found — set it as active
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

            # Check remaining filament weight — warn if 100g or less
            # Spoolman stores remaining weight in grams under 'remaining_weight'
            remaining = spool.get("remaining_weight")
            topic_low = f"nfc/toolhead/{toolhead}/low_spool"
            if remaining is not None and remaining <= 100:
                logging.warning(f"Low spool warning: {name} has {remaining:.1f}g remaining on {toolhead}")
                client.publish(topic_low, "true")
            else:
                client.publish(topic_low, "false")
        else:
            # No spool found — user needs to register this NFC tag in Spoolman
            logging.warning(f"No spool found in Spoolman for UID: {uid}")
            logging.warning(f"Go to Spoolman and add this UID to a spool's nfc_id field.")
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

# Register callbacks
client.on_connect = on_connect   # fired on connection
client.on_message = on_message   # fired on each received message

# Connect to the MQTT broker
logging.info(f"Connecting to MQTT broker at {MQTT_BROKER}:{MQTT_PORT}...")
client.connect(MQTT_BROKER, MQTT_PORT, 60)

# Start the blocking network loop — runs forever, processing messages
client.loop_forever()
