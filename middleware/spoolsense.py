#!/usr/bin/env python3
from __future__ import annotations

__version__ = "1.4.2"
"""
NFC Spoolman Middleware — Unified Edition with AFC Sync & LED Color Override
=============================================================================
Listens for NFC tag scans via MQTT and updates Klipper/Spoolman.
Includes automatic lock/clear logic by watching AFC's variable file,
and overrides BoxTurtle LEDs with actual filament colors from Spoolman.

Supports three toolhead modes (set toolhead_mode in config.yaml):

  single      — Calls SET_ACTIVE_SPOOL directly on every scan.
                Use for single-toolhead printers with one scanner.

  toolchanger — Saves the spool ID per toolhead and publishes the LED color,
                but does NOT call SET_ACTIVE_SPOOL. klipper-toolchanger handles
                activation at each toolchange. Tested on MadMax T0–T3.

  afc         — Calls AFC's SET_SPOOL_ID to register the spool in the correct
                lane. AFC auto-pulls color, material, and weight from Spoolman.
                After a successful scan, locks the scanner on that lane.
                Polls Moonraker's AFC status API for lane changes (eject → clear).
                Designed for BoxTurtle, NightOwl, and other AFC-based units.

Configuration is loaded from ~/SpoolSense/config.yaml — see config.example.yaml.
"""

import json
import logging
import signal
import sys

import paho.mqtt.client as mqtt

import app_state
from config import CONFIG_PATH, load_config
from mqtt_handler import on_connect, on_message
from activation import publish_lock
from afc_status import AfcStatusSync
from var_watcher import start_klipper_watcher

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)


def on_shutdown(signum: int, frame: object) -> None:
    """Runs when you hit Ctrl+C or stop the service. Cleans up locks and disconnects."""
    logger.info("Shutting down...")
    if app_state.afc_status_sync:
        app_state.afc_status_sync.stop()
    if app_state.mqtt_client:
        app_state.mqtt_client.publish("spoolsense/middleware/online", "false", qos=1, retain=True)
        if app_state.cfg["toolhead_mode"] == "afc":
            for lane in app_state.cfg["toolheads"]:
                publish_lock(lane, "clear")
        app_state.mqtt_client.disconnect()
    if app_state.watcher:
        app_state.watcher.stop()
    sys.exit(0)


def main() -> None:
    """
    Application entry point. All runtime startup logic lives here.

    Separating startup from module-level code means spoolsense can be
    imported safely for testing without triggering MQTT connections,
    config loading, or sys.exit() calls.

    CLI flags:
        --check-config   Validate config and print a summary, then exit.
                         Useful for verifying settings without starting the service.

    TODO (Phase 2): reduce reliance on globals by introducing an AppContext
    dataclass and passing dependencies explicitly into handlers.
    """
    import argparse

    parser = argparse.ArgumentParser(description="SpoolSense NFC Middleware")
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="Validate config and print a summary, then exit.",
    )
    args = parser.parse_args()

    app_state.cfg = load_config()

    if args.check_config:
        print(f"Config OK: {CONFIG_PATH}")
        print(f"  toolhead_mode    : {app_state.cfg['toolhead_mode']}")
        print(f"  toolheads        : {', '.join(app_state.cfg['toolheads'])}")
        print(f"  spoolman_url     : {app_state.cfg['spoolman_url'] or 'not set (tag-only mode)'}")
        print(f"  moonraker_url    : {app_state.cfg['moonraker_url']}")
        print(f"  mqtt.broker      : {app_state.cfg['mqtt']['broker']}")
        print(f"  scanner_lane_map : {app_state.cfg.get('scanner_lane_map') or 'not set'}")
        print(f"  afc_sync         : {'Moonraker API polling' if app_state.cfg['toolhead_mode'] == 'afc' else 'n/a (non-AFC mode)'}")
        print(f"  tag_writeback    : {'enabled' if app_state.cfg.get('tag_writeback_enabled') else 'disabled (dry-run)'}")
        print(f"  dispatcher       : {'available' if app_state.DISPATCHER_AVAILABLE else 'unavailable (required — will not start)'}")
        sys.exit(0)

    # Fail early if dispatcher is unavailable — required for all scanner payloads
    if not app_state.DISPATCHER_AVAILABLE:
        logger.error(
            "Rich-tag dispatcher is required but not available "
            "(adapters/ directory not found). The middleware will not start. "
            "Ensure the adapters/ directory is present."
        )
        sys.exit(1)

    # SpoolmanClient for rich-data tag sync (OpenTag3D, spoolsense_scanner)
    if app_state.DISPATCHER_AVAILABLE and app_state.cfg["spoolman_url"]:
        from spoolman.client import SpoolmanClient
        app_state.spoolman_client = SpoolmanClient(app_state.cfg["spoolman_url"])

    # Hook up shutdown signals
    signal.signal(signal.SIGTERM, on_shutdown)
    signal.signal(signal.SIGINT, on_shutdown)

    # Setup MQTT
    app_state.mqtt_client = mqtt.Client()
    if app_state.cfg["mqtt"].get("username"):
        app_state.mqtt_client.username_pw_set(
            app_state.cfg["mqtt"]["username"],
            app_state.cfg["mqtt"].get("password"),
        )

    app_state.mqtt_client.on_connect = on_connect
    app_state.mqtt_client.on_message = on_message
    app_state.mqtt_client.will_set("spoolsense/middleware/online", "false", qos=1, retain=True)

    logger.info(f"Starting SpoolSense Middleware (Mode: {app_state.cfg['toolhead_mode']})")
    logger.info(f"Spoolman: {app_state.cfg['spoolman_url'] or 'disabled (tag-only mode)'}")
    logger.info(f"Moonraker: {app_state.cfg['moonraker_url']}")
    if app_state.DISPATCHER_AVAILABLE:
        logger.info("Rich tag dispatcher: enabled (OpenTag3D, spoolsense_scanner)")
    else:
        logger.info("Rich tag dispatcher: disabled (adapters/ not found, UID-only mode)")
    if app_state.cfg["toolhead_mode"] == "afc":
        logger.info(f"Lanes: {', '.join(app_state.cfg['toolheads'])}")
        logger.info("AFC sync: Moonraker API polling")
    else:
        logger.info(f"Toolheads: {', '.join(app_state.cfg['toolheads'])}")
        logger.info(f"Low spool threshold: {app_state.cfg['low_spool_threshold']}g")
    scanner_map = app_state.cfg.get("scanner_lane_map", {})
    if scanner_map:
        logger.info(f"Scanner lane map: {json.dumps(scanner_map)}")

    # Start AFC status polling or Klipper var watcher based on mode
    if app_state.cfg["toolhead_mode"] == "afc":
        app_state.afc_status_sync = AfcStatusSync()
        app_state.afc_status_sync.start()
    else:
        app_state.watcher = start_klipper_watcher()

    # Start the MQTT loop
    try:
        app_state.mqtt_client.connect(
            app_state.cfg["mqtt"]["broker"], app_state.cfg["mqtt"]["port"], 60
        )
        app_state.mqtt_client.loop_forever()
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
