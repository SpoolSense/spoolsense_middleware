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


# ── Lock management ──────────────────────────────────────────────────────────

def publish_lock(lane: str, state: str) -> None:
    """Updates internal lock state. Lock prevents duplicate scans; clear re-enables scanning."""
    app_state.lane_locks[lane] = (state == "lock")
    logger.info(f"Lock: {lane} -> {state}")


# ── Publisher helpers ────────────────────────────────────────────────────────

def _publish_event(event: SpoolEvent) -> bool:
    """Route event through publisher_manager, fall back to KlipperPublisher if not initialized."""
    manager = app_state.publisher_manager
    if manager is not None:
        return manager.publish(event)
    # Fallback for tests or early startup before publisher_manager is wired
    from publishers.klipper import KlipperPublisher
    return KlipperPublisher(app_state.cfg).publish(event)


def _publish_tag_only(event: SpoolEvent, target: str) -> None:
    """No Spoolman — send tag data directly (color, material, weight) and lock the scanner."""
    tag_event = dataclasses.replace(event, spool_id=None, tag_only=True)
    _publish_event(tag_event)
    publish_lock(target, "lock")


def _cache_pending_spool(
    color_hex: str, material: str, remaining: float | None, spoolman_id: int | None
) -> None:
    """Store tag data for later consumption by afc_status (lane load) or toolchanger_status (tool pickup)."""
    with app_state.state_lock:
        app_state.pending_spool = {
            "color_hex": color_hex,
            "material": material,
            "remaining_g": remaining,
            "spoolman_id": spoolman_id,
        }


# ── Event building ───────────────────────────────────────────────────────────

def _resolve_scan_data(scan: ScanEvent, spool_info: SpoolInfo | None) -> tuple[str, float | None, str]:
    """Pick the best available color, weight, and material label from scan + Spoolman data."""
    # Spoolman enriches when available, tag data is the fallback
    color_hex = (spool_info.color_hex
                 if spool_info and spool_info.color_hex is not None
                 else scan.color_hex or "FFFFFF")
    remaining = (spool_info.remaining_weight_g
                 if spool_info and spool_info.remaining_weight_g is not None
                 else scan.remaining_weight_g)
    filament_label = scan.material_name or scan.material_type or "Unknown"
    return color_hex, remaining, filament_label


def _build_spool_event(
    scanner_cfg: dict, action_enum: Action, target: str | None,
    spoolman_id: int | None, color_hex: str, filament_label: str,
    remaining: float | None, scan: ScanEvent,
) -> SpoolEvent:
    """Build a SpoolEvent from resolved scan data."""
    return SpoolEvent(
        spool_id=spoolman_id,
        action=action_enum,
        target=target or "",
        color=color_hex,
        material=filament_label,
        weight=remaining,
        nozzle_temp_min=getattr(scan, "nozzle_temp_min", None),
        nozzle_temp_max=getattr(scan, "nozzle_temp_max", None),
        bed_temp_min=getattr(scan, "bed_temp_min", None),
        bed_temp_max=getattr(scan, "bed_temp_max", None),
        scanner_id=scanner_cfg.get("device_id", target or "unknown"),
        tag_only=spoolman_id is None,
    )


# ── Spoolman activation ─────────────────────────────────────────────────────

def _try_spoolman_activation(event: SpoolEvent, spoolman_id: int, target: str | None,
                             action_str: str) -> bool:
    """Attempt to activate a spool via publisher_manager. Returns True on success."""
    activated = _publish_event(event)
    if activated and target:
        app_state.active_spools[target] = spoolman_id
    elif not activated:
        logger.error(f"Activation failed for spool {spoolman_id} ({action_str})")
    return activated


# ── Action routing ───────────────────────────────────────────────────────────

def _route_staged(action_enum: Action, spoolman_activated: bool,
                  color_hex: str, filament_label: str, remaining: float | None,
                  spoolman_id: int | None) -> None:
    """Handle afc_stage and toolhead_stage — cache tag data, don't lock."""
    _cache_pending_spool(color_hex, filament_label, remaining, spoolman_id)
    stage_name = "afc_stage" if action_enum == Action.AFC_STAGE else "toolhead_stage"
    if spoolman_activated:
        logger.info(f"[{stage_name}] Spool staged with Spoolman ID, scanner remains unlocked")
    else:
        logger.info(f"[{stage_name}] Tag data cached, waiting for assignment. Scanner remains unlocked")


def _route_dedicated(action_enum: Action, spoolman_activated: bool,
                     spoolman_id: int | None, target: str, event: SpoolEvent) -> None:
    """Handle afc_lane and toolhead — lock after activation or tag-only publish."""
    if spoolman_activated:
        if action_enum == Action.AFC_LANE:
            logger.debug(f"AFC lane data via Spoolman (spool_id={spoolman_id})")
        publish_lock(target, "lock")
    elif spoolman_id is not None:
        # Activation failed — don't lock so user can rescan
        logger.warning(f"Not locking {target} — activation failed, rescan allowed")
    else:
        _publish_tag_only(event, target)


# ── UID-only activation path ────────────────────────────────────────────────

def activate_spool(spool_id: int, action: str, target: str | None = None) -> bool:
    """
    UID-only fallback path — called when tag has no embedded data but maps to a Spoolman spool.
    Builds a minimal SpoolEvent and routes through publisher_manager.
    Returns True if the primary publisher succeeded.
    """
    # Targeted actions need a target (lane or toolhead name)
    if action in ("afc_lane", "toolhead") and not target:
        logger.error(f"Cannot activate spool — action '{action}' requires a target but got None")
        return False

    try:
        action_enum = Action(action)
    except ValueError:
        logger.error(f"Unknown action: {action}")
        return False

    event = SpoolEvent(
        spool_id=spool_id,
        action=action_enum,
        target=target or "",
        color=None, material=None, weight=None,
        nozzle_temp_min=None, nozzle_temp_max=None,
        bed_temp_min=None, bed_temp_max=None,
        scanner_id="legacy",
        tag_only=False,
    )
    return _publish_event(event)


# ── Rich-tag activation path ────────────────────────────────────────────────

def _activate_from_scan(
    scanner_cfg: dict,
    scan: ScanEvent,
    spool_info: SpoolInfo | None = None,
) -> None:
    """
    Main activation entry point for rich-data tags.

    Two concerns handled separately:
      1. Spool-ID activation (Spoolman-backed) — only when spoolman_id is available
      2. Action routing (always) — stage/cache or lock based on scanner action
    """
    action_str = scanner_cfg["action"]
    target     = scanner_cfg.get("lane") or scanner_cfg.get("toolhead")

    try:
        action_enum = Action(action_str)
    except ValueError:
        logger.error(f"Unknown action in scanner config: {action_str!r}")
        return

    # Resolve best available data from tag + Spoolman
    color_hex, remaining, filament_label = _resolve_scan_data(scan, spool_info)
    spoolman_id = spool_info.spoolman_id if spool_info else None

    event = _build_spool_event(scanner_cfg, action_enum, target, spoolman_id,
                               color_hex, filament_label, remaining, scan)

    # Attempt Spoolman activation if we have a spool ID
    spoolman_activated = False
    if spoolman_id is not None:
        spoolman_activated = _try_spoolman_activation(event, spoolman_id, target, action_str)
    else:
        logger.warning(
            "No Spoolman spool_id available for %s (%s); "
            "skipping spool-id activation and continuing with tag-only updates",
            target or "afc_stage", action_str,
        )

    # Route by action type
    if action_enum in (Action.AFC_STAGE, Action.TOOLHEAD_STAGE):
        _route_staged(action_enum, spoolman_activated, color_hex, filament_label, remaining, spoolman_id)
    elif action_enum in (Action.AFC_LANE, Action.TOOLHEAD):
        _route_dedicated(action_enum, spoolman_activated, spoolman_id, target, event)

    if remaining is not None and remaining <= app_state.cfg["low_spool_threshold"]:
        logger.warning(f"Low spool: {filament_label} ({remaining:.1f}g) on {target or 'staged'}")
