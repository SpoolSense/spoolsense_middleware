"""
toolchanger_status.py — Tool assignment via Klipper macro polling.

Polls Moonraker for the ASSIGN_SPOOL gcode macro variable. When a user
runs `ASSIGN_SPOOL TOOL=T5` in Klipper, this module detects the pending
tool assignment and pairs it with cached spool data from the last scan.

Replaces the previous tool-pickup detection approach — macro assignment
is faster and works for any number of tools without physical pickup.

Required Klipper macro (user adds to their printer.cfg):

    [gcode_macro ASSIGN_SPOOL]
    variable_pending_tool: ""
    gcode:
      SET_GCODE_VARIABLE MACRO=ASSIGN_SPOOL VARIABLE=pending_tool VALUE="'{params.TOOL}'"

Data flow:
    poll_loop() → GET /printer/objects/query?gcode_macro ASSIGN_SPOOL
                    → pending_tool changed from ""?
                        → yes + pending_spool → assign to that tool
                        → clear pending_tool via SET_GCODE_VARIABLE
                    → no → sleep and poll again

Works with and without Spoolman:
    With Spoolman: SET_GCODE_VARIABLE (spool_id + color) + SAVE_VARIABLE
    Without:       SET_GCODE_VARIABLE (color only) from tag data
"""
from __future__ import annotations

import logging
import re
import threading

import requests

import app_state
from publishers.klipper import _send_gcode, _validate_color_hex, display_spoolcolor

logger = logging.getLogger(__name__)

POLL_INTERVAL: float = 2.0
RETRY_BASE: float = 2.0
RETRY_MAX: float = 30.0

MACRO_NAME = "ASSIGN_SPOOL"
VARIABLE_NAME = "pending_tool"

# Pattern to extract tool number from values like "T5", "t12", etc.
_TOOL_PATTERN = re.compile(r"^[Tt](\d+)$")


def _assign_spool_to_tool(tool_name: str, pending: dict) -> None:
    """
    Pushes cached spool data to the specified tool via Klipper gcode commands.

    tool_name is the full macro name (e.g. "T5") — used for SET_GCODE_VARIABLE.
    The numeric suffix is extracted for SAVE_VARIABLE.

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

    macro = tool_name.upper()
    match = _TOOL_PATTERN.match(macro)
    tool_number_str = match.group(1) if match else "0"

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
            _send_gcode(moonraker, f"SAVE_VARIABLE VARIABLE=t{tool_number_str}_spool_id VALUE={spoolman_id}")
            logger.info(f"[toolhead_stage] SAVE_VARIABLE t{tool_number_str}_spool_id={spoolman_id}")
        except Exception:
            logger.exception(f"[toolhead_stage] Failed to save spool_id for {macro}")

    # Color — always set from tag data (Spoolman or not)
    spool_color = display_spoolcolor(color_hex)
    if spool_color is not None:
        try:
            _send_gcode(
                moonraker,
                f"SET_GCODE_VARIABLE MACRO={macro} VARIABLE=color VALUE=\"'{spool_color}'\"",
            )
            logger.info(f"[toolhead_stage] SET_GCODE_VARIABLE {macro} color='{spool_color}'")
        except Exception:
            logger.exception(f"[toolhead_stage] Failed to set color on {macro}")

    if material:
        logger.info(f"[toolhead_stage] {macro} material: {material}")
    if remaining_g is not None:
        logger.info(f"[toolhead_stage] {macro} weight: {remaining_g:.0f}g")

    # Write spool data to Moonraker's lane_data database for slicer integration.
    # AFC handles this for its lanes; for toolhead assignments we write directly.
    # Gated by publish_lane_data config flag (opt-in).
    if not app_state.cfg.get("publish_lane_data", False):
        return

    safe_color = ""
    if color_hex:
        c = _validate_color_hex(color_hex)
        if c:
            safe_color = f"#{c}"

    safe_material = ""
    if material and material != "Unknown":
        safe_material = material.replace(" ", "_")

    lane_value = {
        "color": safe_color,
        "material": safe_material,
        "weight": round(remaining_g) if remaining_g else 0,
        "nozzle_temp": None,
        "bed_temp": None,
        "spool_id": spoolman_id,
    }
    try:
        requests.post(
            f"{moonraker}/server/database/item",
            json={"namespace": "lane_data", "key": macro, "value": lane_value},
            timeout=5,
        ).raise_for_status()
        logger.info(f"[toolhead_stage] Published lane_data for {macro}: {safe_material} {safe_color}")
    except Exception:
        logger.exception(f"[toolhead_stage] Failed to publish lane_data for {macro}")


def _fetch_pending_tool() -> str | None:
    """
    Fetches the pending_tool value from the ASSIGN_SPOOL macro.

    Returns the tool name (e.g. "T5"), empty string (no pending), or None on error.
    """
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
        return macro_data.get(VARIABLE_NAME, "")
    except requests.ConnectionError:
        logger.debug("Macro assign: Moonraker not reachable")
        return None
    except requests.Timeout:
        logger.warning("Macro assign: Moonraker request timed out")
        return None
    except Exception:
        logger.exception("Macro assign: unexpected error")
        return None


def _clear_pending_tool() -> None:
    """Clear the pending_tool variable after assignment."""
    moonraker = app_state.cfg.get("moonraker_url", "")
    if not moonraker:
        return

    try:
        _send_gcode(
            moonraker,
            f'SET_GCODE_VARIABLE MACRO={MACRO_NAME} VARIABLE={VARIABLE_NAME} VALUE="\'\'\"',
        )
        logger.debug("Macro assign: cleared pending_tool")
    except Exception:
        logger.exception("Macro assign: failed to clear pending_tool")


class ToolchangerStatusSync:
    """
    Polls the ASSIGN_SPOOL Klipper macro in a background thread.

    Detects when a user sets a pending tool via macro and pushes
    cached spool data to that tool. Same start/stop interface as
    AfcStatusSync for consistency.
    """

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    def start(self) -> None:
        """Start the background polling thread."""
        # Verify the macro exists
        initial = _fetch_pending_tool()
        if initial is None:
            logger.warning(
                "Macro assign: ASSIGN_SPOOL macro not found — "
                "add it to your printer.cfg (see docs)"
            )
        else:
            logger.info("Macro assign: ASSIGN_SPOOL macro detected, polling started")

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._poll_loop,
            name="macro-assign-sync",
            daemon=True,
        )
        self._thread.start()
        logger.info(f"Macro assign: polling started (interval={POLL_INTERVAL}s)")

    def stop(self) -> None:
        """Signal the polling thread to stop and wait for it."""
        if self._thread is None:
            return
        self._stop_event.set()
        self._thread.join(timeout=5)
        if self._thread.is_alive():
            logger.warning("Macro assign: polling thread did not stop cleanly")
        else:
            logger.info("Macro assign: polling stopped")
        self._thread = None

    def _poll_loop(self) -> None:
        """Background loop that polls macro state at regular intervals."""
        consecutive_failures: int = 0

        while not self._stop_event.is_set():
            pending_tool = _fetch_pending_tool()

            if pending_tool is not None:
                consecutive_failures = 0

                # Detect assignment: pending_tool is non-empty
                if pending_tool and pending_tool.strip():
                    tool_name = pending_tool.strip()
                    logger.info(f"Macro assign: tool {tool_name} requested")

                    # Check for pending spool data
                    pending: dict | None = None
                    with app_state.state_lock:
                        if app_state.pending_spool:
                            pending = app_state.pending_spool
                            app_state.pending_spool = None

                    if pending:
                        logger.info(
                            f"Macro assign: assigning cached spool data to {tool_name}"
                        )
                        _assign_spool_to_tool(tool_name, pending)
                    else:
                        logger.warning(
                            f"Macro assign: {tool_name} requested but no spool scanned yet — "
                            "scan a tag first, then run ASSIGN_SPOOL"
                        )

                    # Clear the macro variable
                    _clear_pending_tool()

                wait = POLL_INTERVAL
            else:
                consecutive_failures += 1
                wait = min(RETRY_BASE * (2 ** (consecutive_failures - 1)), RETRY_MAX)
                if consecutive_failures == 1:
                    logger.warning("Macro assign: poll failed, retrying with backoff")
                elif consecutive_failures % 10 == 0:
                    logger.warning(
                        f"Macro assign: {consecutive_failures} consecutive failures, "
                        f"retrying every {wait:.0f}s"
                    )

            self._stop_event.wait(timeout=wait)
