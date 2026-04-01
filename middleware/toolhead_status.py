"""
toolhead_status.py — Toolhead spool eject detection via Moonraker API.

Polls Moonraker's /server/spoolman/spool_id endpoint to detect when the
active spool is ejected (set to null via Mainsail or Moonraker API).
Clears the toolhead lock so the scanner accepts new scans.

Covers both 'toolhead' and 'toolhead_stage' scanner actions.

Data flow:
    poll_loop() → GET /server/spoolman/spool_id → _check_eject(spool_id)
                                                     ├── spool_id present → track
                                                     └── spool_id null    → clear lock
"""
from __future__ import annotations

import logging
import threading
import time

import requests

import app_state
from activation import publish_lock

logger = logging.getLogger(__name__)

POLL_INTERVAL: float = 2.0
RETRY_BASE: float = 2.0
RETRY_MAX: float = 30.0


def _fetch_active_spool_id() -> int | None:
    """
    Fetches the current active spool ID from Moonraker.

    Returns:
        int: the active spool ID
        None: no active spool (ejected) or error
    """
    moonraker_url = app_state.cfg.get("moonraker_url", "")
    if not moonraker_url:
        return None

    try:
        response = requests.get(
            f"{moonraker_url}/server/spoolman/spool_id",
            timeout=5,
        )
        response.raise_for_status()
        result = response.json()

        # Moonraker wraps in {"result": {"spool_id": N}}
        if isinstance(result, dict) and "result" in result:
            result = result["result"]

        spool_id = result.get("spool_id") if isinstance(result, dict) else None
        return int(spool_id) if spool_id is not None else None

    except requests.ConnectionError:
        logger.debug("Toolhead status: Moonraker not reachable")
        return None
    except requests.Timeout:
        logger.warning("Toolhead status: Moonraker request timed out")
        return None
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            logger.debug("Toolhead status: Spoolman integration not configured in Moonraker")
        else:
            logger.exception("Toolhead status: HTTP error")
        return None
    except Exception:
        logger.exception("Toolhead status: unexpected error")
        return None


class ToolheadStatusSync:
    """
    Polls Moonraker's active spool endpoint in a background thread.
    Clears toolhead locks when the spool is ejected.

    Usage:
        sync = ToolheadStatusSync()
        sync.start()
        ...
        sync.stop()
    """

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._last_spool_id: int | None = None
        self._fetch_failed: bool = False  # track if last fetch was an error

    def start(self) -> None:
        """Start the background polling thread."""
        # Initial fetch to establish baseline
        spool_id = _fetch_active_spool_id()
        self._last_spool_id = spool_id
        self._fetch_failed = (spool_id is None)
        if spool_id is not None:
            logger.info(f"Toolhead status: active spool is #{spool_id}")
        else:
            logger.info("Toolhead status: no active spool (or Moonraker unreachable)")

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._poll_loop,
            name="toolhead-status-sync",
            daemon=True,
        )
        self._thread.start()
        logger.info(f"Toolhead status: polling started (interval={POLL_INTERVAL}s)")

    def stop(self) -> None:
        """Signal the polling thread to stop and wait for it."""
        if self._thread is None:
            return
        self._stop_event.set()
        self._thread.join(timeout=5)
        if self._thread.is_alive():
            logger.warning("Toolhead status: polling thread did not stop cleanly")
        else:
            logger.info("Toolhead status: polling stopped")
        self._thread = None

    def _poll_loop(self) -> None:
        """Background loop that polls active spool at regular intervals."""
        consecutive_failures: int = 0

        while not self._stop_event.is_set():
            spool_id = _fetch_active_spool_id()

            if spool_id is not None or not self._fetch_failed:
                try:
                    self._check_transition(spool_id)
                except Exception:
                    logger.exception("Toolhead status: transition check error")
                consecutive_failures = 0
                wait = POLL_INTERVAL
                self._fetch_failed = False
            else:
                consecutive_failures += 1
                wait = min(RETRY_BASE * (2 ** (consecutive_failures - 1)), RETRY_MAX)
                self._fetch_failed = True
                if consecutive_failures == 1:
                    logger.warning("Toolhead status: poll failed, retrying with backoff")
                elif consecutive_failures % 10 == 0:
                    logger.warning(
                        f"Toolhead status: {consecutive_failures} consecutive failures, "
                        f"retrying every {wait:.0f}s"
                    )

            self._stop_event.wait(timeout=wait)

    def _check_transition(self, current_spool_id: int | None) -> None:
        """
        Detect spool eject (non-null → null) and clear the affected toolhead lock.
        """
        prev = self._last_spool_id
        self._last_spool_id = current_spool_id

        if prev is not None and current_spool_id is None:
            # Spool was ejected — find which toolhead had it and clear the lock
            with app_state.state_lock:
                for toolhead, spool_id in list(app_state.active_spools.items()):
                    if spool_id == prev:
                        logger.info(
                            f"Toolhead status: spool #{prev} ejected from {toolhead}, clearing lock"
                        )
                        publish_lock(toolhead, "clear")
                        app_state.active_spools[toolhead] = None
                        return

            # If no toolhead matched, clear all toolhead locks as a fallback
            logger.info(f"Toolhead status: spool #{prev} ejected, clearing all toolhead locks")
            scanners = app_state.cfg.get("scanners", {})
            for scanner_cfg in scanners.values():
                action = scanner_cfg.get("action", "")
                if action in ("toolhead", "toolhead_stage"):
                    target = scanner_cfg.get("toolhead", "")
                    if target and app_state.lane_locks.get(target):
                        publish_lock(target, "clear")

        elif prev is None and current_spool_id is not None:
            # Spool was set externally (not via scanner) — just track it
            logger.info(f"Toolhead status: active spool changed to #{current_spool_id}")

        elif prev != current_spool_id and prev is not None and current_spool_id is not None:
            # Spool changed without going through null — direct swap
            logger.info(
                f"Toolhead status: active spool changed #{prev} → #{current_spool_id}"
            )
            with app_state.state_lock:
                for toolhead, spool_id in list(app_state.active_spools.items()):
                    if spool_id == prev:
                        logger.info(f"Toolhead status: clearing lock on {toolhead} (spool swapped)")
                        publish_lock(toolhead, "clear")
                        app_state.active_spools[toolhead] = None
                        break
