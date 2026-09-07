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


def choose_deduction_baseline(scan: "object", spool_info: "object | None") -> float | None:
    """Pick the UPDATE_TAG deduction baseline for a scanned spool (#119).

    Spoolman's remaining weight is authoritative whenever available: in AFC
    setups the scanner applies off-scanner deductions Spoolman-direct and
    never rewrites the tag, so the tag goes stale — and OpenTag3D v2
    nominal-weight tags never change at all. The tag weight is only a
    fallback for measured/legacy tags Spoolman doesn't know yet. A nominal
    tag with no Spoolman match gets no baseline rather than a wrong one
    that would double-deduct on every re-scan.

    scan needs: remaining_weight_g, weight_source, pending_deduction_g, uid.
    spool_info needs: spoolman_remaining_g (or be None).
    """
    spoolman_remaining = getattr(spool_info, "spoolman_remaining_g", None) if spool_info else None
    if spoolman_remaining is not None:
        pending_raw = getattr(scan, "pending_deduction_g", None)
        valid = isinstance(pending_raw, (int, float)) and pending_raw > 0
        pending = pending_raw if valid else 0.0
        return max(0.0, spoolman_remaining - pending)
    if getattr(scan, "weight_source", None) == "nominal":
        logger.info(
            "Baseline: uid=%s reports a nominal weight and has no Spoolman "
            "match — no deduction baseline until Spoolman knows the spool",
            getattr(scan, "uid", None))
        return None
    return getattr(scan, "remaining_weight_g", None)


def record_tracking(
    target: str,
    uid: str,
    device_id: str = "",
    remaining: float | None = None,
    diameter_mm: float | None = None,
    density: float | None = None,
    tag_format: str | None = None,
) -> bool:
    """Record the deduction baseline for the spool mounted on *target* and
    persist it. Requires a uid. remaining may be None (#119): the record
    still carries uid/device/format so usage-based (toolchanger) deductions
    and mobile deduction routing keep working; the AFC weight-delta path
    skips baselines of None.

    The uid is stored lowercased: tracking records are matched against
    scanner/mobile uids that arrive in either case."""
    if not target or not uid:
        return False

    with app_state.state_lock:
        app_state.active_spool_tracking[target] = app_state.ActiveSpool(
            uid=uid.lower(),
            device_id=device_id or "",
            weight_g=remaining,
            diameter_mm=diameter_mm or 1.75,
            density=density or 1.24,
            tag_format=tag_format or "unknown",
        )
    save_tracking()
    return True


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


def clear_tracking(*targets: str) -> None:
    """Remove tracking records for targets whose spool was ejected/cleared,
    and persist if anything was removed. Callers must NOT hold state_lock.
    A no-op for targets with no record, so poll loops can call it every
    cycle without file churn."""
    removed = False
    with app_state.state_lock:
        for target in targets:
            if app_state.active_spool_tracking.pop(target, None) is not None:
                removed = True
    if removed:
        save_tracking()


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
