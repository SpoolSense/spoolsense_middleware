"""
rest_api.py — FastAPI HTTP server for SpoolSense Mobile.

Runs alongside MQTT in a background thread. Mobile scans reuse the
existing detect_and_parse → _activate_from_scan pipeline.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

import app_state
from adapters.dispatcher import detect_and_parse
from activation import _activate_from_scan
from publishers.klipper import _send_gcode

logger = logging.getLogger(__name__)

app = FastAPI(title="SpoolSense Middleware", docs_url=None, redoc_url=None)


# --- Request/Response models ---

class MobileScanRequest(BaseModel):
    uid: str
    present: bool = True
    tag_data_valid: bool = False
    blank: bool = False
    material_type: str | None = None
    material_name: str | None = None
    manufacturer: str | None = None
    color: str | None = None
    remaining_g: float | None = None
    initial_weight_g: float | None = None
    spoolman_id: int | None = None


class AssignToolRequest(BaseModel):
    toolhead: str


class ApiResponse(BaseModel):
    success: bool
    message: str
    pending: bool | None = None
    replaced: bool | None = None
    action: str | None = None
    toolhead: str | None = None
    spool_id: int | None = None


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
        # Claim it — prevent concurrent assigns from using the same pending
        app_state.pending_spool = None

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
