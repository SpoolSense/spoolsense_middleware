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
from publishers.klipper import _send_afc_lane_data

logger = logging.getLogger(__name__)

# Polling interval in seconds. 2s is responsive enough for detecting
# spool load/eject events (which take seconds of physical action).
POLL_INTERVAL: float = 2.0

# Retry backoff on connection errors: start at 2s, double each failure, cap at 30s.
RETRY_BASE: float = 2.0
RETRY_MAX: float = 30.0


# ── AFC response parsing ─────────────────────────────────────────────────────

def _extract_afc_data(data: dict) -> dict | None:
    """Navigate AFC's response structure to get the unit/lane dict.
    AFC has a quirk: the status key has a trailing colon ('status:')."""
    status_block = data.get("status:") or data.get("status")
    if isinstance(status_block, dict):
        afc_data = status_block.get("AFC")
        if isinstance(afc_data, dict):
            return afc_data
    # Maybe already unwrapped (e.g., direct AFC block)
    return data.get("AFC", data) if isinstance(data, dict) else None


_SKIP_KEYS = {"system", "Tools"}


def _send_spool_id_to_lane(lane_name: str, spoolman_id: int, source: str) -> None:
    """Send SET_SPOOL_ID to AFC so it pulls spool data from Spoolman directly.

    Called on a 2-second delay after a lane load transition to let AFC's own
    load sequence finish first — if sent too early, AFC overwrites the data
    during its load process.
    """
    moonraker_url = app_state.cfg.get("moonraker_url", "")
    if not moonraker_url:
        logger.warning("AFC %s: cannot send SET_SPOOL_ID — no moonraker_url configured", source)
        return

    try:
        from publishers.klipper import _send_gcode
        _send_gcode(moonraker_url, f"SET_SPOOL_ID LANE={lane_name} SPOOL_ID={spoolman_id}")
        logger.info("AFC %s: sent SET_SPOOL_ID LANE=%s SPOOL_ID=%s", source, lane_name, spoolman_id)
    except Exception:
        logger.exception("AFC %s: failed to send SET_SPOOL_ID for %s", source, lane_name)


# ── Lane action publishing ───────────────────────────────────────────────────

def _publish_lane_actions(lane_name: str, action: str | None, pending: dict | None,
                          newly_loaded: bool, source: str) -> None:
    """Publish lock/clear state and push pending tag data. Shared by poll and websocket paths."""
    if action == "lock":
        logger.info(f"AFC {source}: {lane_name} has spool, locking")
        publish_lock(lane_name, "lock")
    elif action == "clear":
        logger.info(f"AFC {source}: {lane_name} empty, clearing")
        publish_lock(lane_name, "clear")

    if newly_loaded and pending:
        spoolman_id = pending.get("spoolman_id")
        if spoolman_id is not None:
            # Spoolman path — tell AFC the spool ID so it pulls data from Spoolman directly.
            # Delayed 2s to let AFC's own load sequence finish first — if sent too early,
            # AFC overwrites material/weight during its load process.
            logger.info(f"AFC {source}: {lane_name} just loaded — scheduling spool ID {spoolman_id} (2s delay)")
            threading.Timer(2.0, _send_spool_id_to_lane, args=(lane_name, spoolman_id, source)).start()
        else:
            # Tag-only path — no Spoolman, send color/material/weight directly
            logger.info(f"AFC {source}: {lane_name} just loaded — sending cached tag data")
            _send_afc_lane_data(
                lane_name,
                pending.get("color_hex", ""),
                pending.get("material", ""),
                pending.get("remaining_g"),
            )


# ── Full sync (HTTP polling) ────────────────────────────────────────────────

def _sync_lane_state(data: dict) -> None:
    """Process the full AFC status response — iterates all units and lanes."""
    afc_data = _extract_afc_data(data)
    if not isinstance(afc_data, dict):
        return

    for unit_name, unit_data in afc_data.items():
        if unit_name in _SKIP_KEYS or not isinstance(unit_data, dict):
            continue

        for lane_name, lane_data in unit_data.items():
            if lane_name == "system" or not isinstance(lane_data, dict):
                continue

            spool_id       = lane_data.get("spool_id")
            status         = lane_data.get("status")
            lane_is_loaded = lane_data.get("load", False)

            # Compute state change under lock, then publish outside it
            action: str | None   = None
            pending: dict | None = None
            newly_loaded: bool   = False

            with app_state.state_lock:
                was_loaded = app_state.lane_load_states.get(lane_name, False)
                is_locked  = app_state.lane_locks.get(lane_name, False)
                app_state.lane_statuses[lane_name]    = status
                app_state.lane_load_states[lane_name] = lane_is_loaded

                if spool_id is not None:
                    if not is_locked:
                        action = "lock"
                    app_state.active_spools[lane_name] = spool_id
                elif lane_is_loaded and not was_loaded and app_state.pending_spool:
                    # Lane transitioned unloaded → loaded with pending afc_stage data
                    newly_loaded = True
                    pending = app_state.pending_spool
                    app_state.pending_spool = None
                else:
                    if is_locked:
                        action = "clear"
                    app_state.active_spools[lane_name] = None

            _publish_lane_actions(lane_name, action, pending, newly_loaded, "Sync")


# ── Single lane sync (websocket) ────────────────────────────────────────────

def _sync_lane_state_single(lane_name: str, data: dict) -> None:
    """Process a single lane's state update from a websocket delta."""
    spool_id   = data.get("spool_id")
    load_state = data.get("load")

    action: str | None   = None
    pending: dict | None = None
    newly_loaded: bool   = False

    with app_state.state_lock:
        prev_spool = app_state.active_spools.get(lane_name)
        prev_load  = app_state.lane_load_states.get(lane_name, False)

        # Update spool tracking
        if spool_id is not None:
            if spool_id and not prev_spool:
                app_state.active_spools[lane_name] = spool_id
                action = "lock"
            elif not spool_id and prev_spool:
                app_state.active_spools.pop(lane_name, None)
                action = "clear"
            elif spool_id and spool_id != prev_spool:
                app_state.active_spools[lane_name] = spool_id
                action = "lock"

        # Track load transitions
        if load_state is not None:
            if load_state and not prev_load:
                newly_loaded = True
            app_state.lane_load_states[lane_name] = load_state

        # Consume pending afc_stage data on load transition
        if newly_loaded and app_state.pending_spool:
            pending = app_state.pending_spool
            app_state.pending_spool = None

        # Update lane status if present in delta
        status = data.get("status")
        if status is not None:
            app_state.lane_statuses[lane_name] = status

    _publish_lane_actions(lane_name, action, pending, newly_loaded, "WS")


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
            logger.exception("AFC status: HTTP error")
        return None
    except Exception:
        logger.exception("AFC status: unexpected error")
        return None


def resync_lock_state() -> None:
    """
    Re-publishes the current lock/clear state for all tracked AFC lanes.

    Called from on_connect() after an MQTT reconnect so that scanners
    (and any other subscribers) get the correct lock state without
    waiting for the next lane state change.
    """
    with app_state.state_lock:
        snapshot = dict(app_state.lane_locks)

    for lane, is_locked in snapshot.items():
        state = "lock" if is_locked else "clear"
        publish_lock(lane, state)
        logger.info(f"AFC resync: re-published {state} for {lane}")


class AfcStatusSync:
    """
    Monitors AFC lane state via Moonraker websocket (primary) or HTTP polling (fallback).

    Usage:
        sync = AfcStatusSync()
        sync.start()                # HTTP polling mode
        sync.start(use_ws=True)     # websocket mode (expects callbacks)
        ...
        sync.stop()
    """

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._use_ws = False

    def on_ws_lane_update(self, lane_name: str, data: dict) -> None:
        """Callback for MoonrakerWebsocket — processes a single lane's delta."""
        try:
            _sync_lane_state_single(lane_name, data)
        except Exception:
            logger.exception(f"AFC websocket: error processing update for {lane_name}")

    def start(self, use_ws: bool = False) -> None:
        """Start monitoring. If use_ws=True, skip polling (websocket provides data)."""
        self._use_ws = use_ws

        if use_ws:
            logger.info("AFC status: using websocket (no polling thread)")
            return

        # HTTP polling fallback
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
        if self._use_ws:
            logger.info("AFC status: websocket mode stopped")
            return
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
                except Exception:
                    logger.exception("AFC status: sync error")
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
