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

import yaml

logger = logging.getLogger(__name__)

CONFIG_PATH: str = os.path.expanduser("~/SpoolSense/config.yaml")

VALID_ACTIONS: tuple[str, ...] = (
    "afc_stage", "afc_lane", "toolhead", "toolhead_stage", "happy_hare_stage",
)

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
    "scanner_topic_prefix": "spoolsense",
    "scanners": {},
    "tag_writeback_enabled": False,
    # Bondtech INDX: mirror save_variables.active_tool to Spoolman's active
    # spool on each tool pickup. Inert on non-INDX printers.
    "active_tool_sync": True,
    "publish_events": True,
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


def _apply_scanner_defaults(config: dict) -> None:
    """
    Apply per-scanner defaults before derivation and validation.

    Runs in load_config() ahead of _derive_toolheads() and
    _validate_scanners() — single-toolhead users shouldn't need to
    specify `toolhead: "T0"` explicitly (#44).
    """
    for scanner_cfg in config.get("scanners", {}).values():
        if isinstance(scanner_cfg, dict) and scanner_cfg.get("action") == "toolhead":
            scanner_cfg.setdefault("toolhead", "T0")


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

        elif action in ("afc_stage", "toolhead_stage", "happy_hare_stage"):
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


def has_happy_hare_scanners(config: dict) -> bool:
    """Returns True if any scanner has a happy_hare_stage action."""
    return any(
        s.get("action") == "happy_hare_stage"
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

    # klipper_var_path is obsolete — variables sync via the Moonraker
    # websocket now (#85). Accept-and-ignore so existing configs don't break.
    if config.get("klipper_var_path"):
        logger.warning(
            "klipper_var_path is deprecated and ignored — Klipper variables "
            "now sync via the Moonraker websocket. Remove it from config.yaml."
        )

    # Migrate legacy config if needed
    config = _migrate_legacy_config(config)

    # Apply scanner defaults before derivation and validation
    _apply_scanner_defaults(config)

    # Derive toolheads from scanner entries if not explicitly provided
    if not config.get("toolheads"):
        config["toolheads"] = _derive_toolheads(config)
        if config["toolheads"]:
            logger.info(f"Derived toolheads from scanners: {config['toolheads']}")

    _validate_scanners(config)
    # happy_hare runs before mobile: the mobile validator reads the
    # happy_hare section, and the type/shape guards live in the HH validator
    _validate_happy_hare(config)
    _validate_mobile(config)

    # Happy Hare: gates become mobile-assignable targets G0..G{n-1}. Runs
    # after validation (num_gates is checked there). Only when no explicit/
    # derived toolheads exist — HH installs have no toolhead scanners, so
    # this is normally the only source.
    if not config.get("toolheads") and config.get("happy_hare", {}).get("num_gates"):
        config["toolheads"] = [f"G{i}" for i in range(config["happy_hare"]["num_gates"])]
        logger.info(f"Derived gate targets from happy_hare.num_gates: {config['toolheads']}")

    return config


def _validate_mobile(config: dict) -> None:
    """Set defaults for the mobile REST API config and validate."""
    mobile = config.setdefault("mobile", {})
    mobile.setdefault("enabled", False)
    mobile.setdefault("action", "afc_stage")
    mobile.setdefault("port", 5001)

    mobile_action = mobile["action"]
    if mobile_action not in ("afc_stage", "toolhead_stage", "toolhead", "happy_hare_stage"):
        _config_error("mobile.action must be afc_stage, toolhead_stage, toolhead, "
                      "or happy_hare_stage (got %s)", mobile_action)
    if mobile_action == "toolhead" and not mobile.get("toolhead"):
        _config_error("mobile.action 'toolhead' requires a 'toolhead' field (e.g. T0)")
    if mobile_action == "happy_hare_stage":
        if not config.get("happy_hare", {}).get("enabled"):
            _config_error("mobile.action 'happy_hare_stage' requires happy_hare.enabled: true")
        # Spool resolution (uid -> Spoolman id) happens at scan time; without
        # Spoolman every scan dead-ends with a misleading not-found message
        if not config.get("spoolman_url"):
            _config_error(
                "mobile.action 'happy_hare_stage' requires spoolman_url — the gate "
                "assign flow resolves scanned tags against Spoolman."
            )
        # The app's gate picker renders the derived G0..G{n-1} targets; without
        # num_gates the picker is empty (or a stale fallback) and every assign fails
        if not config.get("happy_hare", {}).get("num_gates"):
            _config_error(
                "mobile.action 'happy_hare_stage' requires happy_hare.num_gates — "
                "it drives the app's gate picker (targets G0..G{n-1})."
            )
        # The gate-assign flow stages into the shared toolhead pending slot and
        # derives gate names into toolheads. Any other scanner type either
        # starts a competing pending-slot consumer (toolhead_stage; AFC with
        # publish_lane_data) or derives lane/tool names that suppress the gate
        # targets. Keep HH-mobile configs pure: happy_hare_stage scanners only.
        other = [d for d, s in config.get("scanners", {}).items()
                 if isinstance(s, dict) and s.get("action") != "happy_hare_stage"]
        if other:
            _config_error(
                "mobile.action 'happy_hare_stage' can only be combined with "
                "happy_hare_stage scanners (found other actions on: %s).",
                ", ".join(sorted(other)),
            )
        if config.get("toolheads"):
            _config_error(
                "mobile.action 'happy_hare_stage' derives gate targets G0..G{n-1} — "
                "remove the explicit 'toolheads' list from config.yaml."
            )

    mobile_port = mobile["port"]
    if not isinstance(mobile_port, int) or mobile_port < 1 or mobile_port > 65535:
        _config_error("mobile.port must be an integer 1-65535 (got %s)", mobile_port)


def _validate_happy_hare(config: dict) -> None:
    """
    Validate the optional `happy_hare:` top-level section and cross-check
    against scanner actions. A `happy_hare_stage` scanner requires
    `happy_hare.enabled: true` and a `printer_name`.

    Multiple `happy_hare_stage` scanners are intentionally allowed — they
    all bind to whichever gate is currently selected, which is harmless.
    """
    # Malformed YAML (e.g. `happy_hare: null` or `happy_hare: "yes"`) should
    # fail loud, not crash on setdefault later. Absent key is fine — defaults
    # get applied.
    if "happy_hare" in config and not isinstance(config["happy_hare"], dict):
        _config_error(
            "happy_hare must be a mapping in config.yaml (got %s). "
            "Remove the section or format it as `happy_hare:\\n  enabled: true\\n  ...`",
            type(config["happy_hare"]).__name__,
        )

    happy_hare = config.setdefault("happy_hare", {})
    happy_hare.setdefault("enabled", False)
    happy_hare.setdefault("printer_name", "")

    has_hh_scanner = has_happy_hare_scanners(config)
    enabled = bool(happy_hare["enabled"])

    if has_hh_scanner and not enabled:
        _config_error(
            "A scanner has action 'happy_hare_stage' but happy_hare.enabled is false. "
            "Set 'happy_hare.enabled: true' in config.yaml or change the scanner action."
        )

    # printer_name is legacy-tolerated but no longer required: binding goes
    # through MMU_SPOOLMAN SPOOLID/GATE and Happy Hare stamps its own
    # printer identity on the spool.

    # Happy Hare binding writes to Spoolman — the integration cannot function
    # in tag-only mode. Fail loud at config load so users don't discover this
    # only after the first scan.
    if has_hh_scanner and not config.get("spoolman_url"):
        _config_error(
            "A scanner has action 'happy_hare_stage' but spoolman_url is not set. "
            "Happy Hare binding writes to Spoolman — tag-only mode is not supported "
            "for this action. Set 'spoolman_url' in config.yaml."
        )

    # num_gates drives the mobile gate picker (targets G0..G{n-1}). Optional:
    # without it, phone-side gate assignment is unavailable but the physical
    # select-then-scan flow still works.
    num_gates = happy_hare.get("num_gates")
    if num_gates is not None:
        if isinstance(num_gates, bool) or not isinstance(num_gates, int) \
                or num_gates < 1 or num_gates > 32:
            _config_error("happy_hare.num_gates must be an integer 1-32 (got %s)", num_gates)


