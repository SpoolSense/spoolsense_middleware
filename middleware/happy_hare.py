"""
happy_hare.py — Happy Hare MMU integration (pull mode).

Workflow for `action: happy_hare_stage` scanners:
  1. User selects an MMU gate via `MMU_SELECT_GATE GATE=N` (Mainsail/LCD/console)
  2. User scans a tag on the shared scanner
  3. This module reads the currently-selected gate from Moonraker,
     PATCHes the Spoolman spool's `extra.mmu_gate` and `extra.printer_name`,
     then fires `MMU_SPOOLMAN SYNC=1` so Happy Hare picks up the binding
     immediately rather than waiting for its next periodic pull

Pull-mode-only: Happy Hare exposes `spoolman_support` on its `printer.mmu`
object. We query that on first use and refuse to bind in any other mode,
since `MMU_GATE_MAP NEXT_SPOOLID=...` is the right path there.

No background thread, no startup wiring — this is purely a synchronous
helper called from the scan handler.
"""
from __future__ import annotations

import logging
import threading

import app_state
from moonraker_client import query_objects, send_gcode

logger = logging.getLogger(__name__)

REQUIRED_MODE: str = "pull"
_KNOWN_MODES: frozenset[str] = frozenset({"pull", "push", "readonly"})

# We cache only the success case (`pull`). Any other observed value re-fetches
# on the next call so the integration can recover if the user fixes their
# Happy Hare config without restarting the middleware. `_logged_mismatch`
# prevents log spam when the mode is wrong and stays wrong.
_mode_lock = threading.Lock()
_cached_pull_mode: bool = False
_logged_mismatch: bool = False


def _fetch_mmu_status() -> dict | None:
    """GET printer.mmu from Moonraker. Returns the object dict, or None on failure."""
    moonraker_url = app_state.cfg.get("moonraker_url", "")
    status = query_objects(moonraker_url, "mmu", context="Happy Hare")
    if status is None:
        return None
    return status.get("mmu")


def _check_mode(mmu_status: dict) -> bool:
    """
    Return True if Happy Hare's spoolman_support matches the mode this
    integration supports (`pull`).

    Caches only the success case. If the mode is anything else (push,
    readonly, missing, unrecognized), we don't cache — the next bind
    re-fetches so the integration recovers automatically if the user
    fixes their Happy Hare config without restarting middleware.

    Logs the mismatch error exactly once per process while the mode
    is wrong; resets on recovery.
    """
    global _cached_pull_mode, _logged_mismatch
    with _mode_lock:
        if _cached_pull_mode:
            return True

        actual = mmu_status.get("spoolman_support", "")
        if actual == REQUIRED_MODE:
            _cached_pull_mode = True
            _logged_mismatch = False
            return True

        if not _logged_mismatch:
            if actual in _KNOWN_MODES:
                logger.error(
                    "Happy Hare: spoolman_support=%r, this integration requires %r. "
                    "Either switch Happy Hare's spoolman_support to %r in mmu_parameters.cfg, "
                    "or disable happy_hare in the middleware config.",
                    actual, REQUIRED_MODE, REQUIRED_MODE,
                )
            else:
                logger.error(
                    "Happy Hare: unrecognized spoolman_support value %r in printer.mmu. "
                    "Expected one of %s. Cannot bind until this is resolved.",
                    actual, sorted(_KNOWN_MODES),
                )
            _logged_mismatch = True
        return False


def _bind_checks() -> tuple[str, dict] | None:
    """Shared preconditions for any gate bind. Returns (printer_name,
    mmu_status) when binding is possible, None otherwise (reason logged)."""
    happy_hare_cfg = app_state.cfg.get("happy_hare", {})
    if not happy_hare_cfg.get("enabled"):
        logger.debug("Happy Hare: bind skipped — integration not enabled")
        return None

    printer_name = happy_hare_cfg.get("printer_name", "")
    if not printer_name:
        logger.error("Happy Hare: bind skipped — happy_hare.printer_name is empty")
        return None

    mmu_status = _fetch_mmu_status()
    if not mmu_status:
        logger.warning("Happy Hare: bind skipped — could not read printer.mmu from Moonraker")
        return None

    if not _check_mode(mmu_status):
        return None

    if not mmu_status.get("enabled", False):
        logger.warning("Happy Hare: bind skipped — MMU reports enabled=false")
        return None

    if app_state.spoolman_client is None:
        logger.error("Happy Hare: bind skipped — Spoolman client not configured")
        return None

    return printer_name, mmu_status


def bind_spool_to_gate(gate: int, spool_id: int) -> bool:
    """
    Bind a spool to a specific MMU gate — no gate selection required. Used
    by the mobile assign flow, where the phone picks the gate.

    PATCHes the spool's `extra.mmu_gate` + `extra.printer_name`, then
    triggers Happy Hare's Spoolman sync. Returns True on success, False on
    any failure (with a logged reason).
    """
    checks = _bind_checks()
    if checks is None:
        return False
    printer_name, mmu_status = checks

    if isinstance(gate, bool) or not isinstance(gate, int) or gate < 0:
        logger.warning("Happy Hare: bind skipped — invalid gate %r", gate)
        return False

    num_gates = mmu_status.get("num_gates")
    if isinstance(num_gates, int) and gate >= num_gates:
        logger.warning("Happy Hare: bind skipped — gate %d out of range (num_gates=%d)",
                       gate, num_gates)
        return False

    extras = {
        "mmu_gate": gate,
        "printer_name": printer_name,
    }
    if not app_state.spoolman_client.update_spool_extras(spool_id, extras):
        return False

    logger.info("Happy Hare: bound spool %s to gate %d on printer %r",
                spool_id, gate, printer_name)

    # Nudge Happy Hare to re-pull from Spoolman so the new binding is live
    # immediately rather than on its next periodic sync.
    moonraker_url = app_state.cfg.get("moonraker_url", "")
    if moonraker_url:
        try:
            send_gcode(moonraker_url, "MMU_SPOOLMAN SYNC=1")
        except Exception:
            logger.exception("Happy Hare: bind succeeded but MMU_SPOOLMAN SYNC=1 failed; "
                             "Happy Hare will pick up the binding on its next periodic pull")

    return True


def bind_spool_to_current_gate(spool_id: int) -> bool:
    """
    Bind a spool to whichever MMU gate is currently selected (the physical
    select-then-scan flow). Returns True on success, False on any failure
    (with a logged reason).
    """
    checks = _bind_checks()
    if checks is None:
        return False
    _, mmu_status = checks

    gate = mmu_status.get("gate")
    if not isinstance(gate, int) or gate < 0:
        logger.warning("Happy Hare: bind skipped — no gate selected (got %r). "
                       "Run MMU_SELECT_GATE GATE=N before scanning.", gate)
        return False

    return bind_spool_to_gate(gate, spool_id)


def on_ws_mmu(data: dict) -> None:
    """
    Websocket callback for printer.mmu deltas — tracks the live gate so
    /api/status can report it as `active_tool` (the mobile staging board's
    "printing" highlight). Happy Hare manages Spoolman's active spool itself
    in pull mode, so this only updates local state: no gcode, no HTTP.

    Gate values: >= 0 is a real gate; -1 (unknown/unloaded) and -2 (bypass)
    map to None. Deltas are partial — an absent gate key means unchanged.
    """
    if not isinstance(data, dict) or "gate" not in data:
        return
    gate = data.get("gate")
    if isinstance(gate, bool) or not isinstance(gate, int):
        logger.debug("Happy Hare: ignoring non-integer mmu gate %r", gate)
        return
    with app_state.state_lock:
        app_state.indx_active_tool = gate if gate >= 0 else None


def _reset_mode_cache_for_testing() -> None:
    """Test helper — clears the cached spoolman_support read.

    Must be called in setUp() of any test that exercises this module, since
    the cache is module-level and persists across test methods otherwise.
    """
    global _cached_pull_mode, _logged_mismatch
    with _mode_lock:
        _cached_pull_mode = False
        _logged_mismatch = False
