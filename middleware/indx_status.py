"""
indx_status.py — Bondtech INDX active-tool sync (#91).

INDX's published Klipper macros (BondtechAB/INDX `macros/`) persist the
mounted tool in `save_variables.active_tool`: the index is written after
every successful pickup and set to -1 when the toolhead parks. Consuming
that variable from the existing save_variables websocket stream gives
real-time knowledge of which tool is printing — no polling, no
INDX-specific transport, and setups that never write `active_tool`
(every non-INDX printer) are completely unaffected.

On each pickup transition (active_tool -> N >= 0):
  - remember the tool in app_state.indx_active_tool
  - resolve the spool bound to that tool (t<N>_spool_id, the same
    binding ASSIGN_SPOOL and klipper_vars maintain)
  - set Spoolman's active spool so live usage accrues to the spool
    actually printing (gated by config `active_tool_sync`)

Park transitions (-1) are transient inside every toolchange, so they
update app_state but never touch Spoolman — otherwise a 4-tool print
would thrash the active spool twice per swap.

Wired from klipper_vars.on_ws_save_variables — INDX state arrives on
the same object stream as the spool bindings.
"""
from __future__ import annotations

import logging
import threading

import app_state
from moonraker_client import set_active_spool_id

logger = logging.getLogger(__name__)

# Last spool id pushed to Spoolman by this module; avoids repeat POSTs
# when Moonraker resends the full variables dict for unrelated changes.
# Protected by app_state.state_lock.
_last_synced_spool: int | None = None


def _resolve_bound_spool(tool: int, variables: dict) -> int | None:
    """Spool bound to T<tool>: the vars dict itself is authoritative
    (same delta stream that carries the binding), active_spools is the
    fallback for bindings made before this connection."""
    raw = variables.get(f"t{tool}_spool_id")
    try:
        spool_id = int(raw) if raw not in (None, "") else None
    except (TypeError, ValueError):
        spool_id = None
    if spool_id == 0:
        spool_id = None
    if spool_id is None:
        with app_state.state_lock:
            spool_id = app_state.active_spools.get(f"T{tool}")
    return spool_id


def _push_active_spool(spool_id: int, tool: int) -> None:
    """POST the active spool to Moonraker/Spoolman off the websocket
    thread; a slow HTTP call must not stall status dispatch."""
    moonraker = app_state.cfg.get("moonraker_url", "")
    if not moonraker:
        return

    def _worker() -> None:
        global _last_synced_spool
        try:
            set_active_spool_id(moonraker, spool_id)
        except Exception as exc:
            logger.error(
                "INDX sync: failed to set Spoolman active spool %s for T%d: %s",
                spool_id, tool, exc,
            )
            return
        with app_state.state_lock:
            _last_synced_spool = spool_id
        logger.info("INDX sync: T%d active -> Spoolman spool %s", tool, spool_id)

    threading.Thread(target=_worker, name="indx-spool-sync", daemon=True).start()


def on_active_tool(variables: dict) -> None:
    """Handle a save_variables delta that contains `active_tool`."""
    raw = variables.get("active_tool")
    try:
        tool = int(raw)
    except (TypeError, ValueError):
        logger.debug("INDX sync: ignoring non-integer active_tool=%r", raw)
        return

    parked = tool < 0
    with app_state.state_lock:
        prev = app_state.indx_active_tool
        app_state.indx_active_tool = None if parked else tool
        last_synced = _last_synced_spool
    if (None if parked else tool) == prev:
        return  # full-dict resend with no transition

    if parked:
        logger.debug("INDX sync: toolhead parked")
        return

    logger.info("INDX sync: active tool -> T%d", tool)

    if not app_state.cfg.get("active_tool_sync", True):
        return
    spool_id = _resolve_bound_spool(tool, variables)
    if spool_id is None:
        logger.info("INDX sync: T%d has no bound spool, leaving Spoolman as-is", tool)
        return
    if spool_id == last_synced:
        return
    _push_active_spool(spool_id, tool)
