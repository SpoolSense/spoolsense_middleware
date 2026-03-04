#!/usr/bin/env python3
"""
NFC Spoolman Middleware — Beta (Spool Cache)
============================================
Beta version of nfc_listener.py testing in-memory spool cache to avoid
fetching all spools from Spoolman on every NFC scan.

Changes from production:
  - In-memory UID → spool cache with TTL-based refresh (default 5 minutes)
  - Cache miss triggers a forced refresh before giving up
  - requests.Session() reuses TCP connections to both Spoolman and Moonraker
  - _normalize_uid() centralizes UID sanitization

If this tests well, promote to middleware/nfc_listener.py.

Flow:
  ESP32-S3 scans NFC tag
    → publishes UID to MQTT topic nfc/toolhead/T0 (or T1, T2, T3)
      → this script receives the message
        → looks up UID in cache (refreshes if stale or empty)
          → sets active spool in Moonraker
            → updates Klipper toolhead macro variable
              → Fluidd displays correct spool per toolhead
"""

import paho.mqtt.client as mqtt
import requests
import json
import logging
import signal
import sys
from time import monotonic

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

# Remaining filament threshold in grams — LED will breathe when a spool hits this level or below
# Adjust based on your typical spool sizes (e.g. 50 for mini spools, 200 for cautious early warning)
LOW_SPOOL_THRESHOLD = 100

# ============================================================

# Reuse TCP connections across all Spoolman and Moonraker calls
_session = requests.Session()

# In-memory spool cache: UID → spool dict
# Built on first scan, refreshed periodically or on cache miss
_spool_cache: dict = {}
_cache_refreshed_at: float = 0.0
CACHE_TTL = 300  # seconds — full cache refresh interval (5 minutes)


def _normalize_uid(uid: str) -> str:
    """Strip quotes and normalize case for consistent cache key lookups."""
    return uid.strip('"').lower()


def _refresh_spool_cache() -> None:
    """
    Fetch all spools from Spoolman and rebuild the in-memory UID -> spool cache.
    Handles both paginated and non-paginated Spoolman responses.
    Only spools with an nfc_id extra field are indexed.
    """
    global _spool_cache, _cache_refreshed_at
    spools = []
    page = 1
    while True:
        r = _session.get(
            f"{SPOOLMAN_URL}/api/v1/spool",
            params={"page": page},
            timeout=5
        )
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and "items" in data:
            items = data["items"]
            spools.extend(items)
            if not items or len(items) < data.get("page_size", len(items)):
                break
            page += 1
        else:
            # Non-paginated — plain array response
            spools = data
            break

    cache = {}
    for spool in spools:
        extra = spool.get("extra", {}) or {}
        nfc_id = _normalize_uid(extra.get("nfc_id", ""))
        if nfc_id:
            cache[nfc_id] = spool

    _spool_cache = cache
    _cache_refreshed_at = monotonic()
    logging.info(f"Spool cache refreshed: {len(_spool_cache)} tagged spools")


def find_spool_by_nfc(uid: str) -> dict | None:
    """
    Look up a spool by NFC UID, using the in-memory cache.

    Cache is refreshed if stale (older than CACHE_TTL) or empty.
    On a cache miss, a single forced refresh is attempted to catch
    spools added since the last refresh before giving up.

    Args:
        uid: NFC tag UID as scanned by the ESP32-S3, e.g. '04-67-EE-A9-8F-61-80'

    Returns:
        Matching spool dict from Spoolman, or None if not found.
    """
    # Refresh if cache is empty or stale
    if not _spool_cache or (monotonic() - _cache_refreshed_at) > CACHE_TTL:
        try:
            _refresh_spool_cache()
        except Exception as e:
            logging.error(f"Error refreshing spool cache: {e}")

    key = _normalize_uid(uid)
    spool = _spool_cache.get(key)
    if spool:
        return spool

    # Cache miss — refresh once to catch recently added spools, then retry
    logging.info(f"Cache miss for UID {uid} — refreshing and retrying")
    try:
        _refresh_spool_cache()
        return _spool_cache.get(key)
    except Exception as e:
        logging.error(f"Error refreshing spool cache on miss: {e}")
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
        response = _session.post(
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
        response2 = _session.post(
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
        response3 = _session.post(
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
        # Announce middleware is online — published here so it only fires once
        # the broker has acknowledged the connection (not just after TCP connect)
        client.publish("nfc/middleware/online", "true", qos=1, retain=True)
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

            # Check remaining filament weight — warn if at or below LOW_SPOOL_THRESHOLD
            # Spoolman stores remaining weight in grams under 'remaining_weight'
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

# Connect to the MQTT broker
logging.info(f"Connecting to MQTT broker at {MQTT_BROKER}:{MQTT_PORT}...")
client.connect(MQTT_BROKER, MQTT_PORT, 60)

# Start the blocking network loop — runs forever, processing messages
client.loop_forever()
