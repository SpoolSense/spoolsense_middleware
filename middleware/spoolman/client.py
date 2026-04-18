"""
client.py — SpoolmanClient for spool lookup and enrichment.

Read-only interface to Spoolman. Looks up spools by NFC UID and enriches
tag data with Spoolman's color, material, and weight info. Does NOT create
or modify spools — the scanner handles all Spoolman writes, and Moonraker
handles filament usage tracking via sync_rate.
"""
import logging
import time
from typing import Optional

import requests

from state.models import SpoolInfo

logger = logging.getLogger(__name__)

CACHE_TTL = 3600  # seconds before forcing a full Spoolman re-sync


class SpoolmanClient:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip('/')
        self.cache = {}
        self._last_refresh = 0

    def _fetch_all_spools(self) -> None:
        """Pulls all active (non-archived) spools to build the NFC UID lookup cache."""
        try:
            # Only index active spools — archived spools with the same nfc_id
            # would overwrite the active entry and cause lookup failures (#49)
            response = requests.get(f"{self.base_url}/api/v1/spool?archived=false", timeout=5)
            response.raise_for_status()
            new_cache = {}
            for spool in response.json():
                nfc_id = spool.get("extra", {}).get("nfc_id", "").strip('"').lower()
                if nfc_id:
                    new_cache[nfc_id] = spool
            self.cache = new_cache
            self._last_refresh = time.time()
            logger.info(f"Spoolman cache refreshed: {len(self.cache)} spools indexed.")
        except Exception as e:
            logger.error(f"Failed to fetch Spoolman cache: {e}")

    def refresh(self) -> None:
        """Public wrapper around _fetch_all_spools for explicit cache priming."""
        self._fetch_all_spools()

    def get_spool_by_id(self, spool_id: int) -> Optional[dict]:
        """Fetch a single spool directly from Spoolman by ID. Returns None on failure."""
        try:
            response = requests.get(f"{self.base_url}/api/v1/spool/{spool_id}", timeout=5)
            response.raise_for_status()
            return response.json()
        except requests.RequestException:
            logger.exception("Failed to fetch spool %s", spool_id)
            return None

    def find_by_nfc(self, nfc_uid: str) -> Optional[dict]:
        """Looks up a spool by NFC UID, with TTL-based cache and single forced refresh on miss."""
        uid_lower = nfc_uid.lower()

        if time.time() - self._last_refresh > CACHE_TTL:
            self._fetch_all_spools()

        if uid_lower not in self.cache:
            # Could be a newly registered spool — force one refresh before giving up
            logger.info(f"UID {nfc_uid} not in cache, forcing refresh...")
            self._fetch_all_spools()

        return self.cache.get(uid_lower)

    def sync_spool_from_scan(self, scan, prefer_tag: bool = True) -> Optional[SpoolInfo]:
        """
        Look up the scanned spool in Spoolman and enrich tag data.

        Returns SpoolInfo with spoolman_id and enriched fields, or None if the
        spool isn't in Spoolman yet (scanner will create it on its side).
        """
        tag_spool = SpoolInfo(
            spool_uid=scan.uid,
            source=scan.source,
            brand=scan.brand_name,
            material_type=scan.material_type,
            material_name=scan.material_name,
            color_name=scan.color_name,
            color_hex=scan.color_hex,
            diameter_mm=scan.diameter_mm,
            nozzle_temp_min_c=scan.nozzle_temp_min_c,
            nozzle_temp_max_c=scan.nozzle_temp_max_c,
            bed_temp_min_c=scan.bed_temp_min_c,
            bed_temp_max_c=scan.bed_temp_max_c,
            full_weight_g=scan.full_weight_g,
            remaining_weight_g=scan.remaining_weight_g,
            remaining_length_mm=scan.remaining_length_mm,
        )

        if not tag_spool.spool_uid:
            logger.warning("ScanEvent has no UID — cannot look up in Spoolman")
            return None

        existing = self.find_by_nfc(tag_spool.spool_uid)

        if not existing:
            # Spool not in Spoolman yet — scanner handles creation.
            # Return None so activation runs in tag-only mode.
            logger.info(f"NFC {tag_spool.spool_uid} not in Spoolman — running tag-only (scanner creates)")
            return None

        # Enrich tag data with Spoolman's stored values
        spoolman_id = existing["id"]
        filament = existing.get("filament", {})
        tag_spool.spoolman_id = spoolman_id

        # Spoolman's color always wins if set — a human chose it deliberately
        spoolman_color = filament.get("color_hex")
        if spoolman_color:
            logger.info(f"Using Spoolman color #{spoolman_color} over tag color '{tag_spool.color_name or tag_spool.color_hex}'")
            tag_spool.color_hex = spoolman_color

        if prefer_tag:
            # Tag weight is source of truth — don't write to Spoolman,
            # just use the tag value. Moonraker handles weight sync.
            tag_spool.source = "merged (tag preferred)"
        else:
            # Spoolman data wins for everything
            tag_spool.remaining_weight_g = existing.get("remaining_weight", tag_spool.remaining_weight_g)
            if spoolman_color is not None:
                tag_spool.color_hex = spoolman_color
            tag_spool.material_type     = filament.get("material", tag_spool.material_type)
            tag_spool.material_name     = filament.get("name", tag_spool.material_name)
            tag_spool.brand             = filament.get("vendor", {}).get("name", tag_spool.brand)
            tag_spool.diameter_mm       = filament.get("diameter", tag_spool.diameter_mm)
            tag_spool.nozzle_temp_min_c = filament.get("settings_extruder_temp", tag_spool.nozzle_temp_min_c)
            tag_spool.bed_temp_min_c    = filament.get("settings_bed_temp", tag_spool.bed_temp_min_c)
            tag_spool.source = "merged (spoolman preferred)"

        return tag_spool
