"""
tag_sync/policy.py — Write decision logic for NFC tag writeback.

Decides whether a tag write should occur and what should be written.
Uses app_state for per-UID write cooldown tracking (issue #21).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, Literal, Optional
import app_state

logger = logging.getLogger(__name__)


@dataclass
class TagWritePlan:
    device_id: str                                      # Scanner deviceId extracted from the MQTT scan topic
                                                        # (spoolsense/<deviceId>/...)
    uid: str                                            # NFC tag UID to target
    command: Literal["update_remaining", "write_tag"]   # Allowed write commands
    payload: Dict[str, Any]                              # Command payload
    reason: Optional[str] = None                        # Optional — logged when the write is dispatched


def should_write_remaining(
    tag_remaining: float | None,
    spoolman_remaining: float | None,
) -> bool:
    """
    Returns True if the tag's remaining weight should be updated.

    Write rules (downward-only — tags never move up):
      - Spoolman has no remaining data → do not write (no authoritative value)
      - Tag has no remaining data      → write (tag is missing data)
      - Spoolman remaining < tag remaining → write (tag is stale)
      - Otherwise                          → do not write

    This ensures:
      - Tags only move downward in remaining filament
      - Accidental overwrites from stale or incorrect Spoolman values are prevented
    """
    if spoolman_remaining is None:
        return False
    if tag_remaining is None:
        return True
    return spoolman_remaining < tag_remaining


def build_write_plan(
    scan: "ScanEvent",
    spool_info: "SpoolInfo | None",
    device_id: str | None,
) -> TagWritePlan | None:
    """
    Decides whether a tag write is needed and returns a TagWritePlan, or None.

    Only produces a plan when:
      - device_id is known (scanner topic was a spoolsense_scanner topic)
      - scan.uid is present (needed to target the correct tag)
      - should_write_remaining() returns True

    Args:
        scan:       ScanEvent from the dispatcher
        spool_info: SpoolInfo from Spoolman sync, or None if sync failed
        device_id:  Scanner deviceId extracted from the MQTT topic, or None
                    for PN532/ESPHome scans (which don't support writeback)

    Returns:
        TagWritePlan if a write should occur, None otherwise.
    """
    if not device_id:
        # PN532/ESPHome path — writeback not supported
        return None

    if not scan.uid:
        # No UID means we can't target the tag
        return None

    # Cooldown — prevent write loops from our own tag state republishes.
    now = time.monotonic()
    with app_state.state_lock:
        last_write = app_state.tag_write_timestamps.get(scan.uid)
        if last_write is not None:
            elapsed = now - last_write
            if elapsed < app_state.WRITE_COOLDOWN_SECONDS:
                logger.debug(
                    "build_write_plan: skipping uid=%s — wrote %.1fs ago (cooldown %ds)",
                    scan.uid, elapsed, app_state.WRITE_COOLDOWN_SECONDS,
                )
                return None

    spoolman_remaining = spool_info.remaining_weight_g if spool_info else None
    tag_remaining = scan.remaining_weight_g

    if not should_write_remaining(tag_remaining, spoolman_remaining):
        return None

    reason = (
        f"tag remaining={tag_remaining}g, spoolman remaining={spoolman_remaining}g"
        if tag_remaining is not None
        else f"tag missing remaining, spoolman remaining={spoolman_remaining}g"
    )

    if spoolman_remaining < 0:
        logger.warning(
            "build_write_plan: spoolman remaining_g is negative (%.1f) for uid=%s — skipping write",
            spoolman_remaining, scan.uid,
        )
        return None

    # Claim cooldown slot only when we're actually going to produce a write plan.
    # Lazy prune expired entries when dict is large.
    with app_state.state_lock:
        app_state.tag_write_timestamps[scan.uid] = now
        if len(app_state.tag_write_timestamps) > 50:
            expired = [k for k, v in app_state.tag_write_timestamps.items()
                       if now - v > app_state.WRITE_COOLDOWN_SECONDS]
            for k in expired:
                del app_state.tag_write_timestamps[k]

    return TagWritePlan(
        device_id=device_id,
        uid=scan.uid,
        command="update_remaining",
        payload={"remaining_g": spoolman_remaining},
        reason=reason,
    )
