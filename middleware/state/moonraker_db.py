"""
moonraker_db.py — Moonraker database namespace access.

Reads and writes spool assignments from Moonraker's key-value database
(lane_data namespace). Used for slicer integration (Orca Slicer tool colors).
"""
import requests
import logging

logger = logging.getLogger(__name__)
from .models import SpoolInfo, SpoolAssignment

class MoonrakerDB:
    def __init__(self, base_url: str):
        """Initialize with the URL of your Moonraker instance (e.g., http://localhost:7125)"""
        self.base_url = base_url.rstrip('/')
        self.namespace = "nfc_spoolman"

    def save_spool(self, spool: SpoolInfo) -> bool:
        """Saves a normalized SpoolInfo object to the database."""
        url = f"{self.base_url}/server/database/item"
        payload = {
            "namespace": self.namespace,
            "key": f"spools.{spool.spool_uid}",
            "value": spool.to_dict()
        }
        try:
            response = requests.post(url, json=payload, timeout=5)
            response.raise_for_status()
            logger.info(f"Saved spool {spool.spool_uid} to Moonraker DB.")
            return True
        except Exception as e:
            logger.error(f"Failed to save spool to Moonraker DB: {e}")
            return False

    def save_assignment(self, assignment: SpoolAssignment) -> bool:
        """Saves a SpoolAssignment to the database."""
        url = f"{self.base_url}/server/database/item"
        payload = {
            "namespace": self.namespace,
            "key": f"assignments.{assignment.target_type}.{assignment.target_id}",
            "value": assignment.to_dict()
        }
        try:
            response = requests.post(url, json=payload, timeout=5)
            response.raise_for_status()
            logger.info(f"Saved assignment {assignment.target_id} to Moonraker DB.")
            return True
        except Exception as e:
            logger.error(f"Failed to save assignment to Moonraker DB: {e}")
            return False