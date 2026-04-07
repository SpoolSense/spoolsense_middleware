from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

import paho.mqtt.client as mqtt

import app_state
from activation import activate_spool, publish_lock, _activate_from_scan  # activate_spool used for UID-only path
from publishers.klipper import display_spoolcolor
from spoolman_cache import find_spool_by_nfc, refresh_spool_cache
from config import discover_klipper_var_path, has_afc_scanners, has_toolhead_scanners

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
) -> None:
    """Store initial weight, UID, device, and filament properties for UPDATE_TAG deduction tracking."""
    if not target or not uid or remaining is None:
        return
    with app_state.state_lock:
        app_state.active_spool_weights[target]   = remaining
        app_state.active_spool_uids[target]      = uid
        app_state.active_spool_devices[target]    = device_id or ""
        app_state.active_spool_diameters[target]  = diameter_mm or 1.75
        app_state.active_spool_densities[target]  = density or 1.24


# ── UID-only tag handling ────────────────────────────────────────────────────

def _handle_uid_only_tag(client: mqtt.Client, scanner_cfg: dict, uid: str, topic: str) -> None:
    """UID-only tag (e.g. NTAG215) — no filament data on tag, look up spool in Spoolman via NFC ID."""
    target_id = _get_scanner_target(scanner_cfg) or _extract_scanner_device_id(topic) or "unknown"
    logger.info(f"UID-only tag on {target_id}: {uid} — looking up in Spoolman")

    spool = find_spool_by_nfc(uid)
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
        return

    # Dedicated scanners — activate immediately
    if not activate_spool(spool_id, action, target):
        return

    if target:
        app_state.active_spools[target] = spool_id
        _record_spool_tracking(target, uid, device_id or "", remaining)

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
        _activate_from_scan(scanner_cfg, scan, spool_info=spool_info)

        # Record initial weight for UPDATE_TAG filament deduction
        device_id = _extract_scanner_device_id(topic)
        _record_spool_tracking(
            target, scan.uid.lower() if scan.uid else None, device_id or "",
            scan.remaining_weight_g, scan.diameter_mm, scan.density,
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
    refresh_spool_cache()

    # Sync klipper variables for toolhead scanners (AFC uses afc_status.py instead)
    if has_toolhead_scanners(app_state.cfg):
        app_state.cfg["klipper_var_path"] = discover_klipper_var_path()
        sync_from_klipper_vars()
        if app_state.watcher:
            app_state.watcher.stop()
            app_state.watcher.join(timeout=2)
        from var_watcher import start_klipper_watcher
        app_state.watcher = start_klipper_watcher()

    # Re-publish AFC lock state so scanners know current state after reconnect
    if has_afc_scanners(app_state.cfg):
        from afc_status import resync_lock_state
        resync_lock_state()


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
            logger.info(f"Ignoring scan on {target} (locked)")
            return

        if not app_state.DISPATCHER_AVAILABLE:
            logger.warning("Rich-tag dispatcher not available — cannot process scanner payload")
            return

        _handle_rich_tag(client, scanner_cfg, payload, topic)

    except Exception as e:
        logger.error(f"Message error: {e}")
