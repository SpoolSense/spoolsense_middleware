"""
toolchanger_status.py — Toolchanger state sync via Moonraker API.

Polls Moonraker's toolchanger object to detect tool changes. When a tool
pickup is detected and there is pending spool data (from a toolhead_stage
scan), pushes the cached data to the newly active tool.

Data flow:
    poll_loop() → GET /printer/objects/query?toolchanger
                    → tool_number changed?
                        → yes + pending_spool → assign to T{n}
                        → no → sleep and poll again

Works with and without Spoolman:
    With Spoolman: SET_GCODE_VARIABLE (spool_id + color) + SAVE_VARIABLE
    Without:       SET_GCODE_VARIABLE (color only) from tag data
"""
from __future__ import annotations

import logging
import threading

import requests

import app_state
from publishers.klipper import _send_gcode, _validate_color_hex

logger = logging.getLogger(__name__)

POLL_INTERVAL: float = 2.0
RETRY_BASE: float = 2.0
RETRY_MAX: float = 30.0


def _assign_spool_to_tool(tool_number: int, pending: dict) -> None:
    """
    Pushes cached spool data to the specified tool via Klipper gcode commands.

    This function is Klipper-coupled by design — toolchanger is a Klipper/AFC
    concept; there is no platform-agnostic equivalent. When Bambu or Prusa
    support is added, they will have their own polling modules.

    With Spoolman (spoolman_id present):
      - POST /server/spoolman/spool_id
      - SET_GCODE_VARIABLE MACRO=T{n} VARIABLE=spool_id VALUE={id}
      - SAVE_VARIABLE VARIABLE=t{n}_spool_id VALUE={id}
      - SET_GCODE_VARIABLE MACRO=T{n} VARIABLE=color VALUE="'{hex}'"

    Without Spoolman (tag-only):
      - SET_GCODE_VARIABLE MACRO=T{n} VARIABLE=color VALUE="'{hex}'"
    """
    moonraker = app_state.cfg.get("moonraker_url", "")
    if not moonraker:
        return

    macro = f"T{tool_number}"
    spoolman_id: int | None = pending.get("spoolman_id")
    color_hex: str = pending.get("color_hex", "")
    material: str = pending.get("material", "")
    remaining_g: float | None = pending.get("remaining_g")

    # Spoolman path — set spool_id and persist
    if spoolman_id is not None:
        try:
            requests.post(
                f"{moonraker}/server/spoolman/spool_id",
                json={"spool_id": spoolman_id},
                timeout=5,
            ).raise_for_status()
            logger.info(f"[toolhead_stage] SET_ACTIVE_SPOOL {spoolman_id} on {macro}")
        except Exception:
            logger.exception(f"[toolhead_stage] Failed to set active spool on {macro}")

        try:
            _send_gcode(moonraker, f"SET_GCODE_VARIABLE MACRO={macro} VARIABLE=spool_id VALUE={spoolman_id}")
            logger.info(f"[toolhead_stage] SET_GCODE_VARIABLE {macro} spool_id={spoolman_id}")
        except Exception:
            logger.exception(f"[toolhead_stage] Failed to set spool_id variable on {macro}")

        try:
            _send_gcode(moonraker, f"SAVE_VARIABLE VARIABLE=t{tool_number}_spool_id VALUE={spoolman_id}")
            logger.info(f"[toolhead_stage] SAVE_VARIABLE t{tool_number}_spool_id={spoolman_id}")
        except Exception:
            logger.exception(f"[toolhead_stage] Failed to save spool_id for {macro}")

    # Color — always set from tag data (Spoolman or not)
    if color_hex and color_hex not in ("FFFFFF", "000000", ""):
        safe_color = _validate_color_hex(color_hex)
        if safe_color is not None:
            try:
                _send_gcode(
                    moonraker,
                    f"SET_GCODE_VARIABLE MACRO={macro} VARIABLE=color VALUE=\"'{safe_color}'\"",
                )
                logger.info(f"[toolhead_stage] SET_GCODE_VARIABLE {macro} color='{safe_color}'")
            except Exception:
                logger.exception(f"[toolhead_stage] Failed to set color on {macro}")

    if material:
        logger.info(f"[toolhead_stage] {macro} material: {material}")
    if remaining_g is not None:
        logger.info(f"[toolhead_stage] {macro} weight: {remaining_g:.0f}g")


def _fetch_tool_number() -> int | None:
    """
    Fetches the current active tool number from Moonraker.

    Returns the tool_number (0-N), -1 (no tool), or None on error.
    """
    moonraker = app_state.cfg.get("moonraker_url", "")
    if not moonraker:
        return None

    try:
        response = requests.get(
            f"{moonraker}/printer/objects/query?toolchanger",
            timeout=5,
        )
        response.raise_for_status()
        result = response.json()
        toolchanger = result.get("result", {}).get("status", {}).get("toolchanger", {})
        return toolchanger.get("tool_number")
    except requests.ConnectionError:
        logger.debug("Toolchanger status: Moonraker not reachable")
        return None
    except requests.Timeout:
        logger.warning("Toolchanger status: Moonraker request timed out")
        return None
    except Exception:
        logger.exception("Toolchanger status: unexpected error")
        return None


class ToolchangerStatusSync:
    """
    Polls Moonraker's toolchanger object in a background thread.

    Detects tool changes and pushes pending spool data to the new tool.
    Same start/stop interface as AfcStatusSync for consistency.
    """

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._last_tool_number: int | None = None

    def start(self) -> None:
        """Start the background polling thread."""
        # Capture initial tool state so we don't trigger on startup
        initial = _fetch_tool_number()
        self._last_tool_number = initial
        if initial is not None:
            logger.info(f"Toolchanger status: initial tool_number={initial}")
        else:
            logger.warning("Toolchanger status: could not read initial state — will retry in background")

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._poll_loop,
            name="toolchanger-status-sync",
            daemon=True,
        )
        self._thread.start()
        logger.info(f"Toolchanger status: polling started (interval={POLL_INTERVAL}s)")

    def stop(self) -> None:
        """Signal the polling thread to stop and wait for it."""
        if self._thread is None:
            return
        self._stop_event.set()
        self._thread.join(timeout=5)
        if self._thread.is_alive():
            logger.warning("Toolchanger status: polling thread did not stop cleanly")
        else:
            logger.info("Toolchanger status: polling stopped")
        self._thread = None

    def _poll_loop(self) -> None:
        """Background loop that polls toolchanger state at regular intervals."""
        consecutive_failures: int = 0

        while not self._stop_event.is_set():
            tool_number = _fetch_tool_number()

            if tool_number is not None:
                consecutive_failures = 0

                # Detect tool change: number changed AND new tool is valid (>= 0)
                if (
                    self._last_tool_number is not None
                    and tool_number != self._last_tool_number
                    and tool_number >= 0
                ):
                    logger.info(
                        f"Toolchanger: tool changed {self._last_tool_number} → {tool_number}"
                    )
                    # Check for pending spool data
                    pending: dict | None = None
                    with app_state.state_lock:
                        if app_state.pending_spool:
                            pending = app_state.pending_spool
                            app_state.pending_spool = None

                    if pending:
                        logger.info(
                            f"Toolchanger: assigning cached spool data to T{tool_number}"
                        )
                        _assign_spool_to_tool(tool_number, pending)
                    else:
                        logger.debug(
                            f"Toolchanger: tool changed to T{tool_number} but no pending spool data"
                        )

                self._last_tool_number = tool_number
                wait = POLL_INTERVAL
            else:
                consecutive_failures += 1
                wait = min(RETRY_BASE * (2 ** (consecutive_failures - 1)), RETRY_MAX)
                if consecutive_failures == 1:
                    logger.warning("Toolchanger status: poll failed, retrying with backoff")
                elif consecutive_failures % 10 == 0:
                    logger.warning(
                        f"Toolchanger status: {consecutive_failures} consecutive failures, "
                        f"retrying every {wait:.0f}s"
                    )

            self._stop_event.wait(timeout=wait)
