"""
activation.py — Spool activation orchestrator.

Owns the orchestration layer: lock decisions, pending_spool caching,
active_spools tracking, and low-spool detection. Builds SpoolEvent objects
from resolved scan/Spoolman data and routes them through publisher_manager.

Publishers (publishers/klipper.py, etc.) handle all platform-specific output.
This file contains no Moonraker HTTP calls.

publish_lock() is a shared utility used by this module, afc_status.py, and
toolchanger_status.py. It is NOT part of the publisher system.
"""
from __future__ import annotations

import dataclasses
import logging
from typing import TYPE_CHECKING

import app_state
from publishers.base import Action, SpoolEvent

# Validation helpers re-exported here for backward compatibility.
# Tests and other callers that import from activation continue to work.
from publishers.klipper import _validate_color_hex, _validate_material  # noqa: F401

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
    Routes spool activation to the configured publishers based on the scanner's action.

    This function is the UID-only fallback path (called from mqtt_handler.py when
    a tag has no embedded filament data but maps to a Spoolman spool via NFC ID).
    It builds a SpoolEvent and routes through publisher_manager.

    Actions:
      afc_stage  → SET_NEXT_SPOOL_ID SPOOL_ID={id}  (global, no lane)
      afc_lane   → SET_SPOOL_ID LANE={target} SPOOL_ID={id}
      toolhead   → SET_ACTIVE_SPOOL + SAVE_VARIABLE for {target}

    Returns True if the primary publisher succeeded, False otherwise.
    """
    # Guard: afc_lane and toolhead require a target
    if action in ("afc_lane", "toolhead") and not target:
        logger.error(f"Cannot activate spool — action '{action}' requires a target but got None")
        return False

    try:
        action_enum = Action(action)
    except ValueError:
        logger.error(f"Unknown action: {action}")
        return False

    manager = app_state.publisher_manager
    if manager is None:
        # publisher_manager not yet initialized (e.g., during tests)
        # Fall back to direct klipper publish for backward compatibility
        from publishers.klipper import KlipperPublisher
        moonraker = app_state.cfg.get("moonraker_url", "")
        if not moonraker:
            logger.error("Cannot activate spool — moonraker_url not configured")
            return False
        temp_publisher = KlipperPublisher(app_state.cfg)
        event = SpoolEvent(
            spool_id=spool_id,
            action=action_enum,
            target=target or "",
            color=None,
            material=None,
            weight=None,
            nozzle_temp_min=None,
            nozzle_temp_max=None,
            bed_temp_min=None,
            bed_temp_max=None,
            scanner_id="legacy",
            tag_only=False,
        )
        return temp_publisher.publish(event)

    event = SpoolEvent(
        spool_id=spool_id,
        action=action_enum,
        target=target or "",
        color=None,
        material=None,
        weight=None,
        nozzle_temp_min=None,
        nozzle_temp_max=None,
        bed_temp_min=None,
        bed_temp_max=None,
        scanner_id="legacy",
        tag_only=False,
    )
    return manager.publish(event)


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
           — updates active_spools, calls publisher_manager.publish()
      2. Tag-state publication (always runs from scan data)
           — color, low-spool state, LED updates
           — driven by scan object; spool_info enriches color if available

    This means activation always succeeds even when Spoolman is unreachable.
    """
    action_str: str = scanner_cfg["action"]
    target: str | None = scanner_cfg.get("lane") or scanner_cfg.get("toolhead")

    try:
        action_enum = Action(action_str)
    except ValueError:
        logger.error(f"Unknown action in scanner config: {action_str!r}")
        return

    # --- Resolve color and low-spool state from best available source ---
    if spool_info and spool_info.color_hex is not None:
        color_hex: str = spool_info.color_hex
    else:
        color_hex = scan.color_hex or "FFFFFF"

    if spool_info and spool_info.remaining_weight_g is not None:
        remaining: float | None = spool_info.remaining_weight_g
    else:
        remaining = scan.remaining_weight_g
    is_low = remaining is not None and remaining <= app_state.cfg["low_spool_threshold"]
    filament_label = scan.material_name or scan.material_type or "Unknown"

    # --- Build SpoolEvent ---
    spoolman_id: int | None = spool_info.spoolman_id if spool_info else None
    tag_only: bool = spoolman_id is None

    event = SpoolEvent(
        spool_id=spoolman_id,
        action=action_enum,
        target=target or "",
        color=color_hex,
        material=filament_label,
        weight=remaining,
        nozzle_temp_min=getattr(scan, "nozzle_temp_min_c", None),
        nozzle_temp_max=getattr(scan, "nozzle_temp_max_c", None),
        bed_temp_min=getattr(scan, "bed_temp_min_c", None),
        bed_temp_max=getattr(scan, "bed_temp_max_c", None),
        scanner_id=scanner_cfg.get("device_id", target or "unknown"),
        tag_only=tag_only,
    )

    # --- Spool-ID activation (Spoolman-backed, optional) ---
    spoolman_activated: bool = False
    if spoolman_id is not None:
        manager = app_state.publisher_manager
        if manager is not None:
            spoolman_activated = manager.publish(event)
        else:
            # Fallback: use KlipperPublisher directly (e.g., during tests)
            from publishers.klipper import KlipperPublisher
            spoolman_activated = KlipperPublisher(app_state.cfg).publish(event)

        if spoolman_activated and target:
            app_state.active_spools[target] = spoolman_id
        elif not spoolman_activated:
            logger.error(f"Activation failed for spool {spoolman_id} ({action_str})")
    else:
        logger.warning(
            "No Spoolman spool_id available for %s (%s); "
            "skipping spool-id activation and continuing with tag-only updates",
            target or "afc_stage", action_str,
        )

    # --- Action-specific tag-state output ---
    if action_enum == Action.AFC_STAGE:
        # Shared scanner — no lock, scanner stays free.
        # Cache the tag data so afc_status can send it when a lane loads.
        with app_state.state_lock:
            app_state.pending_spool = {
                "color_hex": color_hex,
                "material": filament_label,
                "remaining_g": remaining,
                "spoolman_id": spoolman_id,
            }
        if spoolman_activated:
            logger.info("[afc_stage] Spool staged with Spoolman ID, scanner remains unlocked")
        else:
            logger.info("[afc_stage] Tag data cached, waiting for lane load. Scanner remains unlocked")

    elif action_enum == Action.AFC_LANE:
        if spoolman_activated:
            logger.debug(f"AFC lane data via Spoolman (spool_id={spoolman_id})")
            publish_lock(target, "lock")
        elif spoolman_id is not None:
            # Activation failed — don't lock, allow rescan
            logger.warning(f"Not locking {target} — activation failed, rescan allowed")
        else:
            # No Spoolman — publish tag-only event (klipper.py sends AFC lane data)
            tag_event = dataclasses.replace(event, spool_id=None, tag_only=True)
            manager = app_state.publisher_manager
            if manager is not None:
                manager.publish(tag_event)
            else:
                from publishers.klipper import KlipperPublisher
                KlipperPublisher(app_state.cfg).publish(tag_event)
            publish_lock(target, "lock")

    elif action_enum == Action.TOOLHEAD:
        if spoolman_activated:
            publish_lock(target, "lock")
        elif spoolman_id is not None:
            # Activation failed — don't lock, allow rescan
            logger.warning(f"Not locking {target} — activation failed, rescan allowed")
        else:
            # No Spoolman — publish tag-only event (klipper.py sends gcode variable)
            tag_event = dataclasses.replace(event, spool_id=None, tag_only=True)
            manager = app_state.publisher_manager
            if manager is not None:
                manager.publish(tag_event)
            else:
                from publishers.klipper import KlipperPublisher
                KlipperPublisher(app_state.cfg).publish(tag_event)
            publish_lock(target, "lock")

    elif action_enum == Action.TOOLHEAD_STAGE:
        # Shared scanner for toolchanger — no lock, scanner stays free.
        # Cache the tag data so toolchanger_status can send it when a tool is picked up.
        with app_state.state_lock:
            app_state.pending_spool = {
                "color_hex": color_hex,
                "material": filament_label,
                "remaining_g": remaining,
                "spoolman_id": spoolman_id,
            }
        if spoolman_activated:
            logger.info("[toolhead_stage] Spool staged with Spoolman ID, scanner remains unlocked")
        else:
            logger.info("[toolhead_stage] Tag data cached, waiting for tool pickup. Scanner remains unlocked")

    if is_low:
        logger.warning(f"Low spool: {filament_label} ({remaining:.1f}g) on {target or 'staged'}")
