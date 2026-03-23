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


def activate_spool(spool_id: int, toolhead: str) -> bool:
    """Routes the spool activation to the correct Klipper logic based on your setup."""
    mode = app_state.cfg["toolhead_mode"]
    try:
        if mode == "single":
            # Tell Moonraker/Spoolman directly
            requests.post(
                f"{app_state.cfg['moonraker_url']}/server/spoolman/spool_id",
                json={"spool_id": spool_id},
                timeout=5,
            ).raise_for_status()
            # Save it to Klipper variables so macros survive a restart
            requests.post(
                f"{app_state.cfg['moonraker_url']}/printer/gcode/script",
                json={"script": f"SAVE_VARIABLE VARIABLE=t0_spool_id VALUE={spool_id}"},
                timeout=5,
            ).raise_for_status()
            logger.info(f"[single] Activated spool {spool_id}")

        elif mode == "toolchanger":
            macro = f"T{toolhead[-1]}"
            # Update the specific tool's macro variable
            requests.post(
                f"{app_state.cfg['moonraker_url']}/printer/gcode/script",
                json={"script": f"SET_GCODE_VARIABLE MACRO={macro} VARIABLE=spool_id VALUE={spool_id}"},
                timeout=5,
            ).raise_for_status()
            # Save to disk
            requests.post(
                f"{app_state.cfg['moonraker_url']}/printer/gcode/script",
                json={"script": f"SAVE_VARIABLE VARIABLE=t{toolhead[-1]}_spool_id VALUE={spool_id}"},
                timeout=5,
            ).raise_for_status()
            logger.info(f"[toolchanger] Updated {macro} with spool {spool_id}")

        elif mode == "afc":
            # Let AFC handle the actual assignment logic
            requests.post(
                f"{app_state.cfg['moonraker_url']}/printer/gcode/script",
                json={"script": f"SET_SPOOL_ID LANE={toolhead} SPOOL_ID={spool_id}"},
                timeout=5,
            ).raise_for_status()
            logger.info(f"[afc] Set spool {spool_id} on {toolhead} via AFC")

        return True
    except Exception as e:
        logger.error(f"Activation failed: {e}")
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
                # Replace spaces with underscores — Klipper gcode is space-delimited
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


def _activate_from_scan(toolhead: str, scan: ScanEvent, spool_info: SpoolInfo | None = None) -> None:
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
    mode = app_state.cfg["toolhead_mode"]

    # --- Spool-ID activation (Spoolman-backed, optional) ---
    spoolman_activated: bool = False
    if spool_info and spool_info.spoolman_id is not None:
        spoolman_activated = activate_spool(spool_info.spoolman_id, toolhead)
        if spoolman_activated:
            app_state.active_spools[toolhead] = spool_info.spoolman_id
        else:
            logger.error(f"Activation failed for spool {spool_info.spoolman_id} on {toolhead}")
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
    is_low = remaining is not None and remaining <= app_state.cfg["low_spool_threshold"]
    filament_label = scan.material_name or scan.material_type or "Unknown"

    # --- Mode-specific tag-state output ---
    if mode == "afc":
        if spoolman_activated:
            # SET_SPOOL_ID was sent — AFC queries Spoolman for color/material/weight.
            logger.debug(f"AFC lane data via Spoolman (spool_id={spool_info.spoolman_id})")
            publish_lock(toolhead, "lock")
        elif spool_info and spool_info.spoolman_id is not None:
            # Activation failed — don't lock, allow rescan
            logger.warning(f"Not locking {toolhead} — activation failed, rescan allowed")
        else:
            # No Spoolman — send tag data directly to AFC lane
            _send_afc_lane_data(toolhead, color_hex, filament_label, remaining)
            publish_lock(toolhead, "lock")

    if is_low:
        logger.warning(f"Low spool: {filament_label} ({remaining:.1f}g) on {toolhead}")
