"""
publishers/klipper.py — Klipper/Moonraker output publisher.

Translates SpoolEvent objects into Klipper gcode commands and Moonraker REST
calls. This is the primary publisher for Klipper-based 3D printers.

Extracted from activation.py. All Moonraker HTTP calls, gcode scripts, and
input validation for gcode safety live here.

Actions handled:
    afc_stage       → SET_NEXT_SPOOL_ID SPOOL_ID={id}  (staging only, no lane)
    afc_lane        → SET_SPOOL_ID LANE={target} SPOOL_ID={id}
                      + _send_afc_lane_data() in tag-only mode
    toolhead        → POST /server/spoolman/spool_id + SAVE_VARIABLE
                      + _send_toolhead_tag_data() in tag-only mode
    toolhead_stage  → log staging only (actual assignment handled by
                      toolchanger_status.py on tool pickup)
    unknown         → no-op, returns True (forward-compatible)
"""
from __future__ import annotations

import logging
import re

import requests

from publishers.base import Action, Publisher, SpoolEvent

logger = logging.getLogger(__name__)


def _validate_color_hex(color_hex: str) -> str | None:
    """Return the normalized 6-digit uppercase hex string, or None if invalid."""
    stripped = color_hex.lstrip("#").upper()
    if re.fullmatch(r"[A-F0-9]{6}", stripped):
        return stripped
    return None


def _validate_material(material: str) -> bool:
    """Return True only if material contains safe characters and is a reasonable length."""
    return bool(material) and len(material) <= 50 and bool(re.fullmatch(r"[A-Za-z0-9_ +.-]{1,50}", material))


# Black (000000) can't be displayed on an LED — use dim white instead
# so the user can tell a spool is scanned vs no spool at all.
_LED_BLACK_SUBSTITUTE = "333333"


def display_spoolcolor(color_hex: str) -> str | None:
    """Normalize a spool color for display (LED, gcode variable). Returns 6-digit hex or None if empty/invalid."""
    if not color_hex:
        return None
    safe = _validate_color_hex(color_hex)
    if safe is None:
        return None
    if safe == "000000":
        return _LED_BLACK_SUBSTITUTE
    return safe


def _send_gcode(moonraker: str, script: str) -> None:
    """
    POST a single gcode script to Moonraker.

    Raises on HTTP error or connection failure. Callers are responsible for
    catching and handling exceptions.
    """
    requests.post(
        f"{moonraker}/printer/gcode/script",
        json={"script": script},
        timeout=5,
    ).raise_for_status()


def _send_afc_lane_data(
    moonraker: str,
    toolhead: str,
    color_hex: str,
    material: str,
    remaining_g: float | None,
) -> None:
    """
    Send filament data directly to AFC lane via Klipper gcode commands.

    Used when Spoolman is not available — provides AFC with color, material,
    and weight from tag data so LEDs and lane info work without Spoolman.
    Each command is independent — if one fails, the others still run.
    """
    if not moonraker:
        return

    spool_color = display_spoolcolor(color_hex)
    if spool_color is not None:
        try:
            _send_gcode(moonraker, f"SET_COLOR LANE={toolhead} COLOR={spool_color}")
            logger.info(f"[afc] SET_COLOR {toolhead} = {spool_color}")
        except Exception:
            logger.exception(f"[afc] SET_COLOR failed for {toolhead}")

    if material and material != "Unknown":
        if not _validate_material(material):
            logger.warning(f"[afc] Skipping SET_MATERIAL for {toolhead} — invalid material: {material!r}")
        else:
            try:
                safe_material = material.replace(" ", "_")
                _send_gcode(moonraker, f"SET_MATERIAL LANE={toolhead} MATERIAL={safe_material}")
                logger.info(f"[afc] SET_MATERIAL {toolhead} = {material}")
            except Exception:
                logger.exception(f"[afc] SET_MATERIAL failed for {toolhead}")

    if remaining_g is not None and remaining_g > 0:
        try:
            _send_gcode(moonraker, f"SET_WEIGHT LANE={toolhead} WEIGHT={remaining_g:.0f}")
            logger.info(f"[afc] SET_WEIGHT {toolhead} = {remaining_g:.0f}g")
        except Exception:
            logger.exception(f"[afc] SET_WEIGHT failed for {toolhead}")


def _send_toolhead_tag_data(
    moonraker: str,
    target: str,
    color_hex: str,
    material: str,
    remaining_g: float | None,
) -> None:
    """
    Send tag data directly to a toolhead via Klipper gcode variables.

    Used when Spoolman is not available — provides the toolhead macro with
    color from tag data so slicer integration still works without Spoolman.
    """
    if not moonraker or not target:
        return

    spool_color = display_spoolcolor(color_hex)
    if spool_color is not None:
        try:
            _send_gcode(
                moonraker,
                f"SET_GCODE_VARIABLE MACRO={target} VARIABLE=color VALUE=\"'{spool_color}'\"",
            )
            logger.info(f"[toolhead] SET_GCODE_VARIABLE {target} color='{spool_color}'")
        except Exception:
            logger.exception(f"[toolhead] SET_GCODE_VARIABLE color failed for {target}")

    if material and material != "Unknown":
        logger.info(f"[toolhead] {target} material: {material}")

    if remaining_g is not None and remaining_g > 0:
        logger.info(f"[toolhead] {target} weight: {remaining_g:.0f}g")


def _publish_toolhead_lane_data(moonraker: str, event: SpoolEvent) -> None:
    """
    Write spool data to Moonraker's lane_data database for a toolhead.

    AFC writes lane_data for its own lanes internally. For direct toolhead
    assignments (T0, T1, etc.), there is no AFC — so we write to the same
    namespace so Orca Slicer and other slicers see the tool's filament info.
    """
    if not moonraker or not event.target:
        return

    color = ""
    if event.color:
        safe = _validate_color_hex(event.color)
        if safe:
            color = f"#{safe}"

    material = ""
    if event.material and event.material != "Unknown":
        material = event.material.replace(" ", "_")

    # Extract tool number from target (e.g., "T0" → "0") for Orca Slicer lane sync
    lane_match = re.match(r"[Tt](\d+)", event.target)
    lane = lane_match.group(1) if lane_match else event.target

    value = {
        "color": color,
        "material": material,
        "weight": round(event.weight) if event.weight else 0,
        "nozzle_temp": event.nozzle_temp_max,
        "bed_temp": event.bed_temp_max,
        "spool_id": event.spool_id,
        "lane": lane,
    }

    try:
        requests.post(
            f"{moonraker}/server/database/item",
            json={"namespace": "lane_data", "key": event.target, "value": value},
            timeout=5,
        ).raise_for_status()
        logger.info(f"[toolhead] Published lane_data for {event.target}: {material} {color}")
    except Exception:
        logger.exception(f"[toolhead] Failed to publish lane_data for {event.target}")


class KlipperPublisher(Publisher):
    """
    Primary publisher for Klipper/Moonraker printers.

    Translates SpoolEvent objects into gcode commands delivered via Moonraker's
    /printer/gcode/script endpoint, and into Moonraker REST calls for Spoolman
    spool activation.

    Enabled when moonraker_url is present in config. Disabled otherwise (e.g.,
    future non-Klipper printer support).
    """

    def __init__(self, config: dict) -> None:
        self._config = config

    @property
    def name(self) -> str:
        return "klipper"

    @property
    def primary(self) -> bool:
        return True

    def enabled(self, config: dict) -> bool:
        """Active when moonraker_url is configured."""
        return bool(config.get("moonraker_url", ""))

    def publish(self, event: SpoolEvent) -> bool:
        """
        Route the SpoolEvent to the appropriate Klipper/Moonraker commands.

        Returns True on success, False on any failure. Never raises.
        """
        moonraker = self._config.get("moonraker_url", "")
        if not moonraker:
            logger.error("KlipperPublisher: moonraker_url not configured")
            return False

        try:
            return self._dispatch(moonraker, event)
        except Exception:
            logger.exception(
                "KlipperPublisher: unhandled error (action=%s target=%s)",
                event.action,
                event.target,
            )
            return False

    def _dispatch(self, moonraker: str, event: SpoolEvent) -> bool:
        """Branch on action type and execute the appropriate Klipper commands."""
        action = event.action

        if action == Action.AFC_STAGE:
            return self._handle_afc_stage(moonraker, event)

        elif action == Action.AFC_LANE:
            return self._handle_afc_lane(moonraker, event)

        elif action == Action.TOOLHEAD:
            return self._handle_toolhead(moonraker, event)

        elif action == Action.TOOLHEAD_STAGE:
            # Shared scanner — actual assignment is handled by toolchanger_status.py
            # on tool pickup. Nothing to send to Klipper at scan time.
            logger.info(f"[toolhead_stage] Staged spool {event.spool_id} for next tool pickup")
            return True

        else:
            # Forward-compatible: unknown actions are a no-op success so future
            # action types added in new PRs don't break existing publishers.
            logger.warning("KlipperPublisher: unknown action '%s' — skipping (no-op)", action)
            return True

    def _handle_afc_stage(self, moonraker: str, event: SpoolEvent) -> bool:
        """POST SET_NEXT_SPOOL_ID for afc_stage (only when spool_id is available)."""
        if event.spool_id is None:
            # tag-only mode — nothing for Klipper to do at scan time
            return True

        _send_gcode(moonraker, f"SET_NEXT_SPOOL_ID SPOOL_ID={event.spool_id}")
        logger.info(f"[afc_stage] Staged spool {event.spool_id} for next AFC load")
        return True

    def _handle_afc_lane(self, moonraker: str, event: SpoolEvent) -> bool:
        """
        Assign spool to AFC lane.

        Spoolman path: SET_SPOOL_ID LANE={target} SPOOL_ID={id}
        Tag-only path: _send_afc_lane_data() (color, material, weight)
        """
        if not event.target:
            logger.error("KlipperPublisher [afc_lane]: missing target")
            return False

        if not event.tag_only and event.spool_id is not None:
            requests.post(
                f"{moonraker}/printer/gcode/script",
                json={"script": f"SET_SPOOL_ID LANE={event.target} SPOOL_ID={event.spool_id}"},
                timeout=5,
            ).raise_for_status()
            logger.info(f"[afc_lane] Set spool {event.spool_id} on {event.target} via AFC")
        else:
            _send_afc_lane_data(
                moonraker,
                event.target,
                event.color or "",
                event.material or "",
                event.weight,
            )

        return True

    def _handle_toolhead(self, moonraker: str, event: SpoolEvent) -> bool:
        """
        Activate spool on a specific toolhead.

        Spoolman path: POST /server/spoolman/spool_id + SAVE_VARIABLE + lane_data
        Tag-only path: _send_toolhead_tag_data() (color via SET_GCODE_VARIABLE) + lane_data
        """
        if not event.target:
            logger.error("KlipperPublisher [toolhead]: missing target")
            return False

        if not event.tag_only and event.spool_id is not None:
            requests.post(
                f"{moonraker}/server/spoolman/spool_id",
                json={"spool_id": event.spool_id},
                timeout=5,
            ).raise_for_status()
            try:
                requests.post(
                    f"{moonraker}/printer/gcode/script",
                    json={"script": f"SAVE_VARIABLE VARIABLE={event.target.lower()}_spool_id VALUE={event.spool_id}"},
                    timeout=5,
                ).raise_for_status()
            except Exception:
                # Rollback: revert Spoolman to prevent orphaned spool_id (#15)
                logger.error(
                    "[toolhead] SAVE_VARIABLE failed for spool %s on %s — rolling back Spoolman",
                    event.spool_id, event.target,
                )
                try:
                    requests.post(
                        f"{moonraker}/server/spoolman/spool_id",
                        json={"spool_id": 0},
                        timeout=5,
                    ).raise_for_status()
                except Exception:
                    logger.exception("[toolhead] Rollback also failed — Spoolman may have stale spool_id")
                raise
            logger.info(f"[toolhead] Activated spool {event.spool_id} on {event.target}")
        else:
            _send_toolhead_tag_data(
                moonraker,
                event.target,
                event.color or "",
                event.material or "",
                event.weight,
            )

        # Write spool data to Moonraker's lane_data database so Orca Slicer
        # and other slicers can auto-populate tool info. AFC handles this
        # internally for its lanes; for direct toolhead assignments we must
        # write it ourselves.
        if self._config.get("publish_lane_data", False):
            _publish_toolhead_lane_data(moonraker, event)

        return True
