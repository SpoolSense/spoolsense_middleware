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
    from tag_sync.policy import build_write_plan
    from tag_sync import scanner_writer
    DISPATCHER_AVAILABLE = True
except ImportError:
    DISPATCHER_AVAILABLE = False

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

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

# SpoolmanClient is initialized after cfg is loaded (see below load_config() call)


def load_config():
    """Load and validate configuration from ~/SpoolSense/config.yaml."""
    if not os.path.exists(CONFIG_PATH):
        logger.error(f"Config file not found: {CONFIG_PATH}")
        logger.error("Copy the template:  cp config.example.yaml ~/SpoolSense/config.yaml")
        sys.exit(1)

    try:
        with open(CONFIG_PATH, "r") as f:
            user_config = yaml.safe_load(f) or {}
    except Exception as e:
        logger.error(f"Failed to read/parse {CONFIG_PATH}: {e}")
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
    if not config["moonraker_url"]: missing.append("moonraker_url")

    if missing:
        logger.error(f"Missing required values in {CONFIG_PATH}: {', '.join(missing)}")
        sys.exit(1)

    if config["toolhead_mode"] not in VALID_MODES:
        logger.error(f"Invalid toolhead_mode: '{config['toolhead_mode']}' — must be one of: {', '.join(VALID_MODES)}")
        sys.exit(1)

    # spoolman_url is optional — missing means tag-only mode
    if config["spoolman_url"]:
        config["spoolman_url"] = config["spoolman_url"].rstrip("/")
    else:
        logger.warning(
            "spoolman_url not set — running in tag-only mode. "
            "Spoolman lookup, spool creation, and weight sync are disabled."
        )

    config["moonraker_url"] = config["moonraker_url"].rstrip("/")

    # Validate scanner_lane_map entries against toolheads list
    scanner_map = config.get("scanner_lane_map", {})
    if scanner_map:
        toolheads = set(config.get("toolheads", []))
        bad_lanes = [
            f"{device_id!r} → {lane!r}"
            for device_id, lane in scanner_map.items()
            if lane not in toolheads
        ]
        if bad_lanes:
            logger.error(
                "scanner_lane_map contains lanes not in toolheads list: %s. "
                "Add them to toolheads or fix the mapping.",
                ", ".join(bad_lanes),
            )
            sys.exit(1)

    return config


cfg = load_config()

# Warn if scanner_lane_map is configured but dispatcher is unavailable
if cfg.get("scanner_lane_map") and not DISPATCHER_AVAILABLE:
    logger.warning(
        "scanner_lane_map is configured but the rich-tag dispatcher is not available "
        "(adapters/ directory not found). openprinttag_scanner topics will be subscribed "
        "but payloads will not be parsed — scans will be silently ignored. "
        "Ensure the adapters/ directory is present to enable scanner support."
    )

# SpoolmanClient for rich-data tag sync (OpenTag3D, openprinttag_scanner)
# Only instantiated when both the dispatcher and a Spoolman URL are available.
spoolman_client = None
if DISPATCHER_AVAILABLE and cfg["spoolman_url"]:
    spoolman_client = SpoolmanClient(cfg["spoolman_url"])

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
        logger.info("Discovering Klipper save_variables path...")
        response = requests.get(f"{cfg['moonraker_url']}/printer/configfile/settings", timeout=5)
        response.raise_for_status()
        settings = response.json().get("result", {}).get("settings", {})
        filename = settings.get("save_variables", {}).get("filename")

        if not filename:
            logger.warning("No [save_variables] in Klipper config. Klipper sync disabled.")
            return None

        if not filename.startswith("/"):
            filename = os.path.join(os.path.expanduser("~/printer_data/config"), filename)

        logger.info(f"Discovered Klipper variables file: {filename}")
        return filename
    except Exception as e:
        logger.error(f"Failed to discover Klipper variables path: {e}")
        return None

# ============================================================
# Spoolman Helpers
# ============================================================

def get_spool_by_id(spool_id):
    """Fetch a single spool directly from Spoolman."""
    if not cfg["spoolman_url"]:
        return None
    try:
        response = requests.get(f"{cfg['spoolman_url']}/api/v1/spool/{spool_id}", timeout=5)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        logger.error(f"Failed to fetch spool {spool_id}: {e}")
        return None

def refresh_spool_cache():
    """
    Pulls ALL spools from Spoolman and builds a local dictionary mapping NFC UIDs to Spoolman data.
    We do this so when a tag is scanned, the lookup is instant instead of waiting on a network request.
    """
    global spool_cache, last_cache_refresh
    if not cfg["spoolman_url"]:
        return False
    try:
        logger.info("Refreshing Spoolman cache...")
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
        logger.info(f"Cache updated: {len(spool_cache)} spools indexed.")
        return True
    except Exception as e:
        logger.error(f"Failed to refresh Spoolman cache: {e}")
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
    logger.info(f"UID {uid} not in cache, performing forced refresh...")
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
        logger.debug(f"LED: Skipping {lane} — AFC state is {status}")
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
        logger.info(f"LED: {lane} -> #{color_hex} (low={is_low})")
    except Exception as e:
        logger.error(f"Failed to update LED on {lane}: {e}")

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
            logger.info(f"[single] Activated spool {spool_id}")

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
            logger.info(f"[toolchanger] Updated {macro} with spool {spool_id}")

        elif mode == "afc":
            # Let AFC handle the actual assignment logic
            requests.post(f"{cfg['moonraker_url']}/printer/gcode/script",
                          json={"script": f"SET_SPOOL_ID LANE={toolhead} SPOOL_ID={spool_id}"},
                          timeout=5).raise_for_status()
            logger.info(f"[afc] Set spool {spool_id} on {toolhead} via AFC")

        return True
    except Exception as e:
        logger.error(f"Activation failed: {e}")
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
    logger.info(f"MQTT: {lane} -> {state}")

def on_connect(client, userdata, flags, rc):
    """Fires when we successfully connect to the MQTT broker."""
    if rc == 0:
        logger.info(f"Connected to MQTT broker (Mode: {cfg['toolhead_mode']})")
        client.publish("nfc/middleware/online", "true", qos=1, retain=True)
        
        # Subscribe to all our configured lanes/toolheads (PN532/ESPHome path)
        for t in cfg["toolheads"]:
            client.subscribe(f"nfc/toolhead/{t}")
        logger.info(f"Subscribed to: {', '.join(cfg['toolheads'])}")

        # Subscribe to openprinttag_scanner topics (PN5180 path)
        # Each scanner publishes to: openprinttag/<device_id>/tag/state
        scanner_map = cfg.get("scanner_lane_map", {})
        if scanner_map:
            prefix = cfg.get("scanner_topic_prefix", "openprinttag")
            for device_id in scanner_map:
                topic = f"{prefix}/{device_id}/tag/state"
                client.subscribe(topic)
            logger.info(f"Subscribed to {len(scanner_map)} openprinttag_scanner(s): {', '.join(scanner_map.keys())}")
            
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
        logger.error(f"MQTT connection failed: {rc}")

def _extract_scanner_device_id(topic: str) -> str | None:
    """
    Extracts the scanner deviceId from an openprinttag_scanner MQTT topic.

    Expected topic shape: openprinttag/<deviceId>/tag/state
    Returns the deviceId string, or None if the topic does not match.

    This is the single authoritative place that parses scanner topics.
    Both _resolve_lane_from_topic() and _handle_rich_tag() use this.
    """
    prefix = cfg.get("scanner_topic_prefix", "openprinttag")
    parts = topic.split("/") if topic else []
    if len(parts) >= 4 and parts[0] == prefix and parts[2] == "tag" and parts[3] == "state":
        return parts[1]
    return None


def _resolve_lane_from_topic(topic):
    """
    Determines the lane/toolhead name from an MQTT topic.
    
    For PN532/ESPHome topics like 'nfc/toolhead/lane1', returns 'lane1'.
    For openprinttag_scanner topics like 'openprinttag/scanner-lane1/tag/state',
    looks up the device ID in scanner_lane_map to find the lane name.
    Returns None if the topic can't be mapped to a lane.
    """
    scanner_map = cfg.get("scanner_lane_map", {})

    # Check if it's a scanner topic: openprinttag/<device_id>/tag/state
    device_id = _extract_scanner_device_id(topic)
    if device_id is not None:
        lane = scanner_map.get(device_id)
        if lane:
            return lane
        logger.warning(f"Scanner device '{device_id}' not found in scanner_lane_map")
        return None

    # Otherwise it's a PN532 topic: nfc/toolhead/<lane> (exactly 3 parts)
    parts = topic.split("/") if topic else []
    if len(parts) == 3 and parts[0] == "nfc" and parts[1] == "toolhead":
        return parts[2]

    return None


def _activate_from_scan(client, toolhead, scan, spool_info=None):
    """
    Activates a toolhead from scan data, with optional Spoolman enrichment.

    Two separate concerns:
      1. Spool-ID activation (Spoolman-backed only)
           — only runs when spool_info.spoolman_id is available
           — updates active_spools, calls activate_spool()
      2. Tag-state publication (always runs from scan data)
           — color, low-spool state, LED updates
           — driven by scan object; spool_info enriches color if available

    This means activation always succeeds even when Spoolman is unreachable.
    """
    mode = cfg["toolhead_mode"]

    # --- Spool-ID activation (Spoolman-backed, optional) ---
    if spool_info and spool_info.spoolman_id is not None:
        if activate_spool(spool_info.spoolman_id, toolhead):
            active_spools[toolhead] = spool_info.spoolman_id
    else:
        logger.warning(
            "No Spoolman spool_id available for toolhead %s; "
            "skipping spool-id activation and continuing with tag-only updates",
            toolhead,
        )

    # --- Resolve color and low-spool state from best available source ---
    # Spoolman color wins when available — a human set it deliberately.
    # Fall back to scan color, then white.
    # Use explicit is not None checks — 0.0 remaining and "" color are valid values
    # and must not fall through to the scan fallback via truthiness short-circuit.
    if spool_info and spool_info.color_hex is not None:
        color_hex = spool_info.color_hex
    else:
        color_hex = scan.color_hex or "FFFFFF"

    if spool_info and spool_info.remaining_weight_g is not None:
        remaining = spool_info.remaining_weight_g
    else:
        remaining = scan.remaining_weight_g
    is_low = remaining is not None and remaining <= cfg["low_spool_threshold"]
    filament_label = scan.material_name or scan.material_type or "Unknown"

    # --- Mode-specific tag-state output ---
    # Always driven from scan data. Transport differs by mode:
    #   afc          — color via Klipper LED macro, lock via MQTT nfc/.../lock
    #   single/toolchanger — color and low_spool via MQTT nfc/toolhead/... topics
    if mode == "afc":
        publish_lock(toolhead, "lock")
        update_klipper_led(toolhead, color_hex, is_low)
        if is_low:
            logger.warning(f"Low spool: {filament_label} ({remaining:.1f}g) on {toolhead}")
    else:
        client.publish(
            f"nfc/toolhead/{toolhead}/color",
            color_hex.lstrip("#").upper(),
            qos=1, retain=True,
        )
        topic_low = f"nfc/toolhead/{toolhead}/low_spool"
        if is_low:
            logger.warning(f"Low spool: {filament_label} ({remaining:.1f}g) on {toolhead}")
            client.publish(topic_low, "true", qos=1, retain=True)
        else:
            client.publish(topic_low, "false", qos=1, retain=True)


def _handle_rich_tag(client, toolhead, payload, topic):
    """
    Handles a rich-data NFC tag (OpenTag3D or openprinttag_scanner).

    Routes through the dispatcher to parse the tag data into a ScanEvent.
    Spoolman sync is best-effort — if it fails, activation continues from
    tag data alone. The two concerns are kept separate:

      - Activation path  : always runs from scan data
      - Enrichment path  : Spoolman sync, weight update, spool ID tracking
    """
    try:
        scan = detect_and_parse(payload, toolhead, topic)
        logger.info(f"Rich tag parsed: {scan.source} — {scan.brand_name} {scan.material_type} (UID: {scan.uid})")

        # Guard: scanner may report present=False or invalid data
        if not scan.present:
            logger.debug(f"Scanner reported no tag present on {toolhead}")
            return
        if not scan.tag_data_valid:
            logger.warning(f"Scanner reported invalid tag data on {toolhead}")
            return

        # --- Enrichment path (best-effort) ---
        spool_info = None
        if spoolman_client is None:
            logger.debug("Spoolman not configured — skipping enrichment, running tag-only activation")
        else:
            try:
                spool_info = spoolman_client.sync_spool_from_scan(scan, prefer_tag=True)
            except Exception:
                logger.exception(
                    "Spoolman sync failed for rich tag scan; continuing with tag-only activation. "
                    "uid=%s topic=%s",
                    scan.uid,
                    topic,
                )

        # --- Activation path (always runs) ---
        _activate_from_scan(client, toolhead, scan, spool_info=spool_info)

        # --- Tag writeback (Phase 1: scan-time stale-tag reconciliation) ---
        # device_id is only present for openprinttag_scanner topics.
        # PN532/ESPHome topics return None so writeback is skipped for those.
        device_id = _extract_scanner_device_id(topic)

        write_plan = build_write_plan(scan, spool_info, device_id=device_id)
        if write_plan:
            scanner_writer.execute(write_plan, client)

    except NotImplementedError as e:
        logger.warning(f"Tag format not yet supported: {e}")
    except ValueError as e:
        logger.debug(f"Dispatcher rejected payload: {e}")
        if cfg["toolhead_mode"] != "afc":
            client.publish(f"nfc/toolhead/{toolhead}/low_spool", "false", qos=1, retain=True)
            client.publish(f"nfc/toolhead/{toolhead}/color", "error", qos=1, retain=True)
    except Exception as e:
        logger.error(f"Rich tag processing error: {e}")


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
            logger.warning(f"Could not resolve lane from topic: {topic}")
            return

        uid = payload.get("uid", "")

        # If the lane is locked (already has a spool), ignore the scan
        if lane_locks.get(toolhead):
            logger.info(f"Ignoring scan on {toolhead} (locked)")
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
                logger.warning(f"Empty UID in payload on {toolhead}")
                return

            spool = find_spool_by_nfc(uid)
            if spool:
                spool_id = spool["id"]
                filament = spool.get("filament", {})
                name = filament.get("name", "Unknown")
                color_hex = filament.get("color_hex", "FFFFFF") or "FFFFFF"
                logger.info(f"Found spool: {name} (ID: {spool_id})")
                
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
                            logger.warning(f"Low spool: {name} ({remaining:.1f}g) on {toolhead}")
                    else:
                        topic_low = f"nfc/toolhead/{toolhead}/low_spool"
                        if remaining is not None and remaining <= cfg["low_spool_threshold"]:
                            logger.warning(f"Low spool: {name} ({remaining:.1f}g) on {toolhead}")
                            client.publish(topic_low, "true", qos=1, retain=True)
                        else:
                            client.publish(topic_low, "false", qos=1, retain=True)
            else:
                logger.warning(f"No spool found for UID: {uid}")
                if cfg["toolhead_mode"] != "afc":
                    client.publish(f"nfc/toolhead/{toolhead}/low_spool", "false", qos=1, retain=True)
                    client.publish(f"nfc/toolhead/{toolhead}/color", "error", qos=1, retain=True)

    except Exception as e:
        logger.error(f"Message error: {e}")

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
                        logger.info(f"Klipper Sync: {t} -> spool {spool_id}")
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
                logger.info(f"Klipper Sync: {t} cleared")
                mqtt_client.publish(f"nfc/toolhead/{t}/color", "000000", qos=1, retain=True)
                active_spools[t] = None
    except Exception as e:
        logger.error(f"Klipper Sync failed: {e}")

def sync_from_afc_file():
    """
    The core logic for keeping AFC in sync.
    AFC writes its state to a JSON file (AFC.var.unit). We watch that file.
    When AFC changes state (e.g., finishes loading, or user ejects a spool), this runs.
    """
    path = cfg["afc_var_path"]
    if not os.path.exists(path):
        logger.warning(f"AFC var file not found: {path}")
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
                        logger.info(f"AFC Sync: {lane_name} has spool {spool_id}, locking")
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
                        logger.info(f"AFC Sync: {lane_name} empty, clearing")
                        publish_lock(lane_name, "clear")
                    
                    # Turn off the LED
                    if active_spools.get(lane_name):
                        update_klipper_led(lane_name, "000000", False, force=True)
                        active_spools[lane_name] = None
    except Exception as e:
        logger.error(f"AFC Sync failed: {e}")

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
            logger.info(f"Watching AFC var file in {afc_dir}")
    else:
        klipper_path = cfg.get("klipper_var_path")
        if klipper_path:
            klipper_dir = os.path.dirname(klipper_path)
            if os.path.exists(klipper_dir):
                observer.schedule(handler, klipper_dir, recursive=False)
                logger.info(f"Watching Klipper var file in {klipper_dir}")

    observer.start()
    return observer

# ============================================================
# Main Execution Loop
# ============================================================

def on_shutdown(signum, frame):
    """Runs when you hit Ctrl+C or stop the service. Cleans up locks and disconnects."""
    logger.info("Shutting down...")
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

logger.info(f"Starting SpoolSense Middleware (Mode: {cfg['toolhead_mode']})")
logger.info(f"Spoolman: {cfg['spoolman_url'] or 'disabled (tag-only mode)'}")
logger.info(f"Moonraker: {cfg['moonraker_url']}")
if DISPATCHER_AVAILABLE:
    logger.info("Rich tag dispatcher: enabled (OpenTag3D, openprinttag_scanner)")
else:
    logger.info("Rich tag dispatcher: disabled (adapters/ not found, UID-only mode)")
if cfg["toolhead_mode"] == "afc":
    logger.info(f"Lanes: {', '.join(cfg['toolheads'])}")
    logger.info(f"AFC var file: {cfg['afc_var_path']}")
    logger.info(f"LED macro: {cfg['afc_led_macro']}")
else:
    logger.info(f"Toolheads: {', '.join(cfg['toolheads'])}")
    logger.info(f"Low spool threshold: {cfg['low_spool_threshold']}g")
scanner_map = cfg.get("scanner_lane_map", {})
if scanner_map:
    logger.info(f"Scanner lane map: {json.dumps(scanner_map)}")

# Start the infinite loop
try:
    mqtt_client.connect(cfg["mqtt"]["broker"], cfg["mqtt"]["port"], 60)
    watcher = start_watcher()
    mqtt_client.loop_forever()
except Exception as e:
    logger.error(f"Fatal error: {e}")
    sys.exit(1)