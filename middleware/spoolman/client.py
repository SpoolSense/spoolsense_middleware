import requests
import logging
import time
from typing import Optional
from state.models import SpoolInfo

CACHE_TTL = 3600  # Seconds before forcing a full Spoolman re-sync


class SpoolmanClient:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip('/')
        self.cache = {}
        self._last_refresh = 0

    def _fetch_all_spools(self):
        """Pulls all spools to find NFC UIDs stored in the 'extra' fields."""
        try:
            response = requests.get(f"{self.base_url}/api/v1/spool", timeout=5)
            response.raise_for_status()
            self.cache = {}
            for spool in response.json():
                nfc_id = spool.get("extra", {}).get("nfc_id", "").strip('"').lower()
                if nfc_id:
                    self.cache[nfc_id] = spool
            self._last_refresh = time.time()
            logging.info(f"Spoolman cache refreshed: {len(self.cache)} spools indexed.")
        except Exception as e:
            logging.error(f"Failed to fetch Spoolman cache: {e}")

    def find_by_nfc(self, nfc_uid: str) -> Optional[dict]:
        """Looks up a spool by NFC UID, with TTL-based cache and single forced refresh on miss."""
        uid_lower = nfc_uid.lower()

        if time.time() - self._last_refresh > CACHE_TTL:
            self._fetch_all_spools()

        if uid_lower not in self.cache:
            # Could be a newly registered spool — force one refresh before giving up
            logging.info(f"UID {nfc_uid} not in cache, forcing refresh...")
            self._fetch_all_spools()

        return self.cache.get(uid_lower)

    def sync_spool(self, tag_spool: SpoolInfo, prefer_tag: bool = True) -> SpoolInfo:
        """
        The Merger: Takes a parsed SpoolInfo from a tag, checks Spoolman, and syncs them.

        If the spool is not in Spoolman yet, it creates a new entry and writes the
        NFC UID back so future scans find it.

        If the spool already exists:
          prefer_tag=True  — Tag data wins for weight/material. But Spoolman's
                             color_hex always takes priority if set, since a human
                             likely chose it deliberately (vs our best-guess
                             conversion from a color name like "Galaxy Black").
          prefer_tag=False — Spoolman data wins for everything.

        Always returns a SpoolInfo with spoolman_id populated.
        """
        existing = self.find_by_nfc(tag_spool.spool_uid)

        if not existing:
            logging.info(f"NFC {tag_spool.spool_uid} not in Spoolman. Creating new spool...")
            return self._create_spool_from_tag(tag_spool)

        spoolman_id = existing["id"]
        filament = existing.get("filament", {})
        tag_spool.spoolman_id = spoolman_id

        # Spoolman's color always wins if it has one set — a human chose it
        # deliberately, and it's likely more accurate than our color name → hex guess
        spoolman_color = filament.get("color_hex")
        if spoolman_color:
            logging.info(f"Using Spoolman color #{spoolman_color} over tag color '{tag_spool.color_name or tag_spool.color_hex}'")
            tag_spool.color_hex = spoolman_color

        if prefer_tag:
            # Tag is the source of truth for weight. Push tag weight to Spoolman.
            logging.info(f"Updating Spoolman ID {spoolman_id} with fresh tag data...")
            nominal_g = filament.get("weight")
            self._update_spoolman_weight(spoolman_id, tag_spool.remaining_weight_g, nominal_g)
            tag_spool.source = "merged (tag preferred)"
        else:
            # Spoolman is the source of truth. Pull its data into SpoolInfo.
            logging.info(f"Using existing Spoolman data for ID {spoolman_id}.")
            tag_spool.remaining_weight_g = existing.get("remaining_weight", tag_spool.remaining_weight_g)
            tag_spool.color_hex         = spoolman_color or tag_spool.color_hex
            tag_spool.material_type     = filament.get("material", tag_spool.material_type)
            tag_spool.material_name     = filament.get("name", tag_spool.material_name)
            tag_spool.brand             = filament.get("vendor", {}).get("name", tag_spool.brand)
            tag_spool.diameter_mm       = filament.get("diameter", tag_spool.diameter_mm)
            tag_spool.nozzle_temp_min_c = filament.get("settings_extruder_temp", tag_spool.nozzle_temp_min_c)
            tag_spool.bed_temp_min_c    = filament.get("settings_bed_temp", tag_spool.bed_temp_min_c)
            tag_spool.source = "merged (spoolman preferred)"

        return tag_spool

    def _create_spool_from_tag(self, tag_spool: SpoolInfo) -> SpoolInfo:
        """
        Creates a new filament and spool in Spoolman based on tag data, then writes
        the NFC UID back to Spoolman's extra fields so future scans find it.

        Note: Full vendor/filament deduplication (checking if the brand+material
        already exists before creating) is not yet implemented. For now this always
        creates a new filament entry.
        """
        logging.warning("Auto-creation of Spoolman entries from tags is not yet fully implemented.")
        # TODO: Check if vendor already exists, create if not, then link filament.
        # TODO: POST to /api/v1/filament, then POST to /api/v1/spool with filament_id.
        # Placeholder: pretend Spoolman assigned ID 99.
        tag_spool.spoolman_id = 99
        tag_spool.source = "openprinttag"

        # Write the NFC UID back to Spoolman so the cache can find it on the next scan
        self._write_nfc_id(tag_spool.spoolman_id, tag_spool.spool_uid)
        return tag_spool

    def _update_spoolman_weight(self, spoolman_id: int, remaining_g: Optional[float], nominal_g: Optional[float]):
        """
        Updates the consumed weight in Spoolman.

        Spoolman stores 'used_weight', not 'remaining_weight', so we calculate:
            used_weight = nominal_g (filament["weight"]) - remaining_g
        Both values are required — if either is missing we skip the update.
        """
        if remaining_g is None or nominal_g is None:
            logging.debug(f"Skipping weight update for spool {spoolman_id}: missing remaining or nominal weight.")
            return
        used_weight = max(0.0, nominal_g - remaining_g)
        try:
            requests.patch(
                f"{self.base_url}/api/v1/spool/{spoolman_id}",
                json={"used_weight": used_weight},
                timeout=5
            ).raise_for_status()
            logging.info(f"Spoolman spool {spoolman_id}: used_weight set to {used_weight:.1f}g")
        except Exception as e:
            logging.error(f"Failed to update weight for spool {spoolman_id}: {e}")

    def _write_nfc_id(self, spoolman_id: int, nfc_uid: str):
        """
        Writes the NFC UID into Spoolman's extra fields so the spool can be found
        by NFC lookup on future scans.
        """
        try:
            requests.patch(
                f"{self.base_url}/api/v1/spool/{spoolman_id}",
                json={"extra": {"nfc_id": nfc_uid.lower()}},
                timeout=5
            ).raise_for_status()
            logging.info(f"Wrote NFC UID {nfc_uid} to Spoolman spool {spoolman_id}.")
            # Update the local cache so we don't need a full refresh
            self.cache[nfc_uid.lower()] = {"id": spoolman_id}
        except Exception as e:
            logging.error(f"Failed to write NFC UID to Spoolman spool {spoolman_id}: {e}")
