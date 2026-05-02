"""
config.py — Configuration loading and validation.

Loads ~/SpoolSense/config.yaml, merges with defaults, validates scanners
and mobile config, migrates legacy formats, derives toolheads from scanner
entries. Exits on any invalid config — the middleware does not limp along.
"""
from __future__ import annotations

import logging
import os
import sys

import requests
import yaml

logger = logging.getLogger(__name__)

CONFIG_PATH: str = os.path.expanduser("~/SpoolSense/config.yaml")

VALID_ACTIONS: tuple[str, ...] = ("afc_stage", "afc_lane", "toolhead", "toolhead_stage")

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


def _config_error(msg: str, *args) -> None:
    """Log a config error and exit. All config validation failures are fatal."""
    logger.error(msg, *args)
    sys.exit(1)


def _validate_targeted_scanner(device_id: str, scanner_cfg: dict, action: str,
                               target_field: str, conflict_field: str,
                               toolheads_list: list | None) -> None:
    """Validate a scanner that requires a target (afc_lane or toolhead).
    target_field is 'lane' or 'toolhead', conflict_field is the opposite."""
    target = scanner_cfg.get(target_field)
    if not target:
        _config_error("Scanner '%s' with action '%s' requires a '%s' field.", device_id, action, target_field)
    if conflict_field in scanner_cfg:
        _config_error("Scanner '%s' has action '%s' but also has a '%s' field — remove it.", device_id, action, conflict_field)
    if toolheads_list and target not in toolheads_list:
        _config_error(
            "Scanner '%s' maps to %s '%s' which is not in toolheads list. "
            "Add it to toolheads or fix the scanner config.",
            device_id, target_field, target,
        )


def _validate_scanners(config: dict) -> None:
    """Validates the scanners config entries. Exits on any invalid config."""
    scanners = config.get("scanners", {})
    if not isinstance(scanners, dict) or not scanners:
        _config_error(
            "No scanners configured (or 'scanners' is not a mapping). "
            "Add a 'scanners' section to %s. See config.example.afc.yaml for examples.",
            CONFIG_PATH,
        )

    toolheads_list = config.get("toolheads")

    for device_id, scanner_cfg in scanners.items():
        if not isinstance(scanner_cfg, dict):
            _config_error("Scanner '%s' must be a mapping with 'action' key.", device_id)

        action = scanner_cfg.get("action")
        if action not in VALID_ACTIONS:
            _config_error("Scanner '%s' has invalid action '%s' — must be one of: %s",
                          device_id, action, ", ".join(VALID_ACTIONS))

        if action == "afc_lane":
            _validate_targeted_scanner(device_id, scanner_cfg, action, "lane", "toolhead", toolheads_list)

        elif action == "toolhead":
            _validate_targeted_scanner(device_id, scanner_cfg, action, "toolhead", "lane", toolheads_list)

        elif action in ("afc_stage", "toolhead_stage"):
            # Shared scanners have no target — lane/toolhead fields are invalid
            if "lane" in scanner_cfg or "toolhead" in scanner_cfg:
                _config_error(
                    "Scanner '%s' has action '%s' but has a 'lane' or 'toolhead' field — "
                    "%s is a shared scanner with no target. Remove the extra field.",
                    device_id, action, action,
                )


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


def has_toolhead_stage_scanners(config: dict) -> bool:
    """Returns True if any scanner has a toolhead_stage action."""
    return any(
        s.get("action") == "toolhead_stage"
        for s in config.get("scanners", {}).values()
        if isinstance(s, dict)
    )


def load_config() -> dict:
    """Load and validate configuration from ~/SpoolSense/config.yaml."""
    if not os.path.exists(CONFIG_PATH):
        logger.error("Copy the template:  cp config.example.yaml ~/SpoolSense/config.yaml")
        _config_error("Config file not found: %s", CONFIG_PATH)

    try:
        with open(CONFIG_PATH, "r") as f:
            user_config = yaml.safe_load(f) or {}
    except Exception as e:
        _config_error("Failed to read/parse %s: %s", CONFIG_PATH, e)

    if not isinstance(user_config, dict):
        _config_error("%s must be a YAML mapping (key: value pairs), but got %s",
                      CONFIG_PATH, type(user_config).__name__)

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
        _config_error("Missing required values in %s: %s", CONFIG_PATH, ", ".join(missing))

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

    # Apply scanner defaults before derivation and validation
    for scanner_cfg in config.get("scanners", {}).values():
        if isinstance(scanner_cfg, dict) and scanner_cfg.get("action") == "toolhead":
            scanner_cfg.setdefault("toolhead", "T0")  # single-toolhead users don't need to specify

    # Derive toolheads from scanner entries if not explicitly provided
    if not config.get("toolheads"):
        config["toolheads"] = _derive_toolheads(config)
        if config["toolheads"]:
            logger.info(f"Derived toolheads from scanners: {config['toolheads']}")

    _validate_scanners(config)
    _validate_mobile(config)

    return config


def _validate_mobile(config: dict) -> None:
    """Set defaults for the mobile REST API config and validate."""
    mobile = config.setdefault("mobile", {})
    mobile.setdefault("enabled", False)
    mobile.setdefault("action", "afc_stage")
    mobile.setdefault("port", 5001)

    mobile_action = mobile["action"]
    if mobile_action not in ("afc_stage", "toolhead_stage", "toolhead"):
        _config_error("mobile.action must be afc_stage, toolhead_stage, or toolhead (got %s)", mobile_action)
    if mobile_action == "toolhead" and not mobile.get("toolhead"):
        _config_error("mobile.action 'toolhead' requires a 'toolhead' field (e.g. T0)")

    mobile_port = mobile["port"]
    if not isinstance(mobile_port, int) or mobile_port < 1 or mobile_port > 65535:
        _config_error("mobile.port must be an integer 1-65535 (got %s)", mobile_port)


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
            f"{app_state.cfg['moonraker_url']}/printer/objects/query?configfile=settings",
            timeout=5,
        )
        response.raise_for_status()
        # Defensive walk through the nested response — guard each level with
        # isinstance(dict) so an unexpected Moonraker response shape returns
        # None cleanly instead of raising AttributeError. (CodeRabbit #79)
        cur: object = response.json()
        for key in ("result", "status", "configfile", "settings", "save_variables", "filename"):
            if not isinstance(cur, dict):
                cur = None
                break
            cur = cur.get(key)
        filename = cur if isinstance(cur, str) else None

        if not filename:
            logger.warning("No [save_variables] in Klipper config. Klipper sync disabled.")
            return None

        # Klipper may report the path as `~/...` (literal tilde), absolute, or
        # bare-relative. Expand `~` first so the absolute-path branch is taken
        # when applicable; otherwise fall back to the default config dir.
        filename = os.path.expanduser(filename)
        if not filename.startswith("/"):
            filename = os.path.join(os.path.expanduser("~/printer_data/config"), filename)

        logger.info(f"Discovered Klipper variables file: {filename}")
        return filename
    except (requests.RequestException, ValueError):
        logger.exception("Failed to discover Klipper variables path")
        return None
