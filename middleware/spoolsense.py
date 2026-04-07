#!/usr/bin/env python3
from __future__ import annotations

__version__ = "1.6.0"
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

import logging
from logging.handlers import RotatingFileHandler
import os
import signal
import sys
import threading

import paho.mqtt.client as mqtt

import app_state
from config import CONFIG_PATH, load_config, has_afc_scanners, has_toolhead_scanners, has_toolhead_stage_scanners
from mqtt_handler import on_connect, on_message
from activation import publish_lock
from afc_status import AfcStatusSync
from publisher_manager import PublisherManager
from publishers.klipper import KlipperPublisher
from toolchanger_status import ToolchangerStatusSync
from toolhead_status import ToolheadStatusSync
from filament_usage import FilamentUsageSync
from var_watcher import start_klipper_watcher
from moonraker_ws import WEBSOCKET_AVAILABLE, MoonrakerWebsocket

# ── Logging ──────────────────────────────────────────────────────────────────
LOG_FORMAT       = '%(asctime)s %(levelname)s %(message)s'
LOG_FILE         = os.path.expanduser('~/SpoolSense/middleware/spoolsense.log')
LOG_MAX_BYTES    = 5 * 1024 * 1024                                              # 5MB per log file
LOG_BACKUP_COUNT = 3                                                            # keep 3 rotated copies

logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
_file_handler = RotatingFileHandler(LOG_FILE, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT)
_file_handler.setFormatter(logging.Formatter(LOG_FORMAT))
_file_handler.setLevel(logging.INFO)
logging.getLogger().addHandler(_file_handler)

logger = logging.getLogger(__name__)


# ── Shutdown ─────────────────────────────────────────────────────────────────

def on_shutdown(signum: int, frame: object) -> None:
    """Ctrl+C or systemd stop. Tears down sync services, clears locks, disconnects."""
    logger.info("Shutting down...")

    # Stop background threads in reverse startup order
    if app_state.publisher_manager:
        app_state.publisher_manager.shutdown()
    if app_state.moonraker_ws:
        app_state.moonraker_ws.stop()
    if app_state.afc_status_sync:
        app_state.afc_status_sync.stop()
    if app_state.toolchanger_status_sync:
        app_state.toolchanger_status_sync.stop()
    if app_state.filament_usage_sync:
        app_state.filament_usage_sync.stop()
    if app_state.toolhead_status_sync:
        app_state.toolhead_status_sync.stop()
    if app_state.watcher:
        app_state.watcher.stop()

    # Tell subscribers we're going offline, then release all scanner locks
    if app_state.mqtt_client:
        app_state.mqtt_client.publish("spoolsense/middleware/online", "false", qos=1, retain=True)
        for scanner_cfg in app_state.cfg.get("scanners", {}).values():
            target = scanner_cfg.get("lane") or scanner_cfg.get("toolhead")
            if target:
                publish_lock(target, "clear")
        app_state.mqtt_client.disconnect()

    sys.exit(0)


# ── Startup helpers ──────────────────────────────────────────────────────────

def _print_config_summary() -> None:
    """Prints a human-readable config dump for --check-config. No MQTT connection needed."""
    scanners = app_state.cfg.get("scanners", {})
    mobile   = app_state.cfg.get("mobile", {})

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
    print(f"  mobile_api       : {'enabled on port ' + str(mobile.get('port', 5001)) if mobile.get('enabled') else 'disabled'}")
    if mobile.get("enabled"):
        print(f"  mobile_action    : {mobile.get('action', 'afc_stage')}")


def _log_startup() -> None:
    """Logs what services are active so the user knows what's running."""
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
    if has_toolhead_scanners(app_state.cfg) or has_toolhead_stage_scanners(app_state.cfg):
        logger.info("Toolhead status: Moonraker spool eject polling")
    logger.info(f"Filament usage: UPDATE_TAG macro tracking enabled")
    logger.info(f"Dispatcher: {'enabled' if app_state.DISPATCHER_AVAILABLE else 'disabled'}")


def _setup_mqtt() -> None:
    """Wire up the MQTT client with credentials and callbacks."""
    app_state.mqtt_client = mqtt.Client()

    # Credentials are optional — anonymous connections work for most setups
    if app_state.cfg["mqtt"].get("username"):
        app_state.mqtt_client.username_pw_set(
            app_state.cfg["mqtt"]["username"],
            app_state.cfg["mqtt"].get("password"),
        )

    app_state.mqtt_client.on_connect  = on_connect                              # subscribes to scanner topics, syncs klipper vars
    app_state.mqtt_client.on_message  = on_message                              # dispatches tag payloads to activation pipeline
    app_state.mqtt_client.will_set(                                             # LWT — tells subscribers we're offline if we crash
        "spoolsense/middleware/online", "false", qos=1, retain=True
    )


def _setup_spoolman() -> None:
    """Connect to Spoolman for spool lookups, creation, and weight sync. Skipped if no URL configured."""
    if not app_state.cfg["spoolman_url"]:
        return
    from spoolman.client import SpoolmanClient
    app_state.spoolman_client = SpoolmanClient(app_state.cfg["spoolman_url"])


def _discover_afc_lanes() -> list[str]:
    """Ask Moonraker for AFC_stepper objects so we know which lanes to subscribe to via websocket."""
    if not has_afc_scanners(app_state.cfg):
        return []

    moonraker_url = app_state.cfg.get("moonraker_url", "")
    if not moonraker_url:
        return []

    try:
        import requests as req
        resp = req.get(f"{moonraker_url}/printer/objects/list", timeout=5)
        if not resp.ok:
            return []
        objects = resp.json().get("result", {}).get("objects", [])
        lanes = [o.replace("AFC_stepper ", "") for o in objects if o.startswith("AFC_stepper ")]
        if lanes:
            logger.info(f"Discovered AFC lanes: {lanes}")
        return lanes
    except Exception:
        logger.warning("Could not discover AFC lanes from Moonraker")
        return []


def _setup_websocket(lane_names: list[str]) -> bool:
    """Try to connect to Moonraker via websocket for real-time updates. Falls back to HTTP polling."""
    if not WEBSOCKET_AVAILABLE:
        logger.info("Moonraker: using HTTP polling (websocket-client not installed)")
        return False

    moonraker_url = app_state.cfg.get("moonraker_url")
    if not moonraker_url:
        return False

    # Convert http:// to ws:// for the websocket endpoint
    ws_url = moonraker_url.replace("http://", "ws://").replace("https://", "wss://")
    if not ws_url.endswith("/websocket"):
        ws_url = ws_url.rstrip("/") + "/websocket"

    app_state.moonraker_ws = MoonrakerWebsocket(ws_url)
    app_state.moonraker_ws.set_lane_names(lane_names)                           # needed for AFC_stepper subscriptions
    logger.info(f"Moonraker websocket: {ws_url}")
    return True


def _start_sync_services(use_ws: bool) -> None:
    """Start all background sync threads. Each service handles its own websocket vs polling decision."""
    cfg = app_state.cfg

    # AFC lane state — detects spool load/eject events via Moonraker
    if has_afc_scanners(cfg):
        app_state.afc_status_sync = AfcStatusSync()
        if use_ws:
            app_state.moonraker_ws.on_lane_update = app_state.afc_status_sync.on_ws_lane_update
        app_state.afc_status_sync.start(use_ws=use_ws)

    # ASSIGN_SPOOL macro — lets users assign scanned spools to tools via Klipper console.
    # Also needed when publish_lane_data is on with AFC (lane_data writes on tool assignment)
    if has_toolhead_stage_scanners(cfg) or (
        has_afc_scanners(cfg) and cfg.get("publish_lane_data", False)
    ):
        app_state.toolchanger_status_sync = ToolchangerStatusSync()
        if use_ws:
            app_state.moonraker_ws.on_assign_spool = app_state.toolchanger_status_sync.on_ws_assign_spool
        app_state.toolchanger_status_sync.start(use_ws=use_ws)

    # UPDATE_TAG macro — calculates filament usage after each print, sends deduction to scanner
    app_state.filament_usage_sync = FilamentUsageSync()
    if use_ws:
        app_state.moonraker_ws.on_update_tag = app_state.filament_usage_sync.on_ws_update_tag
    app_state.filament_usage_sync.start(use_ws=use_ws)

    # Wire all websocket callbacks before starting the connection
    if use_ws:
        app_state.moonraker_ws.start()

    # Toolhead spool eject detection — watches for spool_id changes on tool macros
    if has_toolhead_scanners(cfg) or has_toolhead_stage_scanners(cfg):
        app_state.toolhead_status_sync = ToolheadStatusSync()
        app_state.toolhead_status_sync.start()

    # Klipper variables file watcher — syncs spool IDs from save_variables.cfg on manual changes
    if has_toolhead_scanners(cfg):
        app_state.watcher = start_klipper_watcher()


def _start_rest_api() -> None:
    """Spin up the FastAPI server on a daemon thread for mobile app scanning."""
    mobile_cfg = app_state.cfg.get("mobile", {})
    if not mobile_cfg.get("enabled"):
        logger.info("REST API: disabled (mobile.enabled = false)")
        return

    import uvicorn
    from rest_api import app as rest_app

    rest_port = mobile_cfg.get("port", 5001)

    def _run():
        uvicorn.run(rest_app, host="0.0.0.0", port=rest_port, log_level="warning")

    rest_thread = threading.Thread(target=_run, name="rest-api", daemon=True)
    rest_thread.start()
    logger.info(f"REST API: http://0.0.0.0:{rest_port} (mobile scanning enabled)")


# ── Entry point ──────────────────────────────────────────────────────────────

def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="SpoolSense NFC Middleware")
    parser.add_argument("--check-config", action="store_true",
                        help="Validate config and print a summary, then exit.")
    args = parser.parse_args()

    # Load and validate config — exits on invalid config (strict validation)
    app_state.cfg = load_config()

    # Publishers handle output to printer platforms (only Klipper today)
    app_state.publisher_manager = PublisherManager()
    app_state.publisher_manager.register(KlipperPublisher(app_state.cfg))

    if args.check_config:
        _print_config_summary()
        sys.exit(0)

    # Dispatcher parses tag payloads — without it we can't process scans
    if not app_state.DISPATCHER_AVAILABLE:
        logger.error("Rich-tag dispatcher not available (adapters/ not found). Cannot start.")
        sys.exit(1)

    _setup_spoolman()                                                           # optional — runs in tag-only mode without it

    # Clean shutdown on SIGTERM (systemd) and SIGINT (Ctrl+C)
    signal.signal(signal.SIGTERM, on_shutdown)
    signal.signal(signal.SIGINT, on_shutdown)

    _setup_mqtt()
    _log_startup()

    # Discover AFC lanes from Moonraker, set up websocket if available
    lane_names = _discover_afc_lanes()
    use_ws     = _setup_websocket(lane_names)

    _start_sync_services(use_ws)                                                # AFC, toolchanger, toolhead status
    _start_rest_api()                                                           # mobile app scanning (if enabled)

    # Block on the MQTT loop — everything else runs in background threads
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
