"""
tracking_store.py — persist active_spool_tracking across restarts (#91).

The tracking dict holds the deduction baseline (weight at scan time) plus
UID/device/filament properties per target. It only changes on a scan or a
post-deduction re-baseline, so persisting on write is cheap — and without
it, every middleware restart silently broke deduction for every mounted
spool until rescanned. Spools stay mounted for weeks on toolchangers and
INDX setups.

Same pattern as the deductions store: atomic tmp+rename writes, tolerant
loads, snapshot under state_lock with file I/O outside it.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict

import app_state

logger = logging.getLogger(__name__)


def load_tracking() -> None:
    """Load persisted tracking records into app_state at startup."""
    if not os.path.exists(app_state.TRACKING_FILE):
        return
    try:
        with open(app_state.TRACKING_FILE) as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return
        records = {}
        for target, fields in data.items():
            if not isinstance(fields, dict) or not fields.get("uid"):
                continue
            try:
                records[target] = app_state.ActiveSpool(**fields)
            except TypeError:
                # Unknown/missing fields from an older or newer version —
                # skip the record rather than refuse to start
                logger.warning("tracking_store: skipping malformed record for %r", target)
        with app_state.state_lock:
            app_state.active_spool_tracking.update(records)
        if records:
            logger.info("Restored spool tracking for %d target(s): %s",
                        len(records), ", ".join(sorted(records)))
    except Exception:
        logger.exception("tracking_store: failed to load %s", app_state.TRACKING_FILE)


def save_tracking() -> None:
    """Persist the current tracking dict. Never raises."""
    try:
        with app_state.state_lock:
            snapshot = {t: asdict(rec) for t, rec in app_state.active_spool_tracking.items()}
        os.makedirs(os.path.dirname(app_state.TRACKING_FILE), exist_ok=True)
        tmp_path = app_state.TRACKING_FILE + ".tmp"
        with open(tmp_path, "w") as f:
            json.dump(snapshot, f, indent=2)
        os.replace(tmp_path, app_state.TRACKING_FILE)
    except Exception:
        logger.exception("tracking_store: failed to save %s", app_state.TRACKING_FILE)
