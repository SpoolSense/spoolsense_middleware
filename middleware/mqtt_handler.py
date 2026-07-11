"""
mqtt_handler.py — MQTT callbacks and tag processing pipeline.

on_connect()  — subscribes to scanner topics, re-publishes AFC lock state
on_message()  — resolves scanner from topic, checks lock, routes to _handle_rich_tag()

Tag processing splits into two paths:
  Rich tag (OpenPrintTag/OpenTag3D) → dispatcher → Spoolman enrichment → activation → writeback
  UID-only (plain NTAG)            → Spoolman lookup by NFC ID → activation
"""
from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

import paho.mqtt.client as mqtt

import app_state
from activation import activate_spool, notify_observers, publish_lock, _activate_from_scan
from publishers.base import Action, SpoolEvent
from publishers.klipper import display_spoolcolor
from config import has_afc_scanners
import moonraker_client
from filament_usage import _check_low_spool

if TYPE_CHECKING:
    from spoolman.client import SpoolInfo
    from state.models import ScanEvent

if app_state.DISPATCHER_AVAILABLE:
    from adapters.dispatcher import detect_and_parse
    from tag_sync.policy import build_write_plan
    from tag_sync import scanner_writer

logger = logging.getLogger(__name__)


# ── Topic parsing ────────────────────────────────────────────────────────────

def _extract_scanner_device_id(topic: str) -> str | None:
    """Extract deviceId from topic shape: spoolsense/<deviceId>/tag/state"""
    prefix = app_state.cfg.get("scanner_topic_prefix", "spoolsense")
    parts = topic.split("/") if topic else []
    if len(parts) == 4 and parts[0] == prefix and parts[2] == "tag" and parts[3] == "state":
        return parts[1]
    return None


def _resolve_scanner_from_topic(topic: str) -> dict | None:
    """Look up the scanner config dict from an MQTT topic. Returns None if unmapped."""
    scanners = app_state.cfg.get("scanners", {})
    device_id = _extract_scanner_device_id(topic)
    if device_id is not None:
        scanner_cfg = scanners.get(device_id)
        if scanner_cfg:
            return scanner_cfg
        logger.warning(f"Scanner device '{device_id}' not found in scanners config")
    return None


def _get_scanner_target(scanner_cfg: dict) -> str | None:
    """Returns the target (lane or toolhead name), or None for shared scanners (afc_stage/toolhead_stage)."""
    return scanner_cfg.get("lane") or scanner_cfg.get("toolhead")


# ── UPDATE_TAG tracking ─────────────────────────────────────────────────────

def _record_spool_tracking(
    target: str, uid: str, device_id: str,
    remaining: float | None,
    diameter_mm: float | None = None,
    density: float | None = None,
    tag_format: str | None = None,
) -> None:
    """Store initial weight, UID, device, filament properties, and tag format for UPDATE_TAG deduction tracking."""
    if not target or not uid or remaining is None:
        return
    with app_state.state_lock:
        app_state.active_spool_tracking[target] = app_state.ActiveSpool(
            uid=uid,
            device_id=device_id or "",
            weight_g=remaining,
            diameter_mm=diameter_mm or 1.75,
            density=density or 1.24,
            tag_format=tag_format or "unknown",
        )
    # Persist so the deduction baseline survives restarts (#91)
    from tracking_store import save_tracking
    save_tracking()

    # Check low-spool threshold at scan time — if a new spool has plenty of
    # filament, this also clears any latched low-spool state from the same
    # device so the LED stops breathing after a spool swap.
    if device_id:
        _check_low_spool(device_id, remaining)


# ── UID-only tag handling ────────────────────────────────────────────────────

def _uid_only_event(action: str, target: str, spool_id: int, color_hex: str,
                    material: str, remaining: float | None,
                    device_id: str | None) -> SpoolEvent:
    """Build a SpoolEvent for observer notification from UID-only scan data."""
    return SpoolEvent(
        spool_id=spool_id,
        action=Action(action),
        target=target,
        color=color_hex,
        material=material,
        weight=remaining,
        nozzle_temp_min=None,
        nozzle_temp_max=None,
        bed_temp_min=None,
        bed_temp_max=None,
        scanner_id=device_id or "",
        tag_only=False,
    )


def _handle_uid_only_tag(client: mqtt.Client, scanner_cfg: dict, uid: str, topic: str) -> None:
    """UID-only tag (e.g. NTAG215) — no filament data on tag, look up spool in Spoolman via NFC ID."""
    target_id = _get_scanner_target(scanner_cfg) or _extract_scanner_device_id(topic) or "unknown"
    logger.info(f"UID-only tag on {target_id}: {uid} — looking up in Spoolman")

    spool = app_state.spoolman_client.find_by_nfc(uid) if app_state.spoolman_client else None
    if not spool:
        logger.warning(f"No spool found in Spoolman for UID: {uid}")
        return

    spool_id  = spool["id"]
    filament  = spool.get("filament", {})
    name      = filament.get("name", "Unknown")
    color_hex = (filament.get("color_hex") or "FFFFFF").lstrip("#").upper()
    remaining = spool.get("remaining_weight")
    material  = filament.get("material", "Unknown")
    logger.info(f"Found spool for UID {uid}: {name} (ID: {spool_id})")

    # Push color to scanner LED — UID-only tags have no color on the tag itself
    device_id = _extract_scanner_device_id(topic)
    display_color = display_spoolcolor(color_hex)
    if device_id and display_color:
        prefix = app_state.cfg.get("scanner_topic_prefix", "spoolsense")
        client.publish(f"{prefix}/{device_id}/cmd/set_color", display_color)
        logger.info(f"Sent color #{display_color} to scanner {device_id} LED")

    action = scanner_cfg["action"]
    target = _get_scanner_target(scanner_cfg)

    # Happy Hare — bind to currently-selected MMU gate immediately,
    # no cache, no lock.
    if action == "happy_hare_stage":
        from happy_hare import bind_spool_to_current_gate
        if bind_spool_to_current_gate(spool_id):
            notify_observers(_uid_only_event(action, "", spool_id, color_hex,
                                             material, remaining, device_id))
        # Still drive low-spool LED feedback on the scanner so users loading
        # multiple spools into MMU gates see when one is nearly empty.
        if device_id and remaining is not None:
            _check_low_spool(device_id, remaining)
        if remaining is not None and remaining <= app_state.cfg["low_spool_threshold"]:
            logger.warning(f"Low spool: {name} ({remaining:.1f}g) on {target_id}")
        return

    # Shared scanners — cache for later assignment, don't activate yet
    if action in ("toolhead_stage", "afc_stage"):
        with app_state.state_lock:
            app_state.pending_spool = {
                "color_hex": color_hex,
                "material": material,
                "remaining_g": remaining,
                "spoolman_id": spool_id,
            }
        logger.info(f"[{action}] Staged spool {spool_id} ({name}) for assignment")
        # Observer channels (MQTT event stream) still see staged scans even
        # though no printer command fires yet (#93)
        notify_observers(_uid_only_event(action, "", spool_id, color_hex,
                                         material, remaining, device_id))
        return

    # Dedicated scanners — activate immediately
    if not activate_spool(spool_id, action, target, color=color_hex,
                          material=material, weight=remaining,
                          scanner_id=device_id or "legacy"):
        return

    if target:
        app_state.active_spools[target] = spool_id
        _record_spool_tracking(target, uid, device_id or "", remaining,
                               tag_format="uid_only")

    if action in ("afc_lane", "toolhead"):
        publish_lock(target, "lock")

    if remaining is not None and remaining <= app_state.cfg["low_spool_threshold"]:
        logger.warning(f"Low spool: {name} ({remaining:.1f}g) on {target_id}")


# ── Rich-tag handling ────────────────────────────────────────────────────────

def _enrich_from_spoolman(scan: ScanEvent, topic: str) -> SpoolInfo | None:
    """Best-effort Spoolman sync — returns SpoolInfo or None if unavailable/failed."""
    if app_state.spoolman_client is None:
        return None
    try:
        return app_state.spoolman_client.sync_spool_from_scan(scan, prefer_tag=True)
    except Exception:
        logger.exception(
            "Spoolman sync failed for rich tag scan; continuing with tag-only activation. "
            "uid=%s topic=%s",
            scan.uid, topic,
        )
        return None


def _handle_tag_writeback(scan: ScanEvent, spool_info: SpoolInfo | None,
                          device_id: str | None, client: mqtt.Client) -> None:
    """Check if tag weight is stale and write updated data back to the scanner."""
    write_plan = build_write_plan(scan, spool_info, device_id=device_id)
    if not write_plan:
        return
    if app_state.cfg.get("tag_writeback_enabled"):
        scanner_writer.execute(write_plan, client)
    else:
        logger.info(
            "[tag writeback disabled] would write: tag=%s device=%s payload=%s reason=%s",
            write_plan.uid, write_plan.device_id, write_plan.payload, write_plan.reason,
        )


def _handle_rich_tag(client: mqtt.Client, scanner_cfg: dict, payload: dict, topic: str) -> None:
    """
    Handles a rich-data NFC tag (OpenTag3D or spoolsense_scanner).

    Routes through the dispatcher to parse the tag data into a ScanEvent.
    Spoolman sync is best-effort — if it fails, activation continues from
    tag data alone.
    """
    target = _get_scanner_target(scanner_cfg)
    target_id = target or _extract_scanner_device_id(topic) or "unknown"

    try:
        scan = detect_and_parse(payload, target_id, topic)
        logger.info(f"Rich tag parsed: {scan.source} — {scan.brand_name} {scan.material_type} (UID: {scan.uid})")

        # Guard: no tag on scanner
        if not scan.present:
            logger.debug(f"Scanner reported no tag present on {target_id}")
            return

        # UID-only path — plain NTAG with no embedded data, look up in Spoolman
        if not scan.tag_data_valid and not scan.blank and scan.uid:
            _handle_uid_only_tag(client, scanner_cfg, scan.uid.lower(), topic)
            return

        # Invalid tag data — nothing we can do
        if not scan.tag_data_valid:
            logger.warning(f"Scanner reported invalid tag data on {target_id}")
            return

        # Enrich from Spoolman (best-effort), then activate
        spool_info = _enrich_from_spoolman(scan, topic)
        device_id = _extract_scanner_device_id(topic)
        _activate_from_scan(scanner_cfg, scan, spool_info=spool_info, device_id=device_id)

        # Record initial weight for UPDATE_TAG filament deduction
        # tag_format comes from the scanner payload — tells us if this tag supports weight writes
        tag_format = payload.get("tag_format", "unknown")
        _record_spool_tracking(
            target, scan.uid.lower() if scan.uid else None, device_id or "",
            scan.remaining_weight_g, scan.diameter_mm, scan.density,
            tag_format=tag_format,
        )

        # Write updated weight back to tag if stale
        _handle_tag_writeback(scan, spool_info, device_id, client)

    except NotImplementedError as e:
        logger.warning(f"Tag format not yet supported: {e}")
    except ValueError as e:
        logger.debug(f"Dispatcher rejected payload: {e}")
    except Exception as e:
        logger.error(f"Rich tag processing error: {e}")


# ── MQTT callbacks ───────────────────────────────────────────────────────────

def on_connect(client: mqtt.Client, userdata: object, flags: dict, rc: int) -> None:
    """Fires on successful MQTT connection. Subscribes to scanner topics and syncs state."""
    if rc != 0:
        logger.error(f"MQTT connection failed: {rc}")
        return

    logger.info("Connected to MQTT broker")

    if not app_state.DISPATCHER_AVAILABLE:
        logger.error("Rich-tag dispatcher not available — cannot process scanner payloads.")
        client.disconnect()
        return

    scanners = app_state.cfg.get("scanners", {})
    if not scanners:
        logger.error("No scanners configured — add a 'scanners' section to config.yaml.")
        client.disconnect()
        return

    # Subscribe to each scanner's tag/state topic
    prefix = app_state.cfg.get("scanner_topic_prefix", "spoolsense")
    for device_id in scanners:
        client.subscribe(f"{prefix}/{device_id}/tag/state")
    logger.info(f"Subscribed to {len(scanners)} scanner(s): {', '.join(scanners.keys())}")

    client.publish("spoolsense/middleware/online", "true", qos=1, retain=True)

    # Health status (#41) — mark MQTT up and refresh the retained snapshot
    # so subscribers see this session's state after a reconnect.
    from health import set_health, publish_current
    set_health("mqtt", "connected")
    publish_current()

    if app_state.spoolman_client:
        app_state.spoolman_client.refresh()

    # Klipper variable sync happens over the Moonraker websocket
    # (klipper_vars.py) — nothing to re-establish on MQTT reconnect.

    # Re-publish AFC lock state so scanners know current state after reconnect
    if has_afc_scanners(app_state.cfg):
        from afc_status import resync_lock_state
        resync_lock_state()


def _is_printer_idle() -> bool:
    """
    Returns True when Klipper reports `print_stats.state == "standby"`.
    Returns False on any other state or on fetch failure — treat unknown as
    busy so we never auto-release a lock during a print.
    """
    return moonraker_client.is_printer_idle(app_state.cfg.get("moonraker_url", ""))


def _should_auto_release_lock(target: str, payload: dict) -> bool:
    """
    Decide whether a locked target should auto-release for an incoming scan.

    Auto-release happens when:
      - Incoming UID is known and differs from the currently-active UID
      - Klipper is idle (print_stats.state == "standby")

    Same-UID re-scans are ignored without auto-release. UID-less payloads
    (e.g. tag-removed events) leave the lock in place.
    """
    incoming_uid = (payload.get("uid") or "").lower()
    if not incoming_uid:
        return False

    # Snapshot active UID under state_lock before the network I/O in
    # _is_printer_idle() — avoids a race window where the active spool
    # could change while we're querying Klipper.
    with app_state.state_lock:
        rec = app_state.active_spool_tracking.get(target)
        active_uid = rec.uid.lower() if rec else ""

    if active_uid and incoming_uid == active_uid:
        return False

    return _is_printer_idle()


def on_message(client: mqtt.Client, userdata: object, msg: mqtt.MQTTMessage) -> None:
    """Fires on every MQTT message. Resolves scanner, checks lock, routes to handler."""
    try:
        payload: dict = json.loads(msg.payload.decode())
        topic: str = msg.topic

        scanner_cfg = _resolve_scanner_from_topic(topic)
        if not scanner_cfg:
            logger.warning(f"Could not resolve scanner from topic: {topic}")
            return

        # Shared scanners (afc_stage/toolhead_stage) have no target to lock
        target = _get_scanner_target(scanner_cfg)
        if target and app_state.lane_locks.get(target):
            if _should_auto_release_lock(target, payload):
                # _should_auto_release_lock did network I/O (~2s); re-verify
                # under state_lock to close the TOCTOU window before writing.
                # (CodeRabbit #79)
                incoming_uid = (payload.get("uid") or "").lower()
                released = False
                active_uid = ""
                with app_state.state_lock:
                    if app_state.lane_locks.get(target):
                        rec = app_state.active_spool_tracking.get(target)
                        active_uid = rec.uid.lower() if rec else ""
                        if not active_uid or active_uid != incoming_uid:
                            app_state.lane_locks[target] = False
                            released = True
                if released:
                    logger.info(
                        f"Lock auto-release on {target}: idle printer, swap "
                        f"{active_uid or '?'} → {incoming_uid}"
                    )
                    # Fall through to normal scan processing
                else:
                    logger.debug(
                        f"Lock auto-release aborted on {target}: state changed mid-check"
                    )
                    return
            else:
                logger.info(
                    f"Ignoring scan on {target} (locked). To unlock: eject the spool "
                    f"in Mainsail, or POST /api/unlock/{target} on the middleware REST API."
                )
                return

        if not app_state.DISPATCHER_AVAILABLE:
            logger.warning("Rich-tag dispatcher not available — cannot process scanner payload")
            return

        _handle_rich_tag(client, scanner_cfg, payload, topic)

    except Exception as e:
        logger.error(f"Message error: {e}")
