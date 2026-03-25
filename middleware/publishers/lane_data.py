"""
publishers/lane_data.py — Publish spool data to Moonraker's lane_data DB namespace.

Writes spool assignment data (color, material, temps, weight, spool_id) to
Moonraker's database so slicers like Orca Slicer can auto-populate tool
colors, materials, and temperatures.

Enable with: publish_lane_data: true in config.yaml

This publisher is opt-in and should only be enabled when AFC or Happy Hare
are NOT installed (they handle lane_data themselves). If both SpoolSense
and AFC write to lane_data, they will conflict.
"""
from __future__ import annotations

import logging
from typing import Any

import requests

from publishers.base import Publisher, SpoolEvent, Action

logger = logging.getLogger(__name__)


class LaneDataPublisher(Publisher):
    """Publishes spool data to Moonraker's lane_data DB namespace for slicer integration."""

    def __init__(self, config: dict) -> None:
        self._config = config
        self._moonraker_url: str = config.get("moonraker_url", "")
        self._db_url: str = f"{self._moonraker_url}/server/database/item" if self._moonraker_url else ""

    @property
    def name(self) -> str:
        return "lane_data"

    @property
    def primary(self) -> bool:
        return False

    def enabled(self, config: dict) -> bool:
        return bool(config.get("publish_lane_data", False)) and bool(config.get("moonraker_url"))

    def publish(self, event: SpoolEvent) -> bool:
        """Write spool data to Moonraker's lane_data namespace."""
        try:
            if not self._db_url:
                logger.error("[lane_data] Cannot publish — moonraker_url not configured")
                return False

            if not event.target:
                # afc_stage with no target yet — nothing to publish until lane loads
                return True

            # Resolve lane number from target (T0 → "0", lane1 → "1", etc.)
            lane_number = _extract_lane_number(event.target)

            # Build the lane_data payload matching AFC's format
            lane_data: dict[str, Any] = {
                "namespace": "lane_data",
                "key": event.target,
                "value": {
                    "color": f"#{event.color}" if event.color else "",
                    "material": event.material or "",
                    "nozzle_temp": _avg_temp(event.nozzle_temp_min, event.nozzle_temp_max),
                    "bed_temp": _avg_temp(event.bed_temp_min, event.bed_temp_max),
                    "scan_time": "",
                    "td": "",
                    "lane": lane_number,
                    "spool_id": event.spool_id,
                    "weight": event.weight or 0,
                },
            }

            response = requests.post(
                self._db_url,
                json=lane_data,
                timeout=5,
            )
            response.raise_for_status()
            logger.info(f"[lane_data] Published {event.target}: {event.material or 'unknown'} "
                        f"color={event.color or 'none'} weight={event.weight or 0}g")
            return True

        except Exception:
            logger.exception("[lane_data] Failed to publish")
            return False

    def teardown(self) -> None:
        """Clear lane_data on shutdown."""
        pass


def _extract_lane_number(target: str) -> str:
    """
    Extract a lane/tool number from target string.
    'T0' → '0', 'T12' → '12', 'lane1' → '1', 'lane3' → '3'
    """
    import re
    match = re.search(r"(\d+)$", target)
    return match.group(1) if match else "0"


def _avg_temp(temp_min: int | None, temp_max: int | None) -> int | str:
    """
    Average min/max temps for the lane_data format (single value).
    AFC stores a single nozzle_temp / bed_temp, not a range.
    """
    if temp_min is not None and temp_max is not None:
        return (temp_min + temp_max) // 2
    if temp_min is not None:
        return temp_min
    if temp_max is not None:
        return temp_max
    return ""
