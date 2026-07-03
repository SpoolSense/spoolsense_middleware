"""
moonraker_client.py — shared Moonraker HTTP transport helpers.

One home for the Moonraker request shapes used across the middleware:
gcode scripts, printer object queries, the Spoolman active-spool endpoint,
and database items. Pure functions taking the Moonraker URL explicitly —
no app_state coupling (same dependency-injection direction as #67).

Modules keep their own semantic layers (sentinels, retries, log flavor);
this module owns URLs, timeouts, and response envelope unwrapping.
"""
from __future__ import annotations

import logging

import requests

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT: float = 5.0

# Tri-state sentinel: a fetch that *failed* is not the same as a fetch that
# returned "no value". Callers compare with `is`.
FETCH_ERROR = object()


def send_gcode(moonraker: str, script: str, timeout: float = DEFAULT_TIMEOUT) -> None:
    """
    POST a single gcode script to Moonraker.

    Raises on HTTP error or connection failure. Callers are responsible for
    catching and handling exceptions.
    """
    requests.post(
        f"{moonraker}/printer/gcode/script",
        json={"script": script},
        timeout=timeout,
    ).raise_for_status()


def query_objects(moonraker: str, query: str, timeout: float = DEFAULT_TIMEOUT,
                  context: str = "moonraker") -> dict | None:
    """
    GET /printer/objects/query?<query> and unwrap to the result.status dict.

    `query` is the raw query-string portion (e.g. "mmu", "print_stats",
    "gcode_macro%20ASSIGN_SPOOL", or multiple objects joined with "&").
    `context` prefixes log messages so operators can tell which subsystem
    a failure belongs to.

    Returns the status dict, or None on any failure (logged, never raises).
    """
    if not moonraker:
        return None
    try:
        response = requests.get(
            f"{moonraker}/printer/objects/query?{query}",
            timeout=timeout,
        )
        response.raise_for_status()
        status = response.json().get("result", {}).get("status", {})
        return status if isinstance(status, dict) else None
    except requests.ConnectionError:
        logger.debug("%s: Moonraker not reachable", context)
        return None
    except requests.Timeout:
        logger.warning("%s: Moonraker request timed out", context)
        return None
    except Exception:
        logger.exception("%s: unexpected error querying %r", context, query)
        return None


def list_objects(moonraker: str, timeout: float = DEFAULT_TIMEOUT) -> list | None:
    """GET /printer/objects/list. Returns the object name list, or None on failure."""
    if not moonraker:
        return None
    try:
        response = requests.get(f"{moonraker}/printer/objects/list", timeout=timeout)
        response.raise_for_status()
        objects = response.json().get("result", {}).get("objects", [])
        return objects if isinstance(objects, list) else None
    except Exception:
        logger.debug("moonraker: objects/list failed", exc_info=True)
        return None


def get_active_spool_id(moonraker: str, timeout: float = DEFAULT_TIMEOUT):
    """
    Fetch the active spool ID from Moonraker's Spoolman integration.

    Returns:
        int:         the active spool ID
        None:        no active spool (Moonraker reports null or 0)
        FETCH_ERROR: fetch failed — Moonraker unreachable, timeout, etc.
                     Callers must not treat this as "no spool".
    """
    if not moonraker:
        return FETCH_ERROR

    try:
        response = requests.get(
            f"{moonraker}/server/spoolman/spool_id",
            timeout=timeout,
        )
        response.raise_for_status()
        result = response.json()

        # Moonraker wraps in {"result": {"spool_id": N}}
        if isinstance(result, dict) and "result" in result:
            result = result["result"]

        spool_id = result.get("spool_id") if isinstance(result, dict) else None
        # Moonraker returns 0 (not null) when spool is cleared/ejected
        if spool_id is None or spool_id == 0:
            return None
        return int(spool_id)

    except requests.ConnectionError:
        logger.debug("moonraker: not reachable fetching active spool")
        return FETCH_ERROR
    except requests.Timeout:
        logger.warning("moonraker: active spool request timed out")
        return FETCH_ERROR
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            logger.debug("moonraker: Spoolman integration not configured")
        else:
            logger.exception("moonraker: HTTP error fetching active spool")
        return FETCH_ERROR
    except Exception:
        logger.exception("moonraker: unexpected error fetching active spool")
        return FETCH_ERROR


def set_active_spool_id(moonraker: str, spool_id: int,
                        timeout: float = DEFAULT_TIMEOUT) -> None:
    """
    POST the active spool ID to Moonraker's Spoolman integration.

    Use spool_id=0 to clear. Raises on HTTP error or connection failure —
    callers own the failure semantics (rollback, skip, propagate).
    """
    requests.post(
        f"{moonraker}/server/spoolman/spool_id",
        json={"spool_id": spool_id},
        timeout=timeout,
    ).raise_for_status()


def set_database_item(moonraker: str, namespace: str, key: str, value: dict,
                      timeout: float = DEFAULT_TIMEOUT) -> None:
    """
    POST a key/value into Moonraker's database (e.g. the lane_data namespace
    consumed by Orca Slicer). Raises on failure.
    """
    requests.post(
        f"{moonraker}/server/database/item",
        json={"namespace": namespace, "key": key, "value": value},
        timeout=timeout,
    ).raise_for_status()


def is_printer_idle(moonraker: str, timeout: float = 2.0) -> bool:
    """
    True when Klipper reports print_stats.state == "standby".

    False on any other state or on fetch failure — treat unknown as busy so
    callers never take print-unsafe actions (e.g. auto-releasing a scanner
    lock) without positive confirmation the printer is idle.
    """
    status = query_objects(moonraker, "print_stats", timeout=timeout,
                           context="print-state")
    if status is None:
        logger.debug("Could not query Klipper print state; treating as busy")
        return False
    state = status.get("print_stats", {}).get("state", "")
    return state == "standby"
