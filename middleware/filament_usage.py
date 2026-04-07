"""
filament_usage.py — Filament usage deduction via UPDATE_TAG macro.

When UPDATE_TAG fires in PRINT_END, this module:
1. Grabs the last completed job's per-tool filament weights (toolchanger/single)
   or reads AFC lane weights (AFC)
2. Sends deduction commands to the scanner via MQTT
3. Scanner stores deductions and writes to the NFC tag on next scan

Macro detection: websocket push (primary) or HTTP polling (fallback),
same pattern as ASSIGN_SPOOL in toolchanger_status.py.
"""
from __future__ import annotations

import json
import logging
import threading

import requests

import app_state
from config import has_afc_scanners

logger = logging.getLogger(__name__)

POLL_INTERVAL: float = 2.0
RETRY_BASE: float = 2.0
RETRY_MAX: float = 30.0

MACRO_NAME = "UPDATE_TAG"
VARIABLE_NAME = "pending"


def _fetch_last_job_weights() -> list[float] | None:
    """
    Fetch filament_weights from the last completed print job.

    Returns a list of per-tool weights in grams (slicer estimate),
    or None if no completed job found or on error.
    """
    moonraker = app_state.cfg.get("moonraker_url", "")
    if not moonraker:
        return None

    try:
        response = requests.get(
            f"{moonraker}/server/history/list",
            params={"limit": 1, "order": "desc"},
            timeout=10,
        )
        response.raise_for_status()
        result = response.json().get("result", {})
        jobs = result.get("jobs", [])
        if not jobs:
            return None

        job = jobs[0]
        if job.get("status") != "completed":
            logger.debug("UPDATE_TAG: last job status is '%s', not completed", job.get("status"))
            return None

        weights = job.get("metadata", {}).get("filament_weights", [])
        if not isinstance(weights, list):
            return None
        return weights

    except requests.ConnectionError:
        logger.debug("UPDATE_TAG: Moonraker not reachable")
        return None
    except Exception:
        logger.exception("UPDATE_TAG: failed to fetch last job")
        return None


def _fetch_afc_lane_weights() -> dict[str, float] | None:
    """
    Fetch current per-lane weight from AFC status.

    Returns dict like {"lane1": 550.0, "lane2": 720.0}, or None on error.
    """
    moonraker = app_state.cfg.get("moonraker_url", "")
    if not moonraker:
        return None

    try:
        response = requests.get(
            f"{moonraker}/printer/afc/status",
            timeout=5,
        )
        response.raise_for_status()
        result = response.json()

        # Unwrap Moonraker envelope
        if isinstance(result, dict) and "result" in result:
            result = result["result"]

        # Navigate AFC status structure: status: -> AFC -> unit -> lane
        status_block = result.get("status:") or result.get("status")
        if not isinstance(status_block, dict):
            return None
        afc_data = status_block.get("AFC")
        if not isinstance(afc_data, dict):
            return None

        skip_keys = {"system", "Tools"}
        weights: dict[str, float] = {}

        for unit_name, unit_data in afc_data.items():
            if unit_name in skip_keys or not isinstance(unit_data, dict):
                continue
            for lane_name, lane_data in unit_data.items():
                if lane_name == "system" or not isinstance(lane_data, dict):
                    continue
                weight = lane_data.get("weight")
                if isinstance(weight, (int, float)):
                    weights[lane_name] = float(weight)

        return weights if weights else None

    except requests.ConnectionError:
        logger.debug("UPDATE_TAG: Moonraker not reachable for AFC status")
        return None
    except Exception:
        logger.exception("UPDATE_TAG: failed to fetch AFC lane weights")
        return None


def _clear_pending() -> None:
    """Clear the UPDATE_TAG macro variable back to 0."""
    moonraker = app_state.cfg.get("moonraker_url", "")
    if not moonraker:
        return

    try:
        requests.post(
            f"{moonraker}/printer/gcode/script",
            json={"script": f"SET_GCODE_VARIABLE MACRO={MACRO_NAME} VARIABLE={VARIABLE_NAME} VALUE=0"},
            timeout=5,
        ).raise_for_status()
        logger.debug("UPDATE_TAG: cleared pending variable")
    except Exception:
        logger.exception("UPDATE_TAG: failed to clear pending variable")


def _publish_deduction(device_id: str, uid: str, deduct_g: float) -> None:
    """Publish a deduction command to the scanner via MQTT."""
    if not app_state.mqtt_client:
        return

    prefix = app_state.cfg.get("scanner_topic_prefix", "spoolsense")
    topic = f"{prefix}/{device_id}/cmd/deduct/{uid}"
    payload = json.dumps({"deduct_g": round(deduct_g, 2)})

    try:
        result = app_state.mqtt_client.publish(topic, payload, qos=1)
        if result.rc == 0:
            logger.info(f"UPDATE_TAG: sent deduction {deduct_g:.1f}g to {uid} via {device_id}")
        else:
            logger.warning(f"UPDATE_TAG: MQTT publish failed (rc={result.rc})")
    except Exception:
        logger.exception("UPDATE_TAG: failed to publish deduction")


def _handle_update_tag() -> None:
    """
    Main handler — triggered when UPDATE_TAG macro fires.

    For AFC: reads current lane weights, compares to initial scan weight,
    sends difference as deduction.

    For toolchanger/single: reads last completed job's per-tool filament_weights,
    sends each as a deduction to the active spool on that tool.
    """
    if has_afc_scanners(app_state.cfg):
        _handle_afc()
    else:
        _handle_toolchanger()

    _clear_pending()


def _handle_afc() -> None:
    """Handle UPDATE_TAG for AFC setups — deduction from AFC weight tracking."""
    lane_weights = _fetch_afc_lane_weights()
    if not lane_weights:
        logger.info("UPDATE_TAG: no AFC lane weights available — skipping")
        return

    # Snapshot state under lock
    with app_state.state_lock:
        initial_weights = dict(app_state.active_spool_weights)
        uids = dict(app_state.active_spool_uids)
        devices = dict(app_state.active_spool_devices)

    for lane, current_weight in lane_weights.items():
        initial = initial_weights.get(lane)
        if initial is None:
            continue

        deduction = initial - current_weight
        if deduction <= 0:
            continue

        uid = uids.get(lane)
        device_id = devices.get(lane)
        if not uid or not device_id:
            logger.debug(f"UPDATE_TAG: no UID or device for {lane}, skipping")
            continue

        _publish_deduction(device_id, uid, deduction)

        # Update initial weight so next UPDATE_TAG only deducts the delta
        with app_state.state_lock:
            app_state.active_spool_weights[lane] = current_weight


def _handle_toolchanger() -> None:
    """Handle UPDATE_TAG for toolchanger/single — deduction from last job weights."""
    weights = _fetch_last_job_weights()
    if not weights:
        logger.info("UPDATE_TAG: no completed job found — skipping")
        return

    # Snapshot state under lock
    with app_state.state_lock:
        uids = dict(app_state.active_spool_uids)
        devices = dict(app_state.active_spool_devices)

    for index, weight in enumerate(weights):
        if weight <= 0:
            continue

        tool_name = f"T{index}"
        uid = uids.get(tool_name)
        device_id = devices.get(tool_name)

        if not uid or not device_id:
            logger.debug(f"UPDATE_TAG: no active spool on {tool_name}, skipping")
            continue

        _publish_deduction(device_id, uid, weight)


def _fetch_pending() -> int | None:
    """Fetch the UPDATE_TAG macro pending variable. Returns 0/1, or None on error."""
    moonraker = app_state.cfg.get("moonraker_url", "")
    if not moonraker:
        return None

    try:
        response = requests.get(
            f"{moonraker}/printer/objects/query?gcode_macro%20{MACRO_NAME}",
            timeout=5,
        )
        response.raise_for_status()
        result = response.json()
        macro_data = result.get("result", {}).get("status", {}).get(f"gcode_macro {MACRO_NAME}", {})
        return macro_data.get(VARIABLE_NAME, 0)
    except requests.ConnectionError:
        logger.debug("UPDATE_TAG: Moonraker not reachable")
        return None
    except Exception:
        logger.exception("UPDATE_TAG: unexpected error fetching macro state")
        return None


class FilamentUsageSync:
    """
    Monitors UPDATE_TAG macro via websocket (primary) or HTTP polling (fallback).
    Same pattern as ToolchangerStatusSync for ASSIGN_SPOOL.
    """

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._use_ws = False

    def on_ws_update_tag(self, pending: int) -> None:
        """Callback for MoonrakerWebsocket — processes UPDATE_TAG trigger."""
        if not pending:
            return
        logger.info("UPDATE_TAG: triggered via websocket")
        try:
            _handle_update_tag()
        except Exception:
            logger.exception("UPDATE_TAG: error in handler")

    def start(self, use_ws: bool = False) -> None:
        """Start monitoring. If use_ws=True, skip polling."""
        self._use_ws = use_ws

        if use_ws:
            logger.info("UPDATE_TAG: using websocket (no polling thread)")
            return

        # Check if macro exists
        initial = _fetch_pending()
        if initial is None:
            logger.warning(
                "UPDATE_TAG: macro not found — "
                "add it to your printer.cfg (see docs)"
            )
        else:
            logger.info("UPDATE_TAG: macro detected, polling started")

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._poll_loop,
            name="update-tag-sync",
            daemon=True,
        )
        self._thread.start()
        logger.info(f"UPDATE_TAG: polling started (interval={POLL_INTERVAL}s)")

    def stop(self) -> None:
        """Signal the polling thread to stop."""
        if self._use_ws:
            logger.info("UPDATE_TAG: websocket mode stopped")
            return
        if self._thread is None:
            return
        self._stop_event.set()
        self._thread.join(timeout=5)
        if self._thread.is_alive():
            logger.warning("UPDATE_TAG: polling thread did not stop cleanly")
        else:
            logger.info("UPDATE_TAG: polling stopped")
        self._thread = None

    def _poll_loop(self) -> None:
        """Background loop that polls UPDATE_TAG macro state."""
        consecutive_failures: int = 0

        while not self._stop_event.is_set():
            pending = _fetch_pending()

            if pending is not None:
                consecutive_failures = 0
                if pending:
                    logger.info("UPDATE_TAG: triggered via polling")
                    try:
                        _handle_update_tag()
                    except Exception:
                        logger.exception("UPDATE_TAG: error in handler")
                wait = POLL_INTERVAL
            else:
                consecutive_failures += 1
                wait = min(RETRY_BASE * (2 ** (consecutive_failures - 1)), RETRY_MAX)
                if consecutive_failures == 1:
                    logger.warning("UPDATE_TAG: poll failed, retrying with backoff")

            self._stop_event.wait(timeout=wait)
