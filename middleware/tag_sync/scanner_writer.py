"""
tag_sync/scanner_writer.py — MQTT interface to spoolsense_scanner.

Publishes write commands to the scanner firmware via MQTT.
Phase 1: fire-and-forget. No response correlation or retries.

Command topic format:
    spoolsense/<deviceId>/cmd/<command>/<uid>

Response topic (not consumed in Phase 1, for future observability):
    spoolsense/<deviceId>/cmd/response
"""

import json
import logging
import time
import paho.mqtt.client as mqtt
from tag_sync.policy import TagWritePlan
import app_state

logger = logging.getLogger(__name__)


def _release_cooldown_claim(uid: str) -> None:
    """Remove the optimistic cooldown claim so a retry isn't blocked."""
    with app_state.state_lock:
        app_state.tag_write_timestamps.pop(uid, None)


def execute(plan: TagWritePlan, mqtt_client) -> None:
    """
    Publishes a write command to the spoolsense_scanner firmware.

    Phase 1 behavior:
      - Fire-and-forget: does not wait for or consume cmd/response
      - Failures are logged but never raise — writeback must not block activation
      - One command per call — do not batch

    The scanner firmware handles:
      - UID validation (rejects if tag swapped between command and execution)
      - Write queueing (max 8 pending)
      - remaining_g → consumed_weight conversion
      - Aux-region write with full-write fallback
      - Verification retries

    Args:
        plan:        TagWritePlan from build_write_plan()
        mqtt_client: Active paho MQTT client instance
    """
    if not plan.device_id or not plan.uid or not plan.command:
        logger.warning(
            "Tag write skipped — invalid plan: device_id=%r uid=%r command=%r",
            plan.device_id, plan.uid, plan.command,
        )
        return

    # Sanitize MQTT topic segments — NFC UIDs are normally hex-only, but a
    # crafted payload containing /, +, or # could break MQTT topic routing.
    safe_device = plan.device_id.replace("/", "").replace("+", "").replace("#", "")
    safe_uid = plan.uid.replace("/", "").replace("+", "").replace("#", "")
    topic = f"spoolsense/{safe_device}/cmd/{plan.command}/{safe_uid}"
    payload = json.dumps(plan.payload)

    try:
        result = mqtt_client.publish(topic, payload, qos=1)
        if result.rc != mqtt.MQTT_ERR_SUCCESS:
            logger.warning(
                "Tag write publish failed (rc=%d): topic=%s payload=%s",
                result.rc, topic, payload,
            )
            # Release optimistic cooldown claim so a retry isn't blocked.
            _release_cooldown_claim(plan.uid)
        else:
            logger.info(
                "Tag write published: topic=%s payload=%s reason=%s",
                topic,
                payload,
                plan.reason,
            )
            # Refresh cooldown timestamp from actual publish time.
            # build_write_plan() claims the slot optimistically; this extends
            # the window so the cooldown starts from the real publish moment.
            with app_state.state_lock:
                app_state.tag_write_timestamps[plan.uid] = time.monotonic()
    except Exception:
        logger.exception(
            "Tag write failed (non-blocking): topic=%s payload=%s",
            topic,
            payload,
        )
        _release_cooldown_claim(plan.uid)
