from __future__ import annotations

import logging
import time

import requests

import app_state

logger = logging.getLogger(__name__)


def get_spool_by_id(spool_id: int) -> dict | None:
    """Fetch a single spool directly from Spoolman."""
    if not app_state.cfg["spoolman_url"]:
        return None
    try:
        response = requests.get(
            f"{app_state.cfg['spoolman_url']}/api/v1/spool/{spool_id}", timeout=5
        )
        response.raise_for_status()
        return response.json()
    except Exception as e:
        logger.error(f"Failed to fetch spool {spool_id}: {e}")
        return None


def refresh_spool_cache() -> bool:
    """
    Pulls ALL spools from Spoolman and builds a local dictionary mapping NFC UIDs to Spoolman data.
    We do this so when a tag is scanned, the lookup is instant instead of waiting on a network request.
    """
    if not app_state.cfg["spoolman_url"]:
        return False
    try:
        logger.info("Refreshing Spoolman cache...")
        response = requests.get(
            f"{app_state.cfg['spoolman_url']}/api/v1/spool", timeout=5
        )
        response.raise_for_status()
        spools = response.json()

        new_cache: dict = {}
        for spool in spools:
            # Look for the nfc_id inside Spoolman's "extra" fields
            extra = spool.get("extra", {})
            nfc_id = extra.get("nfc_id", "").strip('"').lower()
            if nfc_id:
                new_cache[nfc_id] = spool

        app_state.spool_cache = new_cache
        app_state.last_cache_refresh = time.time()
        logger.info(f"Cache updated: {len(app_state.spool_cache)} spools indexed.")
        return True
    except Exception as e:
        logger.error(f"Failed to refresh Spoolman cache: {e}")
        return False


def find_spool_by_nfc(uid: str) -> dict | None:
    """
    Looks up a scanned NFC UID in our local memory cache.
    If it's not there, or the cache is too old, it forces a refresh.
    """
    uid_lower = uid.lower()
    if time.time() - app_state.last_cache_refresh > app_state.CACHE_TTL:
        refresh_spool_cache()

    if uid_lower in app_state.spool_cache:
        return app_state.spool_cache[uid_lower]

    # If we didn't find it, maybe it was just added to Spoolman 5 seconds ago. Force a refresh.
    logger.info(f"UID {uid} not in cache, performing forced refresh...")
    if refresh_spool_cache():
        return app_state.spool_cache.get(uid_lower)
    return None
