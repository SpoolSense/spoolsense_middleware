"""
rest_api.py — FastAPI HTTP server for SpoolSense Mobile and Web Config Panel.

Runs alongside MQTT in a background thread. Mobile scans reuse the
existing detect_and_parse → _activate_from_scan pipeline. Web config
panel served as static HTML at the root.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

import yaml
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

import app_state
from adapters.dispatcher import detect_and_parse
from activation import _activate_from_scan
from mqtt_handler import _record_spool_tracking, _get_scanner_target
from publishers.klipper import _send_gcode
from config import CONFIG_PATH

logger = logging.getLogger(__name__)

app = FastAPI(title="SpoolSense Middleware", docs_url=None, redoc_url=None)

# Serve the web config panel at the root
_STATIC_DIR = Path(__file__).parent / "static"


# --- Request/Response models ---

class MobileScanRequest(BaseModel):
    uid: str
    present: bool = True
    tag_data_valid: bool = False
    blank: bool = False
    tag_format: Optional[str] = None                # "openprinttag", "opentag3d", "tigertag", etc.
    material_type: Optional[str] = None
    material_name: Optional[str] = None
    manufacturer: Optional[str] = None
    color: Optional[str] = None
    remaining_g: Optional[float] = None
    initial_weight_g: Optional[float] = None
    spoolman_id: Optional[int] = None
    density: Optional[float] = None
    diameter_mm: Optional[float] = None


class AssignToolRequest(BaseModel):
    toolhead: str


class ApiResponse(BaseModel):
    success: bool
    message: str
    pending: Optional[bool] = None
    replaced: Optional[bool] = None
    action: Optional[str] = None
    toolhead: Optional[str] = None
    spool_id: Optional[int] = None


# --- Endpoints ---

@app.get("/api/config")
def get_config() -> dict[str, Any]:
    mobile_cfg = app_state.cfg.get("mobile", {})
    scanners_cfg = app_state.cfg.get("scanners", {})
    toolheads = app_state.cfg.get("toolheads", [])

    scanners_view = []
    for device_id, cfg in scanners_cfg.items():
        scanners_view.append({
            "device_id": device_id,
            "action": cfg.get("action"),
            "target": cfg.get("lane") or cfg.get("toolhead"),
        })

    return {
        "mobile": {
            "enabled": mobile_cfg.get("enabled", False),
            "action": mobile_cfg.get("action", "afc_stage"),
            "toolheads": toolheads,
        },
        "scanners": scanners_view,
        "spoolman_url": app_state.cfg.get("spoolman_url", ""),
    }


@app.get("/api/status")
def get_status() -> dict[str, Any]:
    with app_state.state_lock:
        active = dict(app_state.active_spools)
        pending = app_state.pending_spool.copy() if app_state.pending_spool else None
        locked = [k for k, v in app_state.lane_locks.items() if v]

    return {
        "active_spools": active,
        "pending_spool": pending,
        "locked_targets": locked,
    }


def _configured_targets() -> set[str]:
    """All `lane`/`toolhead` values present in scanner config — what the lock
    gate keys off. Used to validate unlock requests so callers can't poison
    `lane_locks` with arbitrary keys."""
    targets: set[str] = set()
    for s in app_state.cfg.get("scanners", {}).values():
        if isinstance(s, dict):
            t = s.get("lane") or s.get("toolhead")
            if t:
                targets.add(t)
    return targets


@app.post("/api/unlock/{target}", response_model=ApiResponse)
def unlock_target(target: str) -> ApiResponse:
    """
    Explicit unlock for a locked toolhead or AFC lane (#76).

    Use cases:
      - HA automation that wants to drop the lock on a button press
      - User stuck in the lock-out scenario where idle-detection didn't fire
      - Power users scripting against the middleware

    Idempotent: unlocking an already-unlocked target returns success.
    """
    if target not in _configured_targets():
        raise HTTPException(
            status_code=404,
            detail=f"Unknown target '{target}'. Configured targets: "
                   f"{sorted(_configured_targets())}",
        )

    with app_state.state_lock:
        was_locked = bool(app_state.lane_locks.get(target))
        app_state.lane_locks[target] = False

    if was_locked:
        logger.info(f"REST API: explicit unlock for {target}")
        return ApiResponse(
            success=True,
            message=f"Unlocked {target}",
            toolhead=target,
        )
    return ApiResponse(
        success=True,
        message=f"{target} was already unlocked",
        toolhead=target,
    )


@app.post("/api/mobile-scan", response_model=ApiResponse)
def mobile_scan(req: MobileScanRequest) -> ApiResponse:
    mobile_cfg = app_state.cfg.get("mobile", {})
    if not mobile_cfg.get("enabled"):
        raise HTTPException(status_code=503, detail="Mobile scanning not enabled")

    action = mobile_cfg["action"]

    # Build payload dict matching scanner MQTT format
    payload = req.model_dump(exclude_none=True)
    payload["source"] = "mobile"

    try:
        scan = detect_and_parse(payload, target_id="mobile")
    except Exception as e:
        logger.exception("Failed to parse mobile scan payload")
        return ApiResponse(success=False, message=f"Parse error: {e}")

    if not scan.present:
        return ApiResponse(success=False, message="No tag present")

    # toolhead_stage: cache as pending, phone picks toolhead next
    if action == "toolhead_stage":
        with app_state.state_lock:
            replaced = app_state.pending_spool is not None
            app_state.pending_spool = {
                "color_hex": scan.color_hex or "FFFFFF",
                "material": scan.material_name or scan.material_type or "Unknown",
                "remaining_g": scan.remaining_weight_g,
                "spoolman_id": scan.scanner_spoolman_id,
                "uid": scan.uid,
            }

        msg = (
            "Previous pending spool replaced — select a toolhead to assign"
            if replaced
            else "Spool cached — select a toolhead to assign"
        )
        return ApiResponse(success=True, message=msg, pending=True, replaced=replaced)

    # All other actions: activate immediately
    scanner_cfg: dict[str, Any] = {"action": action}
    if action == "toolhead":
        scanner_cfg["toolhead"] = mobile_cfg.get("toolhead")
    elif action == "afc_lane":
        scanner_cfg["lane"] = mobile_cfg.get("lane")

    # Spoolman enrichment (best-effort)
    spool_info = None
    if app_state.spoolman_client and scan.tag_data_valid:
        try:
            spool_info = app_state.spoolman_client.sync_spool_from_scan(scan, prefer_tag=True)
        except Exception:
            logger.exception("Spoolman sync failed for mobile scan — continuing with tag-only")

    _activate_from_scan(scanner_cfg, scan, spool_info=spool_info)

    # Record for UPDATE_TAG deduction tracking — device_id="" signals mobile-scanned
    target = _get_scanner_target(scanner_cfg)
    if target and scan.uid:
        _record_spool_tracking(
            target, scan.uid.lower(), "",                               # empty device_id = mobile
            scan.remaining_weight_g, scan.diameter_mm,
            getattr(scan, "density", None),
            tag_format=req.tag_format or "unknown",
        )

    spool_id = spool_info.spoolman_id if spool_info else scan.scanner_spoolman_id
    return ApiResponse(
        success=True,
        message="Spool activated",
        pending=False,
        action=action,
        spool_id=spool_id,
    )


@app.post("/api/assign-tool", response_model=ApiResponse)
def assign_tool(req: AssignToolRequest) -> ApiResponse:
    mobile_cfg = app_state.cfg.get("mobile", {})
    if not mobile_cfg.get("enabled"):
        raise HTTPException(status_code=503, detail="Mobile scanning not enabled")

    if mobile_cfg.get("action") != "toolhead_stage":
        return ApiResponse(success=False, message="assign-tool only valid for toolhead_stage mode")

    toolheads = app_state.cfg.get("toolheads", [])
    toolhead = req.toolhead.upper()
    if toolheads and toolhead not in toolheads:
        return ApiResponse(
            success=False,
            message=f"Invalid toolhead — available: {', '.join(toolheads)}",
        )

    with app_state.state_lock:
        pending = app_state.pending_spool
        if not pending:
            raise HTTPException(status_code=409, detail="No pending spool — scan a tag first")
        # Don't clear pending_spool here — toolchanger_status.py watcher
        # consumes it when it detects the ASSIGN_SPOOL macro variable change

    moonraker = app_state.cfg.get("moonraker_url", "")
    if not moonraker:
        return ApiResponse(success=False, message="Moonraker URL not configured")

    try:
        _send_gcode(moonraker, f"ASSIGN_SPOOL TOOL={toolhead}")
        logger.info(f"[mobile] Sent ASSIGN_SPOOL TOOL={toolhead}")
    except Exception as e:
        logger.exception("Failed to send ASSIGN_SPOOL gcode")
        raise HTTPException(status_code=502, detail=f"Moonraker gcode call failed: {e}")

    return ApiResponse(
        success=True,
        message=f"Assigned to {toolhead}",
        toolhead=toolhead,
        spool_id=pending.get("spoolman_id"),
    )


# ── Deduction persistence ───────────────────────────────────────────────────

def _load_deductions() -> None:
    """Load pending deductions from disk into app_state on startup."""
    if not os.path.exists(app_state.DEDUCTIONS_FILE):
        return
    try:
        with open(app_state.DEDUCTIONS_FILE) as f:
            data = json.load(f)
        if isinstance(data, dict):
            with app_state.state_lock:
                app_state.pending_mobile_deductions = {k.lower(): float(v) for k, v in data.items()}
            logger.info(f"Loaded {len(data)} pending mobile deductions from disk")
    except Exception:
        logger.exception("Failed to load deductions file")


def _save_deductions() -> None:
    """Persist pending deductions to disk so they survive middleware restarts.
    Uses atomic write (temp file + rename) to prevent corruption on crash."""
    with app_state.state_lock:
        snapshot = dict(app_state.pending_mobile_deductions)
    try:
        tmp_path = app_state.DEDUCTIONS_FILE + ".tmp"
        with open(tmp_path, "w") as f:
            json.dump(snapshot, f)
        os.replace(tmp_path, app_state.DEDUCTIONS_FILE)
    except Exception:
        logger.exception("Failed to save deductions file")


def store_mobile_deduction(uid: str, grams: float) -> None:
    """Store a pending deduction for a mobile-scanned spool. Accumulates if entry exists."""
    uid_lower = uid.lower()
    with app_state.state_lock:
        current = app_state.pending_mobile_deductions.get(uid_lower, 0.0)
        app_state.pending_mobile_deductions[uid_lower] = current + grams
    _save_deductions()
    logger.info(f"Mobile deduction: stored {grams:.1f}g for {uid_lower} (total: {current + grams:.1f}g)")


# ── Deduction endpoints ─────────────────────────────────────────────────────

class DeductionResponse(BaseModel):
    pending_g: float


class DeductionConfirmResponse(BaseModel):
    success: bool
    cleared_g: float


@app.get("/api/deductions/{uid}", response_model=DeductionResponse)
def get_deduction(uid: str) -> DeductionResponse:
    """Return pending deduction for a UID. Returns 0 if none."""
    with app_state.state_lock:
        pending = app_state.pending_mobile_deductions.get(uid.lower(), 0.0)
    return DeductionResponse(pending_g=pending)


@app.post("/api/deductions/{uid}/applied", response_model=DeductionConfirmResponse)
def confirm_deduction(uid: str) -> DeductionConfirmResponse:
    """Clear a pending deduction after the mobile app confirms the tag write succeeded."""
    uid_lower = uid.lower()
    with app_state.state_lock:
        cleared = app_state.pending_mobile_deductions.pop(uid_lower, 0.0)
    if cleared > 0:
        _save_deductions()
        logger.info(f"Mobile deduction: cleared {cleared:.1f}g for {uid_lower}")
    return DeductionConfirmResponse(success=True, cleared_g=cleared)


# ── Web config panel endpoints ───────────────────────────────────────────────

@app.get("/api/full-config")
def get_full_config() -> dict[str, Any]:
    """Return the full middleware config for the web panel."""
    from spoolsense import __version__
    cfg = dict(app_state.cfg)
    cfg["_version"] = __version__
    return cfg


class SaveConfigRequest(BaseModel):
    moonraker_url: str
    spoolman_url: str = ""
    mqtt: dict
    low_spool_threshold: int = 100
    scanner_topic_prefix: str = "spoolsense"
    tag_writeback_enabled: bool = False
    publish_lane_data: bool = False


@app.post("/api/save-config")
def save_config(req: SaveConfigRequest) -> dict[str, Any]:
    """Write updated config to config.yaml and restart the middleware."""
    try:
        # Read existing config to preserve fields we don't edit (scanners, toolheads, mobile)
        with open(CONFIG_PATH) as f:
            existing = yaml.safe_load(f) or {}

        # Update only the fields the web panel controls
        existing["moonraker_url"] = req.moonraker_url
        existing["spoolman_url"] = req.spoolman_url
        existing["mqtt"] = {**existing.get("mqtt", {}), **req.mqtt}
        existing["low_spool_threshold"] = req.low_spool_threshold
        existing["scanner_topic_prefix"] = req.scanner_topic_prefix
        existing["tag_writeback_enabled"] = req.tag_writeback_enabled
        existing["publish_lane_data"] = req.publish_lane_data

        # Atomic write — temp file + rename
        tmp_path = CONFIG_PATH + ".tmp"
        with open(tmp_path, "w") as f:
            yaml.dump(existing, f, default_flow_style=False, sort_keys=False)
        os.replace(tmp_path, CONFIG_PATH)
        logger.info("Web panel: config saved to %s", CONFIG_PATH)

        # Restart the middleware service
        try:
            subprocess.Popen(
                ["sudo", "systemctl", "restart", "spoolsense"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except Exception:
            logger.warning("Web panel: could not restart spoolsense service")

        return {"success": True, "message": "Config saved — restarting"}
    except Exception as e:
        logger.exception("Web panel: failed to save config")
        return {"success": False, "message": str(e)}


# ── Serve web panel at root ──────────────────────────────────────────────────

@app.get("/")
def serve_panel():
    """Serve the web config panel HTML."""
    return FileResponse(_STATIC_DIR / "index.html")
