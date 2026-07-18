"""
klipper_vars.py — Klipper save_variables sync via Moonraker websocket.

Klipper exposes save_variables as a printer object, so a manual spool
change in the UI (SAVE_VARIABLE t0_spool_id=...) arrives as a websocket
status delta — no file watching required. Replaces the old watchdog-based
var_watcher.py (#85). Used for single and toolchanger modes only — AFC
mode uses afc_status.py instead.

Wired up in spoolsense.py: moonraker_ws.on_save_variables = on_ws_save_variables
"""
from __future__ import annotations

import logging
import re

import app_state

logger = logging.getLogger(__name__)

# Matches real toolhead names (T0, T10, T99). cfg["toolheads"] can also
# contain AFC lane targets in mixed configs — those have no Klipper
# t<N>_spool_id variable and must be skipped entirely, or an unrelated
# save_variables event would clear their AFC-managed active_spools slot.
_TOOLHEAD_RE = re.compile(r"[Tt](\d+)")


def on_ws_save_variables(variables: dict) -> None:
    """
    Websocket callback for save_variables deltas.

    Moonraker sends the complete `variables` dict whenever any saved
    variable changes (it is a single object field, so deltas are not
    partial). Reads t<N>_spool_id for each configured toolhead and
    syncs app_state.active_spools.

    Note: active_spools has two authoritative writers for toolheads —
    this callback (Klipper vars) and toolhead_status.py (Spoolman's
    active spool_id poll). In the normal flow SAVE_VARIABLE keeps the
    var aligned with the active spool, so they agree; if an external
    actor changes the spool without SAVE_VARIABLE, the next vars delta
    re-asserts the var's value.
    """
    if not isinstance(variables, dict):
        return

    if "active_tool" in variables:
        # Bondtech INDX publishes the mounted tool here (see indx_status.py);
        # inert on printers that never write the variable.
        from indx_status import on_active_tool
        on_active_tool(variables)

    for t in app_state.cfg.get("toolheads", []):
        m = _TOOLHEAD_RE.fullmatch(t)
        if not m:
            continue
        var_name = f"t{m.group(1)}_spool_id"
        raw = variables.get(var_name)

        spool_id: int | None
        try:
            # Klipper stores native types; 0 means ejected (same convention
            # as toolhead_status.py). Strings tolerated for robustness.
            spool_id = int(raw) if raw not in (None, "") else None
        except (TypeError, ValueError):
            logger.debug("Klipper sync: ignoring non-integer %s=%r", var_name, raw)
            continue
        if spool_id == 0:
            spool_id = None

        with app_state.state_lock:
            current = app_state.active_spools.get(t)
            changed = current != spool_id
            if changed:
                app_state.active_spools[t] = spool_id

        if changed:
            # Any externally-driven change means the tracked baseline no
            # longer describes what's mounted — drop it until the next scan
            from tracking_store import clear_tracking
            clear_tracking(t)
            if spool_id is None:
                logger.info(f"Klipper sync: {t} cleared")
            else:
                logger.info(f"Klipper sync: {t} -> spool {spool_id}")
