from __future__ import annotations

import json
import logging

import paho.mqtt.client as mqtt

import app_state
from activation import activate_spool, publish_lock, _activate_from_scan
from spoolman_cache import find_spool_by_nfc, refresh_spool_cache
from config import discover_klipper_var_path, has_toolhead_scanners
from var_watcher import sync_from_klipper_vars, start_klipper_watcher

if app_state.DISPATCHER_AVAILABLE:
    from adapters.dispatcher import detect_and_parse
    from tag_sync.policy import build_write_plan
    from tag_sync import scanner_writer

logger = logging.getLogger(__name__)


def _extract_scanner_device_id(topic: str) -> str | None:
    """
    Extracts the scanner deviceId from a spoolsense_scanner MQTT topic.

    Expected topic shape: spoolsense/<deviceId>/tag/state
    Returns the deviceId string, or None if the topic does not match.
    """
    prefix = app_state.cfg.get("scanner_topic_prefix", "spoolsense")
    parts = topic.split("/") if topic else []
    if len(parts) == 4 and parts[0] == prefix and parts[2] == "tag" and parts[3] == "state":
        return parts[1]
    return None


def _resolve_scanner_from_topic(topic: str) -> dict | None:
    """
    Resolves the full scanner config from an MQTT topic.

    Extracts the device_id from the topic, looks it up in the scanners config,
    and returns the scanner config dict (with action, lane/toolhead, etc.).
    Returns None if the topic can't be mapped to a scanner.
    """
    scanners = app_state.cfg.get("scanners", {})

    device_id = _extract_scanner_device_id(topic)
    if device_id is not None:
        scanner_cfg = scanners.get(device_id)
        if scanner_cfg:
            return scanner_cfg
        logger.warning(f"Scanner device '{device_id}' not found in scanners config")
        return None

    return None


def _get_scanner_target(scanner_cfg: dict) -> str | None:
    """Returns the target (lane or toolhead name) from a scanner config, or None for afc_stage."""
    return scanner_cfg.get("lane") or scanner_cfg.get("toolhead")


def _handle_rich_tag(client: mqtt.Client, scanner_cfg: dict, payload: dict, topic: str) -> None:
    """
    Handles a rich-data NFC tag (OpenTag3D or spoolsense_scanner).

    Routes through the dispatcher to parse the tag data into a ScanEvent.
    Spoolman sync is best-effort — if it fails, activation continues from
    tag data alone. The two concerns are kept separate:

      - Activation path  : always runs from scan data
      - Enrichment path  : Spoolman sync, weight update, spool ID tracking
    """
    target = _get_scanner_target(scanner_cfg)
    action = scanner_cfg["action"]
    # For dispatcher, use target as the target_id (or device_id for afc_stage)
    target_id = target or _extract_scanner_device_id(topic) or "unknown"

    try:
        scan = detect_and_parse(payload, target_id, topic)
        logger.info(f"Rich tag parsed: {scan.source} — {scan.brand_name} {scan.material_type} (UID: {scan.uid})")

        # Guard: scanner may report present=False or invalid data
        if not scan.present:
            logger.debug(f"Scanner reported no tag present on {target_id}")
            return

        # UID-only ISO14443A tag (e.g. NTAG215) — no embedded filament data,
        # but we can still look the spool up in Spoolman via extra.nfc_id.
        if not scan.tag_data_valid and not scan.blank and scan.uid:
            uid = scan.uid.lower()
            logger.info(f"UID-only tag on {target_id}: {uid} — looking up in Spoolman")
            spool = find_spool_by_nfc(uid)
            if spool:
                spool_id = spool["id"]
                filament = spool.get("filament", {})
                name = filament.get("name", "Unknown")
                color_hex = (filament.get("color_hex") or "FFFFFF").lstrip("#").upper()
                logger.info(f"Found spool for UID {uid}: {name} (ID: {spool_id})")
                if activate_spool(spool_id, action, target):
                    if target:
                        app_state.active_spools[target] = spool_id
                    if action in ("afc_lane", "toolhead"):
                        publish_lock(target, "lock")
                    remaining = spool.get("remaining_weight")
                    if remaining is not None and remaining <= app_state.cfg["low_spool_threshold"]:
                        logger.warning(f"Low spool: {name} ({remaining:.1f}g) on {target_id}")
            else:
                logger.warning(f"No spool found in Spoolman for UID: {uid}")
            return

        if not scan.tag_data_valid:
            logger.warning(f"Scanner reported invalid tag data on {target_id}")
            return

        # --- Enrichment path (best-effort) ---
        spool_info = None
        if app_state.spoolman_client is None:
            logger.debug("Spoolman not configured — skipping enrichment, running tag-only activation")
        else:
            try:
                spool_info = app_state.spoolman_client.sync_spool_from_scan(scan, prefer_tag=True)
            except Exception:
                logger.exception(
                    "Spoolman sync failed for rich tag scan; continuing with tag-only activation. "
                    "uid=%s topic=%s",
                    scan.uid,
                    topic,
                )

        # --- Activation path (always runs) ---
        _activate_from_scan(scanner_cfg, scan, spool_info=spool_info)

        # --- Tag writeback (Phase 1: scan-time stale-tag reconciliation) ---
        device_id = _extract_scanner_device_id(topic)

        write_plan = build_write_plan(scan, spool_info, device_id=device_id)
        if write_plan:
            if app_state.cfg.get("tag_writeback_enabled"):
                scanner_writer.execute(write_plan, client)
            else:
                logger.info(
                    "[tag writeback disabled] would write: tag=%s device=%s payload=%s reason=%s",
                    write_plan.uid,
                    write_plan.device_id,
                    write_plan.payload,
                    write_plan.reason,
                )

    except NotImplementedError as e:
        logger.warning(f"Tag format not yet supported: {e}")
    except ValueError as e:
        logger.debug(f"Dispatcher rejected payload: {e}")
    except Exception as e:
        logger.error(f"Rich tag processing error: {e}")


def on_connect(client: mqtt.Client, userdata: object, flags: dict, rc: int) -> None:
    """Fires when we successfully connect to the MQTT broker."""
    if rc == 0:
        logger.info("Connected to MQTT broker")

        if not app_state.DISPATCHER_AVAILABLE:
            logger.error(
                "Rich-tag dispatcher is required but not available "
                "(adapters/ directory not found). Cannot process scanner payloads."
            )
            client.disconnect()
            return

        scanners = app_state.cfg.get("scanners", {})
        if not scanners:
            logger.error(
                "No scanners configured — no MQTT topics to subscribe to. "
                "Add a 'scanners' section to config.yaml."
            )
            client.disconnect()
            return

        # Subscribe to spoolsense_scanner topics
        prefix = app_state.cfg.get("scanner_topic_prefix", "spoolsense")
        for device_id in scanners:
            topic = f"{prefix}/{device_id}/tag/state"
            client.subscribe(topic)
        logger.info(
            f"Subscribed to {len(scanners)} scanner(s): {', '.join(scanners.keys())}"
        )

        client.publish("spoolsense/middleware/online", "true", qos=1, retain=True)
        refresh_spool_cache()

        # Klipper var sync for toolhead scanners (AFC sync is handled by afc_status.py)
        if has_toolhead_scanners(app_state.cfg):
            app_state.cfg["klipper_var_path"] = discover_klipper_var_path()
            sync_from_klipper_vars()

            # Restart the Klipper var watcher now that we know the path
            if app_state.watcher:
                app_state.watcher.stop()
                app_state.watcher.join(timeout=2)
            app_state.watcher = start_klipper_watcher()
    else:
        logger.error(f"MQTT connection failed: {rc}")


def on_message(client: mqtt.Client, userdata: object, msg: mqtt.MQTTMessage) -> None:
    """
    Fires every time an MQTT message arrives on a subscribed topic.

    Resolves the scanner config from the topic, checks lock state,
    then routes through the dispatcher for parsing and activation.
    """
    try:
        payload: dict = json.loads(msg.payload.decode())
        topic: str = msg.topic

        # Resolve which scanner this message belongs to
        scanner_cfg = _resolve_scanner_from_topic(topic)
        if not scanner_cfg:
            logger.warning(f"Could not resolve scanner from topic: {topic}")
            return

        # For afc_stage, there's no target to lock — always process
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
