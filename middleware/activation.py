from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

import requests

import app_state

if TYPE_CHECKING:
    from spoolman.client import SpoolInfo
    from state.models import ScanEvent

logger = logging.getLogger(__name__)


def publish_lock(lane: str, state: str) -> None:
    """Updates internal lock state for a lane. Lock prevents duplicate scans; clear re-enables scanning."""
    app_state.lane_locks[lane] = (state == "lock")
    logger.info(f"Lock: {lane} -> {state}")


def activate_spool(spool_id: int, action: str, target: str | None = None) -> bool:
    """
    Routes spool activation to Klipper based on the scanner's action.

    Actions:
      afc_stage  → SET_NEXT_SPOOL_ID SPOOL_ID={id}  (global, no lane)
      afc_lane   → SET_SPOOL_ID LANE={target} SPOOL_ID={id}
      toolhead   → SET_ACTIVE_SPOOL + SAVE_VARIABLE for {target}
    """
    moonraker = app_state.cfg.get("moonraker_url", "")
    if not moonraker:
        logger.error("Cannot activate spool — moonraker_url not configured")
        return False
    # Guard: afc_lane and toolhead require a target
    if action in ("afc_lane", "toolhead") and not target:
        logger.error(f"Cannot activate spool — action '{action}' requires a target but got None")
        return False

    try:
        if action == "afc_stage":
            requests.post(
                f"{moonraker}/printer/gcode/script",
                json={"script": f"SET_NEXT_SPOOL_ID SPOOL_ID={spool_id}"},
                timeout=5,
            ).raise_for_status()
            logger.info(f"[afc_stage] Staged spool {spool_id} for next AFC load")

        elif action == "afc_lane":
            requests.post(
                f"{moonraker}/printer/gcode/script",
                json={"script": f"SET_SPOOL_ID LANE={target} SPOOL_ID={spool_id}"},
                timeout=5,
            ).raise_for_status()
            logger.info(f"[afc_lane] Set spool {spool_id} on {target} via AFC")

        elif action == "toolhead":
            requests.post(
                f"{moonraker}/server/spoolman/spool_id",
                json={"spool_id": spool_id},
                timeout=5,
            ).raise_for_status()
            requests.post(
                f"{moonraker}/printer/gcode/script",
                json={"script": f"SAVE_VARIABLE VARIABLE={target}_spool_id VALUE={spool_id}"},
                timeout=5,
            ).raise_for_status()
            logger.info(f"[toolhead] Activated spool {spool_id} on {target}")

        elif action == "toolhead_stage":
            # Shared scanner — spool_id is staged, actual assignment happens
            # on tool pickup (handled by toolchanger_status.py)
            logger.info(f"[toolhead_stage] Staged spool {spool_id} for next tool pickup")

        else:
            logger.error(f"Unknown action: {action}")
            return False

        return True
    except Exception:
        logger.exception(f"Activation failed ({action})")
        return False


def _validate_color_hex(color_hex: str) -> str | None:
    """Return the normalized 6-digit uppercase hex string, or None if invalid."""
    stripped = color_hex.lstrip("#").upper()
    if re.fullmatch(r"[A-Fa-f0-9]{6}", stripped):
        return stripped
    return None


def _validate_material(material: str) -> bool:
    """Return True only if material contains safe characters and is a reasonable length."""
    return bool(material) and len(material) <= 50 and bool(re.fullmatch(r"[A-Za-z0-9_ -]{1,50}", material))


def _send_afc_lane_data(
    toolhead: str,
    color_hex: str,
    material: str,
    remaining_g: float | None,
) -> None:
    """
    Send filament data directly to AFC lane via Klipper gcode commands.
    Used when Spoolman is not available — provides AFC with color, material,
    and weight from tag data so LEDs and lane info work without Spoolman.
    Each command is independent — if one fails, the others still run.
    """
    moonraker = app_state.cfg.get("moonraker_url", "")
    if not moonraker:
        return

    if color_hex and color_hex not in ("FFFFFF", "000000", ""):
        safe_color = _validate_color_hex(color_hex)
        if safe_color is None:
            logger.warning(f"[afc] Skipping SET_COLOR for {toolhead} — invalid color_hex: {color_hex!r}")
        else:
            try:
                requests.post(
                    f"{moonraker}/printer/gcode/script",
                    json={"script": f'SET_COLOR LANE={toolhead} COLOR={safe_color}'},
                    timeout=5,
                ).raise_for_status()
                logger.info(f"[afc] SET_COLOR {toolhead} = {safe_color}")
            except Exception as e:
                logger.error(f"[afc] SET_COLOR failed for {toolhead}: {e}")

    if material and material != "Unknown":
        if not _validate_material(material):
            logger.warning(f"[afc] Skipping SET_MATERIAL for {toolhead} — invalid material: {material!r}")
        else:
            try:
                safe_material = material.replace(" ", "_")
                requests.post(
                    f"{moonraker}/printer/gcode/script",
                    json={"script": f'SET_MATERIAL LANE={toolhead} MATERIAL={safe_material}'},
                    timeout=5,
                ).raise_for_status()
                logger.info(f"[afc] SET_MATERIAL {toolhead} = {material}")
            except Exception as e:
                logger.error(f"[afc] SET_MATERIAL failed for {toolhead}: {e}")

    if remaining_g is not None and remaining_g > 0:
        try:
            requests.post(
                f"{moonraker}/printer/gcode/script",
                json={"script": f'SET_WEIGHT LANE={toolhead} WEIGHT={remaining_g:.0f}'},
                timeout=5,
            ).raise_for_status()
            logger.info(f"[afc] SET_WEIGHT {toolhead} = {remaining_g:.0f}g")
        except Exception as e:
            logger.error(f"[afc] SET_WEIGHT failed for {toolhead}: {e}")


def _activate_from_scan(
    scanner_cfg: dict,
    scan: ScanEvent,
    spool_info: SpoolInfo | None = None,
) -> None:
    """
    Activates a scanner from scan data, routed by the scanner's action config.

    Two separate concerns:
      1. Spool-ID activation (Spoolman-backed only)
           — only runs when spool_info.spoolman_id is available
           — updates active_spools, calls activate_spool()
      2. Tag-state publication (always runs from scan data)
           — color, low-spool state, LED updates
           — driven by scan object; spool_info enriches color if available

    This means activation always succeeds even when Spoolman is unreachable.
    """
    action: str = scanner_cfg["action"]
    target: str | None = scanner_cfg.get("lane") or scanner_cfg.get("toolhead")

    # --- Spool-ID activation (Spoolman-backed, optional) ---
    spoolman_activated: bool = False
    if spool_info and spool_info.spoolman_id is not None:
        spoolman_activated = activate_spool(spool_info.spoolman_id, action, target)
        if spoolman_activated and target:
            app_state.active_spools[target] = spool_info.spoolman_id
        elif not spoolman_activated:
            logger.error(f"Activation failed for spool {spool_info.spoolman_id} ({action})")
    else:
        logger.warning(
            "No Spoolman spool_id available for %s (%s); "
            "skipping spool-id activation and continuing with tag-only updates",
            target or "afc_stage", action,
        )

    # --- Resolve color and low-spool state from best available source ---
    if spool_info and spool_info.color_hex is not None:
        color_hex = spool_info.color_hex
    else:
        color_hex = scan.color_hex or "FFFFFF"

    if spool_info and spool_info.remaining_weight_g is not None:
        remaining = spool_info.remaining_weight_g
    else:
        remaining = scan.remaining_weight_g
    is_low = remaining is not None and remaining <= app_state.cfg["low_spool_threshold"]
    filament_label = scan.material_name or scan.material_type or "Unknown"

    # --- Action-specific tag-state output ---
    if action == "afc_stage":
        # Shared scanner — no lock, scanner stays free.
        # Cache the tag data so afc_status can send it when a lane loads.
        with app_state.state_lock:
            app_state.pending_spool = {
                "color_hex": color_hex,
                "material": filament_label,
                "remaining_g": remaining,
                "spoolman_id": spool_info.spoolman_id if spool_info else None,
            }
        if spoolman_activated:
            logger.info("[afc_stage] Spool staged with Spoolman ID, scanner remains unlocked")
        else:
            logger.info("[afc_stage] Tag data cached, waiting for lane load. Scanner remains unlocked")

    elif action == "afc_lane":
        if spoolman_activated:
            logger.debug(f"AFC lane data via Spoolman (spool_id={spool_info.spoolman_id})")
            publish_lock(target, "lock")
        elif spool_info and spool_info.spoolman_id is not None:
            # Activation failed — don't lock, allow rescan
            logger.warning(f"Not locking {target} — activation failed, rescan allowed")
        else:
            # No Spoolman — send tag data directly to AFC lane
            _send_afc_lane_data(target, color_hex, filament_label, remaining)
            publish_lock(target, "lock")

    elif action == "toolhead":
        # Toolhead activation — lock is implicit (scanner is per-toolhead)
        if spoolman_activated:
            publish_lock(target, "lock")

    elif action == "toolhead_stage":
        # Shared scanner for toolchanger — no lock, scanner stays free.
        # Cache the tag data so toolchanger_status can send it when a tool is picked up.
        with app_state.state_lock:
            app_state.pending_spool = {
                "color_hex": color_hex,
                "material": filament_label,
                "remaining_g": remaining,
                "spoolman_id": spool_info.spoolman_id if spool_info else None,
            }
        if spoolman_activated:
            logger.info("[toolhead_stage] Spool staged with Spoolman ID, scanner remains unlocked")
        else:
            logger.info("[toolhead_stage] Tag data cached, waiting for tool pickup. Scanner remains unlocked")

    if is_low:
        logger.warning(f"Low spool: {filament_label} ({remaining:.1f}g) on {target or 'staged'}")
