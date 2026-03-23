"""
afc_status.py — AFC lane state sync via Moonraker API.

Replaces the file watcher (watchdog on AFC.var.unit) with HTTP polling of
Moonraker's /printer/afc/status endpoint. Runs in a background thread,
polling every few seconds to detect lane state changes (spool loaded,
spool ejected) and updating lock/clear state accordingly.

Structured with a clean start/stop interface so the polling can be
swapped for Moonraker websocket subscription in the future (see #11).

Data flow:
    poll_loop() → GET /printer/afc/status → _sync_lane_state(data)
                                               ├── spool_id present → lock
                                               └── spool_id empty   → unlock
"""
from __future__ import annotations

import logging
import threading
import time

import requests

import app_state
from activation import publish_lock

logger = logging.getLogger(__name__)

# Polling interval in seconds. 2s is responsive enough for detecting
# spool load/eject events (which take seconds of physical action).
POLL_INTERVAL: float = 2.0

# Retry backoff on connection errors: start at 2s, double each failure, cap at 30s.
RETRY_BASE: float = 2.0
RETRY_MAX: float = 30.0


def _sync_lane_state(data: dict) -> None:
    """
    Processes the AFC status response and updates lock/clear state.

    This is the same logic as the old sync_from_afc_file(), but reads
    from parsed JSON (API response) instead of a file on disk.

    The AFC status response nests lane data under unit names:
        result.status:.AFC.<unit_name>.<lane_name>

    We skip entries named "system" (per-unit and top-level) and "Tools".
    """
    # Navigate the response structure.
    # AFC has a quirk: the key is "status:" with a trailing colon.
    afc_data: dict | None = None
    status_block = data.get("status:") or data.get("status")
    if isinstance(status_block, dict):
        afc_data = status_block.get("AFC")
    if not isinstance(afc_data, dict):
        # Maybe the response was already unwrapped (e.g., direct AFC block)
        afc_data = data.get("AFC", data)

    skip_keys = {"system", "Tools"}

    for unit_name, unit_data in afc_data.items():
        if unit_name in skip_keys or not isinstance(unit_data, dict):
            continue

        for lane_name, lane_data in unit_data.items():
            if lane_name == "system" or not isinstance(lane_data, dict):
                continue

            spool_id = lane_data.get("spool_id")
            status = lane_data.get("status")
            is_locked = app_state.lane_locks.get(lane_name, False)

            # Track AFC status for LED override logic
            app_state.lane_statuses[lane_name] = status

            if spool_id:
                if not is_locked:
                    logger.info(f"AFC Sync: {lane_name} has spool {spool_id}, locking")
                    publish_lock(lane_name, "lock")
                app_state.active_spools[lane_name] = spool_id
            else:
                if is_locked:
                    logger.info(f"AFC Sync: {lane_name} empty, clearing")
                    publish_lock(lane_name, "clear")
                app_state.active_spools[lane_name] = None


def _fetch_afc_status() -> dict | None:
    """
    Fetches the current AFC status from Moonraker.

    Returns the parsed JSON response dict, or None on error.
    """
    moonraker_url = app_state.cfg.get("moonraker_url", "")
    if not moonraker_url:
        return None

    try:
        response = requests.get(
            f"{moonraker_url}/printer/afc/status",
            timeout=5,
        )
        response.raise_for_status()
        result = response.json()

        # Unwrap the Moonraker "result" envelope
        if isinstance(result, dict) and "result" in result:
            return result["result"]
        return result

    except requests.ConnectionError:
        logger.debug("AFC status: Moonraker not reachable")
        return None
    except requests.Timeout:
        logger.warning("AFC status: Moonraker request timed out")
        return None
    except requests.HTTPError as e:
        # 404 likely means AFC is not installed
        if e.response is not None and e.response.status_code == 404:
            logger.warning("AFC status: endpoint not found — AFC may not be installed")
        else:
            logger.error(f"AFC status: HTTP error: {e}")
        return None
    except Exception as e:
        logger.error(f"AFC status: unexpected error: {e}")
        return None


class AfcStatusSync:
    """
    Polls Moonraker's AFC status endpoint in a background thread.

    Usage:
        sync = AfcStatusSync()
        sync.start()   # starts background polling
        ...
        sync.stop()    # clean shutdown

    The start/stop interface is designed so the polling implementation
    can be swapped for websocket subscription without changing callers.
    """

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    def start(self) -> None:
        """Start the background polling thread. Does an initial sync first."""
        # Initial sync — fetch once synchronously before starting the loop
        data = _fetch_afc_status()
        if data is not None:
            _sync_lane_state(data)
            logger.info("AFC status: initial sync complete")
        else:
            logger.warning(
                "AFC status: initial sync failed — will retry in background. "
                "AFC lane state may be stale until Moonraker is reachable."
            )

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._poll_loop,
            name="afc-status-sync",
            daemon=True,
        )
        self._thread.start()
        logger.info(f"AFC status: polling started (interval={POLL_INTERVAL}s)")

    def stop(self) -> None:
        """Signal the polling thread to stop and wait for it."""
        if self._thread is None:
            return
        self._stop_event.set()
        self._thread.join(timeout=5)
        if self._thread.is_alive():
            logger.warning("AFC status: polling thread did not stop cleanly")
        else:
            logger.info("AFC status: polling stopped")
        self._thread = None

    def _poll_loop(self) -> None:
        """Background loop that polls AFC status at regular intervals."""
        consecutive_failures: int = 0

        while not self._stop_event.is_set():
            data = _fetch_afc_status()

            if data is not None:
                try:
                    _sync_lane_state(data)
                except Exception as e:
                    logger.error(f"AFC status: sync error: {e}")
                consecutive_failures = 0
                wait = POLL_INTERVAL
            else:
                consecutive_failures += 1
                # Exponential backoff: 2s, 4s, 8s, 16s, 30s (capped)
                wait = min(RETRY_BASE * (2 ** (consecutive_failures - 1)), RETRY_MAX)
                if consecutive_failures == 1:
                    logger.warning("AFC status: poll failed, retrying with backoff")
                elif consecutive_failures % 10 == 0:
                    logger.warning(
                        f"AFC status: {consecutive_failures} consecutive failures, "
                        f"retrying every {wait:.0f}s"
                    )

            self._stop_event.wait(timeout=wait)
