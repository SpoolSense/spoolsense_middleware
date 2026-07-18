"""
happy_hare.py — Happy Hare MMU integration (pull mode).

Binding goes through Happy Hare's own primitive:

    MMU_SPOOLMAN SPOOLID=<spool> GATE=<gate>

which calls the mmu_server component's set_spool_gate — it validates the
gate, unsets any other spool claiming that printer+gate, writes the
`mmu_gate_map`/`printer_name` extras in the encoding HH expects, and
updates HH's spool-location cache. The middleware deliberately does NOT
write Spoolman extras itself: an earlier version PATCHed `extra.mmu_gate`
directly, but HH reads `mmu_gate_map` (components/mmu_server.py
MMU_GATE_FIELD) and json-decodes its values, so the direct write was
invisible to every HH version.

Flows:
  - Physical scanner (`action: happy_hare_stage`): select a gate
    (MMU_SELECT_GATE GATE=N), scan — binds to the selected gate.
  - Mobile (`mobile.action: happy_hare_stage`): scan on the phone, pick a
    gate from the app — binds to that gate, no selection needed.

Pull-mode-only: Happy Hare exposes `spoolman_support` on its `printer.mmu`
object. We query that on first use and refuse to bind in any other mode,
since `MMU_GATE_MAP NEXT_SPOOLID=...` is the right path there.

Also hosts on_ws_mmu — the websocket follower for printer.mmu deltas that
feeds the live gate into /api/status `active_tool`.
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


def _bind_checks() -> dict | None:
    """Shared preconditions for any gate bind (one printer.mmu fetch).
    Returns the mmu status dict when binding is possible, None otherwise
    (reason logged)."""
    happy_hare_cfg = app_state.cfg.get("happy_hare", {})
    if not happy_hare_cfg.get("enabled"):
        logger.debug("Happy Hare: bind skipped — integration not enabled")
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

    return mmu_status


def _bind_core(gate: int, spool_id: int, mmu_status: dict) -> bool:
    """Validate the gate against the already-fetched mmu status and hand the
    bind to Happy Hare's own set_spool_gate via gcode. HH validates again,
    unsets any other spool on that printer+gate, writes the extras it
    actually reads, and updates its cache — no Spoolman writes from here."""
    if isinstance(gate, bool) or not isinstance(gate, int) or gate < 0:
        logger.warning("Happy Hare: bind skipped — invalid gate %r", gate)
        return False

    num_gates = mmu_status.get("num_gates")
    if isinstance(num_gates, int) and gate >= num_gates:
        logger.warning("Happy Hare: bind skipped — gate %d out of range (num_gates=%d)",
                       gate, num_gates)
        return False

    moonraker_url = app_state.cfg.get("moonraker_url", "")
    if not moonraker_url:
        logger.error("Happy Hare: bind skipped — moonraker_url not configured")
        return False

    try:
        send_gcode(moonraker_url, f"MMU_SPOOLMAN SPOOLID={spool_id} GATE={gate}")
    except Exception:
        logger.exception("Happy Hare: MMU_SPOOLMAN SPOOLID=%s GATE=%s failed",
                         spool_id, gate)
        return False

    logger.info("Happy Hare: bound spool %s to gate %d", spool_id, gate)
    return True


def bind_spool_to_gate(gate: int, spool_id: int) -> bool:
    """
    Bind a spool to a specific MMU gate — no gate selection required. Used
    by the mobile assign flow, where the phone picks the gate. Returns True
    on success, False on any failure (with a logged reason).
    """
    mmu_status = _bind_checks()
    if mmu_status is None:
        return False
    return _bind_core(gate, spool_id, mmu_status)


def bind_spool_to_current_gate(spool_id: int) -> bool:
    """
    Bind a spool to whichever MMU gate is currently selected (the physical
    select-then-scan flow). One printer.mmu fetch serves both the gate read
    and the bind checks. Returns True on success, False on any failure
    (with a logged reason).
    """
    mmu_status = _bind_checks()
    if mmu_status is None:
        return False

    gate = mmu_status.get("gate")
    if not isinstance(gate, int) or gate < 0:
        logger.warning("Happy Hare: bind skipped — no gate selected (got %r). "
                       "Run MMU_SELECT_GATE GATE=N before scanning.", gate)
        return False

    return _bind_core(gate, spool_id, mmu_status)


def on_ws_mmu(data: dict) -> None:
    """
    Websocket callback for printer.mmu deltas — feeds /api/status for the
    mobile staging board. Happy Hare manages Spoolman itself in pull mode,
    so this only updates local state: no gcode, no HTTP.

    Two fields are mirrored (deltas are partial; absent key = unchanged):
    - `gate` -> active_tool: >= 0 is a real gate; -1 (unknown/unloaded)
      and -2 (bypass) map to None.
    - `gate_spool_id` -> active_spools["G<i>"]: HH's authoritative per-gate
      spool map, so occupancy reflects binds from ANY source (middleware,
      MMU_GATE_MAP, HH UI). -1 means unassigned.
    """
    if not isinstance(data, dict):
        return

    gate = data.get("gate")
    has_gate = ("gate" in data and not isinstance(gate, bool)
                and isinstance(gate, int))
    if "gate" in data and not has_gate:
        logger.debug("Happy Hare: ignoring non-integer mmu gate %r", gate)

    spool_ids = data.get("gate_spool_id")
    has_map = isinstance(spool_ids, list)

    if not has_gate and not has_map:
        return
    with app_state.state_lock:
        if has_gate:
            app_state.indx_active_tool = gate if gate >= 0 else None
        if has_map:
            for i, sid in enumerate(spool_ids):
                valid = not isinstance(sid, bool) and isinstance(sid, int) and sid > 0
                app_state.active_spools[f"G{i}"] = sid if valid else None


def _reset_mode_cache_for_testing() -> None:
    """Test helper — clears the cached spoolman_support read.

    Must be called in setUp() of any test that exercises this module, since
    the cache is module-level and persists across test methods otherwise.
    """
    global _cached_pull_mode, _logged_mismatch
    with _mode_lock:
        _cached_pull_mode = False
        _logged_mismatch = False
