from __future__ import annotations

import logging
import os
import sys

import requests
import yaml

logger = logging.getLogger(__name__)

CONFIG_PATH: str = os.path.expanduser("~/SpoolSense/config.yaml")

DEFAULTS: dict = {
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
    # spoolsense_scanner settings (optional — only needed for PN5180 setups)
    # Maps scanner MQTT device IDs to lane/toolhead names.
    # Each ESP32 running spoolsense_scanner publishes to:
    #   spoolsense/<device_id>/tag/state
    # This mapping tells the middleware which lane each scanner belongs to.
    "scanner_topic_prefix": "spoolsense",
    "scanner_lane_map": {},  # e.g. {"scanner-lane1": "lane1", "scanner-lane2": "lane2"}
    # Tag writeback — disabled by default. Enable only after verifying dry-run logs.
    "tag_writeback_enabled": False,
}

VALID_MODES: tuple[str, ...] = ("single", "toolchanger", "afc")


def load_config() -> dict:
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

    if not isinstance(user_config, dict):
        logger.error(
            f"{CONFIG_PATH} must be a YAML mapping (key: value pairs), "
            f"but got {type(user_config).__name__}. Check your config file."
        )
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

    # Validate toolheads list
    toolheads = config.get("toolheads")
    if not toolheads:
        logger.error("toolheads must be a non-empty list in %s", CONFIG_PATH)
        sys.exit(1)

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


def discover_klipper_var_path() -> str | None:
    """
    Queries Moonraker to find exactly where Klipper is saving its variables.
    This is better than hardcoding it, because users put save_variables.cfg in different places.
    """
    import app_state

    if app_state.cfg.get("klipper_var_path"):
        return app_state.cfg["klipper_var_path"]

    try:
        logger.info("Discovering Klipper save_variables path...")
        response = requests.get(
            f"{app_state.cfg['moonraker_url']}/printer/configfile/settings", timeout=5
        )
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
