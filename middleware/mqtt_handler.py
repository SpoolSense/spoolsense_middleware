from __future__ import annotations

import json
import logging

import paho.mqtt.client as mqtt

import app_state
from activation import activate_spool, publish_lock, _activate_from_scan
from spoolman_cache import find_spool_by_nfc, refresh_spool_cache
from var_watcher import sync_from_afc_file, sync_from_klipper_vars, start_watcher
from config import discover_klipper_var_path

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

    This is the single authoritative place that parses scanner topics.
    Both _resolve_lane_from_topic() and _handle_rich_tag() use this.
    """
    prefix = app_state.cfg.get("scanner_topic_prefix", "spoolsense")
    parts = topic.split("/") if topic else []
    if len(parts) == 4 and parts[0] == prefix and parts[2] == "tag" and parts[3] == "state":
        return parts[1]
    return None


def _resolve_lane_from_topic(topic: str) -> str | None:
    """
    Determines the lane/toolhead name from an MQTT topic.

    For spoolsense_scanner topics like 'spoolsense/scanner-lane1/tag/state',
    looks up the device ID in scanner_lane_map to find the lane name.
    Returns None if the topic can't be mapped to a lane.
    """
    scanner_map = app_state.cfg.get("scanner_lane_map", {})

    device_id = _extract_scanner_device_id(topic)
    if device_id is not None:
        lane = scanner_map.get(device_id)
        if lane:
            return lane
        logger.warning(f"Scanner device '{device_id}' not found in scanner_lane_map")
        return None

    return None


def _handle_rich_tag(client: mqtt.Client, toolhead: str, payload: dict, topic: str) -> None:
    """
    Handles a rich-data NFC tag (OpenTag3D or spoolsense_scanner).

    Routes through the dispatcher to parse the tag data into a ScanEvent.
    Spoolman sync is best-effort — if it fails, activation continues from
    tag data alone. The two concerns are kept separate:

      - Activation path  : always runs from scan data
      - Enrichment path  : Spoolman sync, weight update, spool ID tracking
    """
    try:
        scan = detect_and_parse(payload, toolhead, topic)
        logger.info(f"Rich tag parsed: {scan.source} — {scan.brand_name} {scan.material_type} (UID: {scan.uid})")

        # Guard: scanner may report present=False or invalid data
        if not scan.present:
            logger.debug(f"Scanner reported no tag present on {toolhead}")
            return

        # UID-only ISO14443A tag (e.g. NTAG215) — no embedded filament data,
        # but we can still look the spool up in Spoolman via extra.nfc_id.
        if not scan.tag_data_valid and not scan.blank and scan.uid:
            uid = scan.uid.lower()
            logger.info(f"UID-only tag on {toolhead}: {uid} — looking up in Spoolman")
            spool = find_spool_by_nfc(uid)
            if spool:
                spool_id = spool["id"]
                filament = spool.get("filament", {})
                name = filament.get("name", "Unknown")
                color_hex = (filament.get("color_hex") or "FFFFFF").lstrip("#").upper()
                logger.info(f"Found spool for UID {uid}: {name} (ID: {spool_id})")
                if activate_spool(spool_id, toolhead):
                    app_state.active_spools[toolhead] = spool_id
                    if app_state.cfg["toolhead_mode"] == "afc":
                        publish_lock(toolhead, "lock")
                    remaining = spool.get("remaining_weight")
                    if remaining is not None and remaining <= app_state.cfg["low_spool_threshold"]:
                        logger.warning(f"Low spool: {name} ({remaining:.1f}g) on {toolhead}")
            else:
                logger.warning(f"No spool found in Spoolman for UID: {uid}")
            return

        if not scan.tag_data_valid:
            logger.warning(f"Scanner reported invalid tag data on {toolhead}")
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
        _activate_from_scan(toolhead, scan, spool_info=spool_info)

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
        logger.info(f"Connected to MQTT broker (Mode: {app_state.cfg['toolhead_mode']})")

        if not app_state.DISPATCHER_AVAILABLE:
            logger.error(
                "Rich-tag dispatcher is required but not available "
                "(adapters/ directory not found). Cannot process scanner payloads."
            )
            client.disconnect()
            return

        scanner_map = app_state.cfg.get("scanner_lane_map", {})
        if not scanner_map:
            logger.error(
                "scanner_lane_map is empty — no scanner topics to subscribe to. "
                "Add scanner device IDs to scanner_lane_map in config.yaml."
            )
            client.disconnect()
            return

        # Subscribe to spoolsense_scanner topics
        prefix = app_state.cfg.get("scanner_topic_prefix", "spoolsense")
        for device_id in scanner_map:
            topic = f"{prefix}/{device_id}/tag/state"
            client.subscribe(topic)
        logger.info(
            f"Subscribed to {len(scanner_map)} spoolsense_scanner(s): {', '.join(scanner_map.keys())}"
        )

        client.publish("spoolsense/middleware/online", "true", qos=1, retain=True)
        refresh_spool_cache()

        # Kick off the initial state sync based on our mode
        if app_state.cfg["toolhead_mode"] == "afc":
            sync_from_afc_file()
        else:
            app_state.cfg["klipper_var_path"] = discover_klipper_var_path()
            sync_from_klipper_vars()

            # Restart the file watcher now that we know the path
            if app_state.watcher:
                app_state.watcher.stop()
                app_state.watcher.join(timeout=2)
            app_state.watcher = start_watcher()
    else:
        logger.error(f"MQTT connection failed: {rc}")


def on_message(client: mqtt.Client, userdata: object, msg: mqtt.MQTTMessage) -> None:
    """
    Fires every time an MQTT message arrives on a subscribed topic.

    All payloads are rich data from spoolsense_scanner — JSON with tag data.
    Routes through the dispatcher to parse, syncs with Spoolman, then activates.
    The lane is resolved from the MQTT topic via scanner_lane_map.
    """
    try:
        payload: dict = json.loads(msg.payload.decode())
        topic: str = msg.topic

        # Resolve which lane/toolhead this message belongs to
        toolhead = _resolve_lane_from_topic(topic)
        if not toolhead:
            logger.warning(f"Could not resolve lane from topic: {topic}")
            return

        # If the lane is locked (already has a spool), ignore the scan
        if app_state.lane_locks.get(toolhead):
            logger.info(f"Ignoring scan on {toolhead} (locked)")
            return

        if not app_state.DISPATCHER_AVAILABLE:
            logger.warning("Rich-tag dispatcher not available — cannot process scanner payload")
            return

        _handle_rich_tag(client, toolhead, payload, topic)

    except Exception as e:
        logger.error(f"Message error: {e}")
