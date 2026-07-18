"""
indx_status.py — Bondtech INDX active-tool sync (#91).

INDX's published Klipper macros (BondtechAB/INDX `macros/`) persist the
mounted tool in `save_variables.active_tool`: the index is written after
every successful pickup and set to -1 when the toolhead parks. Consuming
that variable from the existing save_variables websocket stream gives
real-time knowledge of which tool is printing — no polling, no
INDX-specific transport, and setups that never write `active_tool`
(every non-INDX printer) are completely unaffected.

On each pickup (active_tool -> N >= 0), and on a rebind of the mounted
tool (t<N>_spool_id changing while N stays mounted):
  - remember the tool in app_state.indx_active_tool
  - resolve the spool bound to that tool (t<N>_spool_id, the same
    binding ASSIGN_SPOOL and klipper_vars maintain)
  - set Spoolman's active spool so live usage accrues to the spool
    actually printing (gated by config `active_tool_sync`)

Park transitions (-1) are transient inside every toolchange, so they
update app_state but never touch Spoolman — otherwise a 4-tool print
would thrash the active spool twice per swap.

Moonraker resends the complete variables dict on any save_variables
change; identical (tool, spool) observations are ignored so unrelated
variable writes don't repost. Genuine transitions always POST — other
components also write Spoolman's active spool, so no assumption is made
about its current value.

Spoolman POSTs run on one serialized latest-wins worker: rapid
toolchanges coalesce and the newest transition is guaranteed to be the
last one applied, which per-transition threads could not guarantee.

Wired from klipper_vars.on_ws_save_variables — INDX state arrives on
the same object stream as the spool bindings.
"""
from __future__ import annotations

import logging
import threading

import app_state
from moonraker_client import set_active_spool_id

logger = logging.getLogger(__name__)

# Last (tool, spool) pair observed from the variables stream — dedupes
# Moonraker's full-dict resends. Distinct from "last synced": genuine
# transitions always POST. Protected by app_state.state_lock.
_last_observed: tuple[int, int | None] | None = None

# Latest-wins sync target consumed by the single worker thread.
# Protected by app_state.state_lock.
_pending_sync: tuple[int, int] | None = None   # (spool_id, tool)
_worker_running: bool = False


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
            fallback = app_state.active_spools.get(f"T{tool}")
        spool_id = fallback if isinstance(fallback, int) else None
    return spool_id


def _sync_worker() -> None:
    """Drain _pending_sync until empty, always applying the newest target.
    One instance runs at a time; each POST happens outside the lock."""
    global _pending_sync, _worker_running
    while True:
        with app_state.state_lock:
            target = _pending_sync
            _pending_sync = None
            if target is None:
                _worker_running = False
                return
        spool_id, tool = target
        moonraker = app_state.cfg.get("moonraker_url", "")
        if not moonraker:
            continue
        try:
            set_active_spool_id(moonraker, spool_id)
            logger.info("INDX sync: T%d active -> Spoolman spool %s", tool, spool_id)
        except Exception as exc:
            logger.error(
                "INDX sync: failed to set Spoolman active spool %s for T%d: %s",
                spool_id, tool, exc,
            )


def _enqueue_sync(spool_id: int, tool: int) -> None:
    """Replace any not-yet-applied target and ensure the worker runs."""
    global _pending_sync, _worker_running
    with app_state.state_lock:
        _pending_sync = (spool_id, tool)
        start = not _worker_running
        if start:
            _worker_running = True
    if start:
        threading.Thread(target=_sync_worker, name="indx-spool-sync",
                         daemon=True).start()


def on_active_tool(variables: dict) -> None:
    """Handle a save_variables delta that contains `active_tool`."""
    global _last_observed
    raw = variables.get("active_tool")
    try:
        tool = int(raw)
    except (TypeError, ValueError):
        logger.debug("INDX sync: ignoring non-integer active_tool=%r", raw)
        return

    if tool < 0:
        with app_state.state_lock:
            was_mounted = app_state.indx_active_tool is not None
            app_state.indx_active_tool = None
            _last_observed = (tool, None)
        if was_mounted:
            logger.debug("INDX sync: toolhead parked")
        return

    spool_id = _resolve_bound_spool(tool, variables)
    observed = (tool, spool_id)
    with app_state.state_lock:
        if _last_observed == observed:
            return  # full-dict resend, nothing changed
        transition = app_state.indx_active_tool != tool
        app_state.indx_active_tool = tool
        _last_observed = observed

    if transition:
        logger.info("INDX sync: active tool -> T%d", tool)

    if not app_state.cfg.get("active_tool_sync", True):
        return
    if spool_id is None:
        logger.info("INDX sync: T%d has no bound spool, leaving Spoolman as-is", tool)
        return
    _enqueue_sync(spool_id, tool)
