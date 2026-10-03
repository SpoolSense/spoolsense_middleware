"""
client.py — SpoolmanClient for spool lookup and enrichment.

Primarily read-only: looks up spools by NFC UID and enriches tag data with
Spoolman's color, material, and weight info. The scanner handles spool
creation and the bulk of Spoolman writes; Moonraker handles filament usage
tracking via sync_rate.

One narrow write path exists: `update_spool_extras()` is used by integrations
that need to bind metadata onto an existing spool (e.g. Happy Hare's MMU gate
assignment).
"""
import json
import logging
import re
import time
from typing import Optional

import requests

from state.models import SpoolInfo

logger = logging.getLogger(__name__)

CACHE_TTL = 3600  # seconds before forcing a full Spoolman re-sync

# Quotes (extra.nfc_id is stored JSON-quoted) plus the separators Spoolman's
# own normalize_uid strips, so "04:A2:B3", '"04a2b3"' and "04-a2-b3" share a key
_UID_NOISE = re.compile(r'[\s:_.\-"]+')


def _normalize_uid(uid: str) -> str:
    """Cache key for a tag UID: no quotes, separators or 0x prefix, lowercase."""
    return _UID_NOISE.sub("", uid).lower().removeprefix("0x")


def _native_tag_uids(spool: dict) -> list[str]:
    """
    Normalized UIDs of a spool's native Spoolman tags (v0.27+, #123).

    Older servers send no `tags` key, so this returns [] for them — no
    version check needed. Malformed entries are skipped, never raised on.
    """
    tags = spool.get("tags")
    if not isinstance(tags, list):
        return []
    uids = []
    for tag in tags:
        uid = tag.get("uid") if isinstance(tag, dict) else None
        if isinstance(uid, str) and _normalize_uid(uid):
            uids.append(_normalize_uid(uid))
    return uids


def _index_spools_by_uid(spools: list[dict]) -> tuple[dict[str, dict], int, int]:
    """
    Map normalized UID → spool from `extra.nfc_id` and native tags.

    Returns (index, native_tag_count, nfc_id_count). Among `extra.nfc_id`
    claimants the last spool wins, as it always has. A native tag is unique
    and authoritative, so it overrides them; every other spool whose
    `extra.nfc_id` claims the same UID is named in a WARNING — including one
    hidden behind a dual-written spool that also owns the legacy slot.
    """
    legacy_claims: dict[str, list[dict]] = {}
    for spool in spools:
        nfc_id = _normalize_uid(str(spool.get("extra", {}).get("nfc_id", "")))
        if nfc_id:
            legacy_claims.setdefault(nfc_id, []).append(spool)
    index = {uid: claimants[-1] for uid, claimants in legacy_claims.items()}

    native_count = 0
    for spool in spools:
        for uid in _native_tag_uids(spool):
            others = [s.get("id") for s in legacy_claims.get(uid, []) if s.get("id") != spool.get("id")]
            if others:
                logger.warning(
                    "UID %s is a native tag on spool %s and extra.nfc_id on spool %s — using spool %s",
                    uid, spool.get("id"), ", ".join(str(o) for o in others), spool.get("id"),
                )
            index[uid] = spool
            native_count += 1
    return index, native_count, len(legacy_claims)


class SpoolmanClient:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip('/')
        self.cache = {}
        self._last_refresh = 0

    def _fetch_all_spools(self) -> None:
        """
        Pulls all active (non-archived) spools to build the NFC UID lookup cache.

        Indexes both the legacy `extra.nfc_id` field and native Spoolman tags
        (v0.27+, #123). A native tag is unique and authoritative, so it wins
        when another spool's `extra.nfc_id` claims the same UID.
        """
        try:
            # Only index active spools — archived spools with the same nfc_id
            # would overwrite the active entry and cause lookup failures (#49)
            response = requests.get(f"{self.base_url}/api/v1/spool?archived=false", timeout=5)
            response.raise_for_status()
            new_cache, native_count, legacy_count = _index_spools_by_uid(response.json())
            self.cache = new_cache
            self._last_refresh = time.time()
            logger.info(
                "Spoolman cache refreshed: %d UIDs indexed (%d native tags, %d extra.nfc_id).",
                len(self.cache), native_count, legacy_count,
            )
            # Imported lazily: this client stays free of app_state coupling (#41)
            from health import set_health
            set_health("spoolman", "connected")
        except Exception as e:
            logger.error(f"Failed to fetch Spoolman cache: {e}")
            from health import set_health
            set_health("spoolman", "unreachable")

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

    def update_spool_extras(self, spool_id: int, extras: dict) -> bool:
        """
        PATCH the `extra` field on a spool. Returns True on success.

        Spoolman stores `extra` values as JSON-encoded strings (an int 4
        becomes "4", a string "muffin" becomes '"muffin"'), so each value
        is run through `json.dumps()` before sending.

        Only the keys passed in are written; Spoolman merges them with the
        spool's existing extras. Unknown extra keys (fields not declared
        under Spoolman → Extras) are rejected with HTTP 400 — the error
        body is logged so users can see which field to declare.
        """
        payload = {"extra": {k: json.dumps(v) for k, v in extras.items()}}
        try:
            response = requests.patch(
                f"{self.base_url}/api/v1/spool/{spool_id}",
                json=payload,
                timeout=5,
            )
            response.raise_for_status()
            return True
        except requests.HTTPError as e:
            # Include Spoolman's response body — "Unknown extra field X" and
            # similar messages are actionable, but only if the user sees them.
            body = e.response.text if e.response is not None else ""
            logger.error(
                "Failed to update extras on spool %s: %s. Spoolman response: %s",
                spool_id, e, body,
            )
            return False
        except requests.RequestException:
            logger.exception("Failed to update extras on spool %s", spool_id)
            return False

    def find_by_nfc(self, nfc_uid: str) -> Optional[dict]:
        """Looks up a spool by NFC UID, with TTL-based cache and single forced refresh on miss."""
        uid_lower = _normalize_uid(nfc_uid)

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
        tag_spool.spoolman_remaining_g = existing.get("remaining_weight")

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
