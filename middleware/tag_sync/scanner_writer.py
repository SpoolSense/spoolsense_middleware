"""
tag_sync/scanner_writer.py — MQTT interface to openprinttag_scanner.

Publishes write commands to the scanner firmware via MQTT.
Phase 1: fire-and-forget. No response correlation or retries.

Command topic format:
    openprinttag/<deviceId>/cmd/<command>/<uid>

Response topic (not consumed in Phase 1, for future observability):
    openprinttag/<deviceId>/cmd/response
"""

import json
import logging
from tag_sync.policy import TagWritePlan

logger = logging.getLogger(__name__)


def execute(plan: TagWritePlan, mqtt_client) -> None:
    """
    Publishes a write command to the openprinttag_scanner firmware.

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
    topic = f"openprinttag/{plan.device_id}/cmd/{plan.command}/{plan.uid}"
    payload = json.dumps(plan.payload)

    try:
        mqtt_client.publish(topic, payload, qos=1)
        logger.info(
            "Tag write published: topic=%s payload=%s reason=%s",
            topic,
            payload,
            plan.reason,
        )
    except Exception:
        logger.exception(
            "Tag write failed (non-blocking): topic=%s payload=%s",
            topic,
            payload,
        )
