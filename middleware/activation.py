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
    slot: str,
    color_hex: str, material: str, remaining: float | None, spoolman_id: int | None,
    nozzle_temp_min: int | None = None, nozzle_temp_max: int | None = None,
    bed_temp_min: int | None = None, bed_temp_max: int | None = None,
    uid: str | None = None, device_id: str = "",
    diameter_mm: float | None = None, density: float | None = None,
    tag_format: str | None = None,
) -> bool:
    """Store tag data in the per-consumer pending slot ("afc" is consumed by
    afc_status on lane load; "toolhead" by toolchanger_status on ASSIGN_SPOOL).

    The uid is stored exactly as the caller sent it — /api/status echoes this
    dict and the shipped mobile app reads it back (consumers that need a
    canonical form lowercase at their own boundary). uid and the filament
    props let the consumer record a deduction baseline on assignment (#109).

    Returns True if an earlier pending spool was replaced."""
    pending = {
        "color_hex": color_hex,
        "material": material,
        "remaining_g": remaining,
        "spoolman_id": spoolman_id,
        "nozzle_temp_min": nozzle_temp_min,
        "nozzle_temp_max": nozzle_temp_max,
        "bed_temp_min": bed_temp_min,
        "bed_temp_max": bed_temp_max,
        "uid": uid,
        "device_id": device_id,
        "diameter_mm": diameter_mm,
        "density": density,
        "tag_format": tag_format or "unknown",
    }
    with app_state.state_lock:
        if slot == "afc":
            replaced = app_state.pending_spool_afc is not None
            app_state.pending_spool_afc = pending
        else:
            replaced = app_state.pending_spool_toolhead is not None
            app_state.pending_spool_toolhead = pending
    return replaced


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
    remaining: float | None, scan: ScanEvent, device_id: str | None = None,
) -> SpoolEvent:
    """Build a SpoolEvent from resolved scan data."""
    # scanner_id source order: the device_id the caller resolved from the MQTT
    # topic (or "mobile" for REST scans) → an explicit device_id in the config
    # dict (not normally present) → the target → "unknown". The topic-derived
    # device_id is what lets event-stream (#93) consumers tell scanners apart.
    scanner_id = device_id or scanner_cfg.get("device_id") or target or "unknown"
    return SpoolEvent(
        spool_id=spoolman_id,
        action=action_enum,
        target=target or "",
        color=color_hex,
        material=filament_label,
        weight=remaining,
        # ScanEvent temp fields carry a _c suffix — the old getattr on the
        # suffixless names silently returned None for every rich scan,
        # nulling temps in events and lane_data since the publisher split.
        nozzle_temp_min=getattr(scan, "nozzle_temp_min_c", None),
        nozzle_temp_max=getattr(scan, "nozzle_temp_max_c", None),
        bed_temp_min=getattr(scan, "bed_temp_min_c", None),
        bed_temp_max=getattr(scan, "bed_temp_max_c", None),
        scanner_id=scanner_id,
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
                  spoolman_id: int | None, event: SpoolEvent,
                  scan: ScanEvent | None = None,
                  device_id: str | None = None) -> None:
    """Handle afc_stage and toolhead_stage — cache tag data, don't lock."""
    slot = "afc" if action_enum == Action.AFC_STAGE else "toolhead"
    raw = getattr(scan, "raw", None) or {}
    # spoolsense_scanner payloads carry the format in the payload; direct
    # OpenTag3D/OpenPrintTag payloads have no such key — their parser puts
    # the format name in scan.source, which matches _WRITABLE_FORMATS
    tag_format = raw.get("tag_format") or getattr(scan, "source", None)
    _cache_pending_spool(slot, color_hex, filament_label, remaining, spoolman_id,
                         event.nozzle_temp_min, event.nozzle_temp_max,
                         event.bed_temp_min, event.bed_temp_max,
                         uid=getattr(scan, "uid", None),
                         device_id=device_id or "",
                         diameter_mm=getattr(scan, "diameter_mm", None),
                         density=getattr(scan, "density", None),
                         tag_format=tag_format)
    stage_name = "afc_stage" if action_enum == Action.AFC_STAGE else "toolhead_stage"
    if spoolman_activated:
        logger.info(f"[{stage_name}] Spool staged with Spoolman ID, scanner remains unlocked")
    else:
        logger.info(f"[{stage_name}] Tag data cached, waiting for assignment. Scanner remains unlocked")
    if spoolman_id is None:
        # Tag-only staged scans never reach the publisher chain — the event
        # stream still gets them via the observer path (#93)
        notify_observers(event)


def notify_observers(event: SpoolEvent) -> None:
    """Fan an event out to secondary (observer) publishers only — never the
    primary, so no printer commands fire. No-op when the manager isn't wired."""
    manager = app_state.publisher_manager
    if manager is not None:
        manager.notify(event)


def _route_happy_hare(spoolman_id: int | None, event: SpoolEvent) -> None:
    """Handle happy_hare_stage — bind the scanned spool to the currently-selected
    MMU gate via Spoolman extras + Happy Hare sync trigger. No lock, no cache."""
    if spoolman_id is None:
        logger.warning("[happy_hare_stage] No Spoolman ID for scan — cannot bind to MMU gate. "
                       "The scanned tag must be registered in Spoolman first.")
        return
    from happy_hare import bind_spool_to_current_gate
    if bind_spool_to_current_gate(spoolman_id):
        notify_observers(event)


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

def activate_spool(spool_id: int, action: str, target: str | None = None,
                   color: str | None = None, material: str | None = None,
                   weight: float | None = None,
                   scanner_id: str = "legacy") -> bool:
    """
    UID-only fallback path — called when tag has no embedded data but maps to a Spoolman spool.
    Builds a SpoolEvent (with whatever Spoolman-resolved metadata the caller
    has) and routes through publisher_manager.
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
        color=color, material=material, weight=weight,
        nozzle_temp_min=None, nozzle_temp_max=None,
        bed_temp_min=None, bed_temp_max=None,
        scanner_id=scanner_id,
        tag_only=False,
    )
    return _publish_event(event)


# ── Rich-tag activation path ────────────────────────────────────────────────

def _activate_from_scan(
    scanner_cfg: dict,
    scan: ScanEvent,
    spool_info: SpoolInfo | None = None,
    device_id: str | None = None,
) -> None:
    """
    Main activation entry point for rich-data tags.

    Two concerns handled separately:
      1. Spool-ID activation (Spoolman-backed) — only when spoolman_id is available
      2. Action routing (always) — stage/cache or lock based on scanner action

    device_id is the scanner that produced the scan (topic-derived, or "mobile"
    for REST scans); it becomes the event stream's scanner_id (#93).
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
                               color_hex, filament_label, remaining, scan, device_id)

    # Happy Hare has its own binding path — skip the generic publisher chain
    # so a no-op publisher call doesn't burn a round trip for every scan.
    if action_enum == Action.HAPPY_HARE_STAGE:
        _route_happy_hare(spoolman_id, event)
        if remaining is not None and remaining <= app_state.cfg["low_spool_threshold"]:
            logger.warning(f"Low spool: {filament_label} ({remaining:.1f}g) on {target or 'staged'}")
        return

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
        _route_staged(action_enum, spoolman_activated, color_hex, filament_label,
                      remaining, spoolman_id, event, scan, device_id)
    elif action_enum in (Action.AFC_LANE, Action.TOOLHEAD):
        _route_dedicated(action_enum, spoolman_activated, spoolman_id, target, event)

    if remaining is not None and remaining <= app_state.cfg["low_spool_threshold"]:
        logger.warning(f"Low spool: {filament_label} ({remaining:.1f}g) on {target or 'staged'}")
