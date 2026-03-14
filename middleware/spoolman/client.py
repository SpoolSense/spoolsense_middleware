import logging
import time
from typing import Optional

import requests

from state.models import SpoolInfo

logger = logging.getLogger(__name__)

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
            logger.info(f"Spoolman cache refreshed: {len(self.cache)} spools indexed.")
        except Exception as e:
            logger.error(f"Failed to fetch Spoolman cache: {e}")

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
        Bridge between the new ScanEvent model and Spoolman sync.

        Takes a ScanEvent from the dispatcher, converts it to a SpoolInfo,
        then runs the standard sync_spool merge logic.

        Args:
            scan: A ScanEvent from the dispatcher.
            prefer_tag: If True, tag weight wins. Spoolman color always wins if set.

        Returns:
            SpoolInfo with spoolman_id populated, or None if sync failed.
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
            logger.warning("ScanEvent has no UID — cannot sync with Spoolman")
            return None

        return self.sync_spool(tag_spool, prefer_tag=prefer_tag)

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
            logger.info(f"NFC {tag_spool.spool_uid} not in Spoolman. Creating new spool...")
            return self._create_spool_from_tag(tag_spool)

        spoolman_id = existing["id"]
        filament = existing.get("filament", {})
        tag_spool.spoolman_id = spoolman_id

        # Spoolman's color always wins if it has one set — a human chose it
        # deliberately, and it's likely more accurate than our color name → hex guess
        spoolman_color = filament.get("color_hex")
        if spoolman_color:
            logger.info(f"Using Spoolman color #{spoolman_color} over tag color '{tag_spool.color_name or tag_spool.color_hex}'")
            tag_spool.color_hex = spoolman_color

        if prefer_tag:
            # Tag is the source of truth for weight. Push tag weight to Spoolman.
            logger.info(f"Updating Spoolman ID {spoolman_id} with fresh tag data...")
            nominal_g = filament.get("weight")
            self._update_spoolman_weight(spoolman_id, tag_spool.remaining_weight_g, nominal_g)
            tag_spool.source = "merged (tag preferred)"
        else:
            # Spoolman is the source of truth. Pull its data into SpoolInfo.
            logger.info(f"Using existing Spoolman data for ID {spoolman_id}.")
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

    def _create_spool_from_tag(self, tag_spool: SpoolInfo) -> SpoolInfo:
        """
        Creates a vendor (if needed), filament (if needed), and spool in Spoolman
        based on tag data, then writes the NFC UID back so future scans find it.

        Deduplication strategy:
          - Vendor: matched case-insensitively by name. Created if not found.
          - Filament: matched by vendor_id + material + color_hex + name (all four
            must match). Created if not found.
          - Spool: always created fresh — a new physical spool is a new Spoolman entry.
        """
        # --- Vendor ---
        vendor_name = tag_spool.brand or "Unknown"
        vendor = self._get_vendor_by_name(vendor_name)
        if vendor is None:
            logger.info(f"Vendor '{vendor_name}' not found in Spoolman — creating.")
            vendor = self._create_vendor(vendor_name)
        vendor_id = vendor["id"]

        # --- Filament ---
        filament = self._get_filament(
            vendor_id=vendor_id,
            material=tag_spool.material_type or "",
            color_hex=tag_spool.color_hex or "",
            name=tag_spool.material_name,
        )
        if filament is None:
            logger.info(f"No matching filament found for vendor {vendor_id} / "
                        f"{tag_spool.material_type} / {tag_spool.color_hex} — creating.")
            filament = self._create_filament(
                vendor_id=vendor_id,
                material=tag_spool.material_type or "",
                color_hex=tag_spool.color_hex or "",
                name=tag_spool.material_name,
                diameter=tag_spool.diameter_mm,
                density=getattr(tag_spool, "density", None),
            )
        filament_id = filament["id"]

        # --- Spool ---
        spool = self._create_spool(
            filament_id=filament_id,
            weight=tag_spool.full_weight_g,
        )
        tag_spool.spoolman_id = spool["id"]
        tag_spool.source = "created"

        # Write NFC UID back so the cache finds it on the next scan
        self._write_nfc_id(tag_spool.spoolman_id, tag_spool.spool_uid)
        logger.info(f"Created Spoolman spool {tag_spool.spoolman_id} for UID {tag_spool.spool_uid} "
                    f"({vendor_name} {tag_spool.material_type} / filament {filament_id})")
        return tag_spool

    def _get_vendor_by_name(self, name: str) -> Optional[dict]:
        """
        Returns the first Spoolman vendor whose name matches case-insensitively,
        or None if not found.
        """
        try:
            response = requests.get(f"{self.base_url}/api/v1/vendor", timeout=5)
            response.raise_for_status()
            name_lower = name.lower()
            for vendor in response.json():
                if vendor.get("name", "").lower() == name_lower:
                    return vendor
            return None
        except Exception as e:
            logger.error(f"Failed to fetch vendors from Spoolman: {e}")
            raise

    def _create_vendor(self, name: str) -> dict:
        """
        Creates a new vendor in Spoolman and returns the created object.
        """
        try:
            response = requests.post(
                f"{self.base_url}/api/v1/vendor",
                json={"name": name},
                timeout=5,
            )
            response.raise_for_status()
            vendor = response.json()
            logger.info(f"Created Spoolman vendor '{name}' (id={vendor['id']})")
            return vendor
        except Exception as e:
            logger.error(f"Failed to create vendor '{name}' in Spoolman: {e}")
            raise

    def _get_filament(
        self,
        vendor_id: int,
        material: str,
        color_hex: str,
        name: Optional[str] = None,
    ) -> Optional[dict]:
        """
        Returns the first Spoolman filament matching all four criteria:
            vendor_id + material + color_hex + name
        Returns None if no match is found.

        All four fields must match exactly (color_hex and name are case-sensitive).
        A filament with the same vendor/material/color but a different name is a miss
        — treat as user error and create a new entry.
        """
        target_material = (material or "").strip()
        target_color = (color_hex or "").strip().lstrip("#").lower()
        target_name = (name or "").strip().casefold() or None

        try:
            response = requests.get(
                f"{self.base_url}/api/v1/filament",
                params={"vendor_id": vendor_id},
                timeout=5,
            )
            response.raise_for_status()
            for filament in response.json():
                filament_material = (filament.get("material") or "").strip()
                filament_color = (filament.get("color_hex") or "").strip().lstrip("#").lower()
                filament_name = (filament.get("name") or "").strip().casefold() or None
                if (
                    filament.get("vendor", {}).get("id") == vendor_id
                    and filament_material == target_material
                    and filament_color == target_color
                    and filament_name == target_name
                ):
                    return filament
            return None
        except Exception as e:
            logger.error(f"Failed to fetch filaments from Spoolman: {e}")
            raise

    def _create_filament(
        self,
        vendor_id: int,
        material: str,
        color_hex: str,
        name: Optional[str] = None,
        diameter: Optional[float] = None,
        density: Optional[float] = None,
    ) -> dict:
        """
        Creates a new filament in Spoolman and returns the created object.
        Only includes optional fields (name, diameter, density) when provided.
        """
        payload: dict = {
            "vendor_id": vendor_id,
            "material": material,
            "color_hex": color_hex,
        }
        if name is not None:
            payload["name"] = name
        if diameter is not None:
            payload["diameter"] = diameter
        if density is not None:
            payload["density"] = density

        try:
            response = requests.post(
                f"{self.base_url}/api/v1/filament",
                json=payload,
                timeout=5,
            )
            response.raise_for_status()
            filament = response.json()
            logger.info(f"Created Spoolman filament '{name or material}' (id={filament['id']}, "
                        f"vendor={vendor_id}, color={color_hex})")
            return filament
        except Exception as e:
            logger.error(f"Failed to create filament in Spoolman: {e}")
            raise

    def _create_spool(self, filament_id: int, weight: Optional[float] = None) -> dict:
        """
        Creates a new spool in Spoolman linked to the given filament_id.
        weight is the nominal full weight in grams — stored as Spoolman's
        initial_weight. Omitted if not provided.
        """
        payload: dict = {"filament_id": filament_id}
        if weight is not None:
            payload["initial_weight"] = weight

        try:
            response = requests.post(
                f"{self.base_url}/api/v1/spool",
                json=payload,
                timeout=5,
            )
            response.raise_for_status()
            spool = response.json()
            logger.info(f"Created Spoolman spool (id={spool['id']}, filament={filament_id})")
            return spool
        except Exception as e:
            logger.error(f"Failed to create spool in Spoolman: {e}")
            raise

    def _update_spoolman_weight(self, spoolman_id: int, remaining_g: Optional[float], nominal_g: Optional[float]):
        """
        Updates the consumed weight in Spoolman.

        Spoolman stores 'used_weight', not 'remaining_weight', so we calculate:
            used_weight = nominal_g (filament["weight"]) - remaining_g
        Both values are required — if either is missing we skip the update.
        """
        if remaining_g is None or nominal_g is None:
            logger.debug(f"Skipping weight update for spool {spoolman_id}: missing remaining or nominal weight.")
            return
        used_weight = max(0.0, nominal_g - remaining_g)
        try:
            requests.patch(
                f"{self.base_url}/api/v1/spool/{spoolman_id}",
                json={"used_weight": used_weight},
                timeout=5
            ).raise_for_status()
            logger.info(f"Spoolman spool {spoolman_id}: used_weight set to {used_weight:.1f}g")
        except Exception as e:
            logger.warning(f"Failed to update weight for spool {spoolman_id}: {e}")

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
            logger.info(f"Wrote NFC UID {nfc_uid} to Spoolman spool {spoolman_id}.")
            # Update the local cache so we don't need a full refresh
            self.cache[nfc_uid.lower()] = {"id": spoolman_id}
        except Exception as e:
            logger.error(f"Failed to write NFC UID to Spoolman spool {spoolman_id}: {e}")
            raise
