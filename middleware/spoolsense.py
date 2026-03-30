#!/usr/bin/env python3
from __future__ import annotations

__version__ = "1.5.5"
"""
SpoolSense NFC Middleware
=========================
Listens for NFC tag scans via MQTT and updates Klipper/Spoolman.
Each scanner is configured with an action that determines how scans are routed:

  afc_stage   — Stages the spool for AFC via SET_NEXT_SPOOL_ID. The user loads
                filament into any lane, and AFC assigns the spool automatically.
                Scanner stays unlocked — ideal for a single shared scanner.

  afc_lane    — Assigns the spool to a specific AFC lane via SET_SPOOL_ID.
                Locks the scanner until the lane is cleared. One scanner per lane.

  toolhead    — Activates the spool on a specific toolhead. Sets active spool
                in Moonraker/Spoolman and saves to Klipper variables.

AFC lane state is synced via Moonraker's /printer/afc/status API (polling).
Klipper variables are synced via file watcher for toolhead scanners.

Configuration is loaded from ~/SpoolSense/config.yaml — see config.example.*.yaml.
"""

import json
import logging
import signal
import sys

import paho.mqtt.client as mqtt

import app_state
from config import CONFIG_PATH, load_config, has_afc_scanners, has_toolhead_scanners, has_toolhead_stage_scanners
from mqtt_handler import on_connect, on_message
from activation import publish_lock
from afc_status import AfcStatusSync
from publisher_manager import PublisherManager
from publishers.klipper import KlipperPublisher
from toolchanger_status import ToolchangerStatusSync
from var_watcher import start_klipper_watcher
from moonraker_ws import WEBSOCKET_AVAILABLE, MoonrakerWebsocket

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)


def on_shutdown(signum: int, frame: object) -> None:
    """Runs when you hit Ctrl+C or stop the service. Cleans up locks and disconnects."""
    logger.info("Shutting down...")
    if app_state.publisher_manager:
        app_state.publisher_manager.shutdown()
    if app_state.moonraker_ws:
        app_state.moonraker_ws.stop()
    if app_state.afc_status_sync:
        app_state.afc_status_sync.stop()
    if app_state.toolchanger_status_sync:
        app_state.toolchanger_status_sync.stop()
    if app_state.mqtt_client:
        app_state.mqtt_client.publish("spoolsense/middleware/online", "false", qos=1, retain=True)
        # Clear locks for scanners that use them (afc_lane and toolhead)
        scanners = app_state.cfg.get("scanners", {})
        for scanner_cfg in scanners.values():
            target = scanner_cfg.get("lane") or scanner_cfg.get("toolhead")
            if target:
                publish_lock(target, "clear")
        app_state.mqtt_client.disconnect()
    if app_state.watcher:
        app_state.watcher.stop()
    sys.exit(0)


def main() -> None:
    """
    Application entry point. All runtime startup logic lives here.

    CLI flags:
        --check-config   Validate config and print a summary, then exit.
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

    # Initialize publisher_manager early so check-config can reference it if needed
    app_state.publisher_manager = PublisherManager()
    app_state.publisher_manager.register(KlipperPublisher(app_state.cfg))

    if args.check_config:
        scanners = app_state.cfg.get("scanners", {})
        print(f"Config OK: {CONFIG_PATH}")
        print(f"  scanners         : {len(scanners)} configured")
        for device_id, cfg in scanners.items():
            target = cfg.get("lane") or cfg.get("toolhead") or "(shared)"
            print(f"    {device_id}: {cfg['action']} → {target}")
        if app_state.cfg.get("toolheads"):
            print(f"  toolheads        : {', '.join(app_state.cfg['toolheads'])}")
        print(f"  spoolman_url     : {app_state.cfg['spoolman_url'] or 'not set (tag-only mode)'}")
        print(f"  moonraker_url    : {app_state.cfg['moonraker_url']}")
        print(f"  mqtt.broker      : {app_state.cfg['mqtt']['broker']}")
        print(f"  afc_sync         : {'Moonraker API polling' if has_afc_scanners(app_state.cfg) else 'n/a'}")
        print(f"  macro_assign     : {'ASSIGN_SPOOL macro polling' if has_toolhead_stage_scanners(app_state.cfg) else 'n/a'}")
        print(f"  klipper_sync     : {'file watcher' if has_toolhead_scanners(app_state.cfg) else 'n/a'}")
        print(f"  tag_writeback    : {'enabled' if app_state.cfg.get('tag_writeback_enabled') else 'disabled (dry-run)'}")
        print(f"  dispatcher       : {'available' if app_state.DISPATCHER_AVAILABLE else 'unavailable (required — will not start)'}")
        sys.exit(0)

    # Fail early if dispatcher is unavailable
    if not app_state.DISPATCHER_AVAILABLE:
        logger.error(
            "Rich-tag dispatcher is required but not available "
            "(adapters/ directory not found). The middleware will not start."
        )
        sys.exit(1)

    # SpoolmanClient for rich-data tag sync
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

    # Startup logging
    scanners = app_state.cfg.get("scanners", {})
    logger.info(f"Starting SpoolSense Middleware v{__version__}")
    logger.info(f"Spoolman: {app_state.cfg['spoolman_url'] or 'disabled (tag-only mode)'}")
    logger.info(f"Moonraker: {app_state.cfg['moonraker_url']}")
    logger.info(f"Scanners: {len(scanners)} configured")
    for device_id, cfg in scanners.items():
        target = cfg.get("lane") or cfg.get("toolhead") or "(shared)"
        logger.info(f"  {device_id}: {cfg['action']} → {target}")
    if has_afc_scanners(app_state.cfg):
        logger.info("AFC sync: Moonraker API polling")
    if has_toolhead_stage_scanners(app_state.cfg):
        logger.info("Macro assign: ASSIGN_SPOOL macro polling")
    if has_toolhead_scanners(app_state.cfg):
        logger.info("Klipper sync: file watcher")
    logger.info(f"Dispatcher: {'enabled' if app_state.DISPATCHER_AVAILABLE else 'disabled'}")

    # Discover AFC lane names for websocket subscription
    lane_names = []
    if has_afc_scanners(app_state.cfg):
        try:
            moonraker_url = app_state.cfg.get("moonraker_url", "")
            if moonraker_url:
                import requests as req
                resp = req.get(f"{moonraker_url}/printer/objects/list", timeout=5)
                if resp.ok:
                    objects = resp.json().get("result", {}).get("objects", [])
                    lane_names = [
                        o.replace("AFC_stepper ", "")
                        for o in objects
                        if o.startswith("AFC_stepper ")
                    ]
                    if lane_names:
                        logger.info(f"Discovered AFC lanes: {lane_names}")
        except Exception:
            logger.warning("Could not discover AFC lanes from Moonraker")

    # Try websocket mode, fall back to HTTP polling
    use_ws = False
    if WEBSOCKET_AVAILABLE and app_state.cfg.get("moonraker_url"):
        moonraker_url = app_state.cfg["moonraker_url"]
        ws_url = moonraker_url.replace("http://", "ws://").replace("https://", "wss://")
        if not ws_url.endswith("/websocket"):
            ws_url = ws_url.rstrip("/") + "/websocket"
        app_state.moonraker_ws = MoonrakerWebsocket(ws_url)
        app_state.moonraker_ws.set_lane_names(lane_names)
        use_ws = True
        logger.info(f"Moonraker websocket: {ws_url}")
    elif not WEBSOCKET_AVAILABLE:
        logger.info("Moonraker: using HTTP polling (websocket-client not installed)")

    # Start sync services
    if has_afc_scanners(app_state.cfg):
        app_state.afc_status_sync = AfcStatusSync()
        if use_ws:
            app_state.moonraker_ws.on_lane_update = app_state.afc_status_sync.on_ws_lane_update
        app_state.afc_status_sync.start(use_ws=use_ws)

    if has_toolhead_stage_scanners(app_state.cfg) or (
        has_afc_scanners(app_state.cfg) and app_state.cfg.get("publish_lane_data", False)
    ):
        app_state.toolchanger_status_sync = ToolchangerStatusSync()
        if use_ws:
            app_state.moonraker_ws.on_assign_spool = app_state.toolchanger_status_sync.on_ws_assign_spool
        app_state.toolchanger_status_sync.start(use_ws=use_ws)

    # Start websocket connection after callbacks are wired
    if use_ws:
        app_state.moonraker_ws.start()

    if has_toolhead_scanners(app_state.cfg):
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
