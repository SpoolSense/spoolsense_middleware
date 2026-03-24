from __future__ import annotations

import logging
import os
import sys

import requests
import yaml

logger = logging.getLogger(__name__)

CONFIG_PATH: str = os.path.expanduser("~/SpoolSense/config.yaml")

VALID_ACTIONS: tuple[str, ...] = ("afc_stage", "afc_lane", "toolhead")

DEFAULTS: dict = {
    "mqtt": {
        "broker": None,
        "port": 1883,
        "username": None,
        "password": None,
    },
    "spoolman_url": None,
    "moonraker_url": None,
    "low_spool_threshold": 100,
    "klipper_var_path": None,
    "scanner_topic_prefix": "spoolsense",
    "scanners": {},
    "tag_writeback_enabled": False,
}

# Legacy keys that trigger auto-migration
_LEGACY_KEYS: set[str] = {"toolhead_mode", "scanner_lane_map", "afc_var_path"}


def _migrate_legacy_config(config: dict) -> dict:
    """
    Auto-converts legacy toolhead_mode + scanner_lane_map configs to
    the new scanners format. Logs deprecation warnings.

    Legacy format:
        toolhead_mode: "afc"
        scanner_lane_map: {"ecb338": "lane1", "abcd12": "lane2"}

    New format:
        scanners:
          ecb338: {action: "afc_lane", lane: "lane1"}
          abcd12: {action: "afc_lane", lane: "lane2"}
    """
    has_legacy = any(k in config for k in _LEGACY_KEYS)
    has_scanners = bool(config.get("scanners"))

    if has_scanners and has_legacy:
        logger.warning(
            "Both 'scanners' and legacy config (toolhead_mode/scanner_lane_map) found. "
            "Using 'scanners' — legacy keys are ignored."
        )
        return config

    if not has_legacy:
        return config

    mode = config.get("toolhead_mode", "afc")
    scanner_map = config.get("scanner_lane_map", {})

    if not scanner_map:
        logger.warning(
            "Legacy toolhead_mode found but scanner_lane_map is empty. "
            "No scanners to migrate. Add a 'scanners' section to your config."
        )
        return config

    logger.warning(
        "Migrating legacy config: toolhead_mode=%s + scanner_lane_map → scanners format. "
        "Update your config.yaml to use the new 'scanners' section. "
        "See config.example.afc.yaml for examples.",
        mode,
    )

    scanners: dict[str, dict] = {}
    for device_id, target in scanner_map.items():
        if mode == "afc":
            scanners[device_id] = {"action": "afc_lane", "lane": target}
        elif mode in ("toolchanger", "single"):
            scanners[device_id] = {"action": "toolhead", "toolhead": target}

    config["scanners"] = scanners
    return config


def _validate_scanners(config: dict) -> None:
    """Validates the scanners config entries."""
    scanners = config.get("scanners", {})
    if not isinstance(scanners, dict) or not scanners:
        logger.error(
            "No scanners configured (or 'scanners' is not a mapping). "
            "Add a 'scanners' section to %s. See config.example.afc.yaml for examples.",
            CONFIG_PATH,
        )
        sys.exit(1)

    # Build the set of valid targets from toolheads (if provided)
    # or derive from scanner entries
    toolheads_list = config.get("toolheads")

    for device_id, scanner_cfg in scanners.items():
        if not isinstance(scanner_cfg, dict):
            logger.error("Scanner '%s' must be a mapping with 'action' key.", device_id)
            sys.exit(1)

        action = scanner_cfg.get("action")
        if action not in VALID_ACTIONS:
            logger.error(
                "Scanner '%s' has invalid action '%s' — must be one of: %s",
                device_id, action, ", ".join(VALID_ACTIONS),
            )
            sys.exit(1)

        if action == "afc_lane":
            lane = scanner_cfg.get("lane")
            if not lane:
                logger.error("Scanner '%s' with action 'afc_lane' requires a 'lane' field.", device_id)
                sys.exit(1)
            if "toolhead" in scanner_cfg:
                logger.error(
                    "Scanner '%s' has action 'afc_lane' but also has a 'toolhead' field — remove it.",
                    device_id,
                )
                sys.exit(1)
            if toolheads_list and lane not in toolheads_list:
                logger.error(
                    "Scanner '%s' maps to lane '%s' which is not in toolheads list. "
                    "Add it to toolheads or fix the scanner config.",
                    device_id, lane,
                )
                sys.exit(1)

        elif action == "toolhead":
            toolhead = scanner_cfg.get("toolhead")
            if not toolhead:
                logger.error("Scanner '%s' with action 'toolhead' requires a 'toolhead' field.", device_id)
                sys.exit(1)
            if "lane" in scanner_cfg:
                logger.error(
                    "Scanner '%s' has action 'toolhead' but also has a 'lane' field — remove it.",
                    device_id,
                )
                sys.exit(1)
            if toolheads_list and toolhead not in toolheads_list:
                logger.error(
                    "Scanner '%s' maps to toolhead '%s' which is not in toolheads list. "
                    "Add it to toolheads or fix the scanner config.",
                    device_id, toolhead,
                )
                sys.exit(1)

        elif action == "afc_stage":
            if "lane" in scanner_cfg or "toolhead" in scanner_cfg:
                logger.error(
                    "Scanner '%s' has action 'afc_stage' but has a 'lane' or 'toolhead' field — "
                    "afc_stage is a shared scanner with no target. Remove the extra field.",
                    device_id,
                )
                sys.exit(1)

        # afc_stage requires no additional fields


def _derive_toolheads(config: dict) -> list[str]:
    """
    Derives the toolheads list from scanner entries if not explicitly provided.

    Returns a list of unique lane/toolhead targets from all scanner configs.
    afc_stage scanners don't contribute (they have no target).
    """
    targets: list[str] = []
    seen: set[str] = set()
    for scanner_cfg in config.get("scanners", {}).values():
        action = scanner_cfg.get("action")
        target: str | None = None
        if action == "afc_lane":
            target = scanner_cfg.get("lane")
        elif action == "toolhead":
            target = scanner_cfg.get("toolhead")
        if target and target not in seen:
            targets.append(target)
            seen.add(target)
    return targets


def has_afc_scanners(config: dict) -> bool:
    """Returns True if any scanner has an AFC action (afc_stage or afc_lane)."""
    return any(
        s.get("action") in ("afc_stage", "afc_lane")
        for s in config.get("scanners", {}).values()
        if isinstance(s, dict)
    )


def has_toolhead_scanners(config: dict) -> bool:
    """Returns True if any scanner has a toolhead action."""
    return any(
        s.get("action") == "toolhead"
        for s in config.get("scanners", {}).values()
        if isinstance(s, dict)
    )


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
    if config.get("klipper_var_path"):
        config["klipper_var_path"] = os.path.expanduser(config["klipper_var_path"])

    # Validate required fields
    missing: list[str] = []
    if not config["mqtt"]["broker"]:
        missing.append("mqtt.broker")
    if not config["moonraker_url"]:
        missing.append("moonraker_url")

    if missing:
        logger.error(f"Missing required values in {CONFIG_PATH}: {', '.join(missing)}")
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

    # Migrate legacy config if needed
    config = _migrate_legacy_config(config)

    # Derive toolheads from scanner entries if not explicitly provided
    if not config.get("toolheads"):
        config["toolheads"] = _derive_toolheads(config)
        if config["toolheads"]:
            logger.info(f"Derived toolheads from scanners: {config['toolheads']}")

    # Validate scanners
    _validate_scanners(config)

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
