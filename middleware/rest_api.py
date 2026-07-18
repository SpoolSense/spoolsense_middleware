"""
rest_api.py — FastAPI HTTP server for SpoolSense Mobile and Web Config Panel.

Runs alongside MQTT in a background thread. Mobile scans reuse the
existing detect_and_parse → _activate_from_scan pipeline. Web config
panel served as static HTML at the root.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import signal
import subprocess
import threading
from pathlib import Path
from typing import Any, Optional

import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

import app_state
from adapters.dispatcher import detect_and_parse
from activation import _activate_from_scan, _cache_pending_spool
from moonraker_client import send_gcode
from mqtt_handler import _record_spool_tracking, _get_scanner_target
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
    # Sent by the iOS app since 1.0.0 — previously ignored by this model (#101)
    min_print_temp: Optional[int] = None
    max_print_temp: Optional[int] = None
    min_bed_temp: Optional[int] = None
    max_bed_temp: Optional[int] = None
    filament_length_m: Optional[float] = None


class AssignToolRequest(BaseModel):
    toolhead: str
    uid: Optional[str] = None            # uid from the originating scan — binds the assignment to that spool (#31)
    spool_id: Optional[int] = None       # spoolman id from the scan — rides along with uid for newer clients


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
        pending_afc = app_state.pending_spool_afc.copy() if app_state.pending_spool_afc else None
        pending_toolhead = app_state.pending_spool_toolhead.copy() if app_state.pending_spool_toolhead else None
        locked = [k for k, v in app_state.lane_locks.items() if v]
        indx_tool = app_state.indx_active_tool

    return {
        "active_spools": active,
        "active_tool": indx_tool,
        # Legacy combined view (web panel pending dot) + explicit slots
        "pending_spool": pending_toolhead or pending_afc,
        "pending_spool_afc": pending_afc,
        "pending_spool_toolhead": pending_toolhead,
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
        replaced = _cache_pending_spool(
            "toolhead",
            scan.color_hex or "FFFFFF",
            scan.material_name or scan.material_type or "Unknown",
            scan.remaining_weight_g,
            scan.scanner_spoolman_id,
            scan.nozzle_temp_min_c,
            scan.nozzle_temp_max_c,
            scan.bed_temp_min_c,
            scan.bed_temp_max_c,
            uid=scan.uid,
            device_id="",
            diameter_mm=scan.diameter_mm,
            density=scan.density,
            tag_format=req.tag_format or scan.source,
        )

        msg = (
            "Previous pending spool replaced — select a toolhead to assign"
            if replaced
            else "Spool cached — select a toolhead to assign"
        )
        return ApiResponse(success=True, message=msg, pending=True, replaced=replaced)

    # happy_hare_stage: cache as pending, phone picks a gate next. The bind
    # writes to Spoolman, so the spool must exist there — resolve now and
    # tell the user at scan time rather than failing at assign time.
    if action == "happy_hare_stage":
        # The UID is the authority: scanner_spoolman_id is a hint that can go
        # stale when a tag is relinked to a different spool. Config requires
        # spoolman_url for this action, so a missing client is a hard error.
        spool_id: Optional[int] = None
        if scan.uid and app_state.spoolman_client is not None:
            spool = app_state.spoolman_client.find_by_nfc(scan.uid)
            if spool:
                spool_id = spool.get("id")
                hint = scan.scanner_spoolman_id
                if hint is not None and hint != spool_id:
                    logger.info(
                        f"[mobile] uid {scan.uid} resolves to spool {spool_id}; "
                        f"ignoring stale payload id {hint}")
        if spool_id is None:
            return ApiResponse(
                success=False,
                message="Spool not found in Spoolman — register the tag first",
            )
        replaced = _cache_pending_spool(
            "toolhead",
            scan.color_hex or "FFFFFF",
            scan.material_name or scan.material_type or "Unknown",
            scan.remaining_weight_g,
            spool_id,
            scan.nozzle_temp_min_c,
            scan.nozzle_temp_max_c,
            scan.bed_temp_min_c,
            scan.bed_temp_max_c,
            uid=scan.uid,
            device_id="",
            diameter_mm=scan.diameter_mm,
            density=scan.density,
            tag_format=req.tag_format or scan.source,
        )
        msg = (
            "Previous pending spool replaced — select a gate to assign"
            if replaced
            else "Spool cached — select a gate to assign"
        )
        return ApiResponse(success=True, message=msg, pending=True,
                           replaced=replaced, spool_id=spool_id)

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

    _activate_from_scan(scanner_cfg, scan, spool_info=spool_info, device_id="mobile")

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


_GATE_NAME = re.compile(r"^G(\d+)$")


def _assign_gate(req: AssignToolRequest) -> ApiResponse:
    """Gate-targeted Happy Hare assignment: claim the staged spool, bind it
    to the requested gate, restore the stage on failure. Same uid/409
    machinery as the toolhead flow — the shipped app's status-code contract
    is identical."""
    toolheads = app_state.cfg.get("toolheads", [])
    match = _GATE_NAME.match(req.toolhead.upper())
    if not match or (toolheads and req.toolhead.upper() not in toolheads):
        return ApiResponse(
            success=False,
            message=f"Invalid gate — available: {', '.join(toolheads) or 'none (set happy_hare.num_gates)'}",
        )
    gate = int(match.group(1))

    with app_state.state_lock:
        pending = app_state.pending_spool_toolhead
        if not pending:
            raise HTTPException(status_code=409, detail={
                "code": "no_pending_spool",
                "message": "No pending spool — scan a tag first",
            })
        if req.uid and req.uid.lower() != (pending.get("uid") or "").lower():
            raise HTTPException(status_code=409, detail={
                "code": "pending_spool_changed",
                "message": "Pending spool changed since scan — rescan and try again",
            })
        # Claim it — unlike the toolhead flow there is no macro watcher; the
        # endpoint is the consumer. Record the stage generation so a failed
        # bind can tell "nothing changed" from "a newer scan came and went"
        app_state.pending_spool_toolhead = None
        claimed_gen = app_state.pending_spool_toolhead_gen

    spool_id = pending.get("spoolman_id")
    if spool_id is None:
        # Staging requires a Spoolman id, so this should be unreachable —
        # but never bind nothing
        return ApiResponse(success=False, message="Staged spool has no Spoolman id — rescan")

    from happy_hare import bind_spool_to_gate
    if bind_spool_to_gate(gate, spool_id):
        logger.info(f"[mobile] Bound spool {spool_id} to gate {gate}")
        return ApiResponse(success=True,
                           message=f"Spool bound to gate {gate}",
                           action="happy_hare_stage",
                           toolhead=req.toolhead.upper(),
                           spool_id=spool_id)

    # Failed bind — restore the stage only if NOTHING was staged since the
    # claim. An empty slot alone isn't enough: a newer scan could have been
    # staged and consumed by a concurrent assign during our network call,
    # and restoring then would resurrect a stale spool (assignable by
    # legacy clients that omit the uid guard).
    with app_state.state_lock:
        if (app_state.pending_spool_toolhead is None
                and app_state.pending_spool_toolhead_gen == claimed_gen):
            app_state.pending_spool_toolhead = pending
    raise HTTPException(status_code=502,
                        detail="Happy Hare bind failed — see middleware log")


@app.post("/api/assign-tool", response_model=ApiResponse)
def assign_tool(req: AssignToolRequest) -> ApiResponse:
    mobile_cfg = app_state.cfg.get("mobile", {})
    if not mobile_cfg.get("enabled"):
        raise HTTPException(status_code=503, detail="Mobile scanning not enabled")

    if mobile_cfg.get("action") == "happy_hare_stage":
        return _assign_gate(req)

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
        pending = app_state.pending_spool_toolhead
        if not pending:
            # detail carries a machine-readable code for clients that parse
            # error bodies. The shipped iOS 1.0.0 build ignores non-2xx bodies
            # entirely (status code only), so this is free to evolve; the
            # status codes themselves are the frozen contract.
            raise HTTPException(status_code=409, detail={
                "code": "no_pending_spool",
                "message": "No pending spool — scan a tag first",
            })
        # Spool binding (spoolsense-mobile #31): newer clients echo back the uid
        # they scanned. The toolhead pending slot is last-write-wins, so a scan
        # from any phone landing between mobile-scan and assign-tool would hand
        # this assignment the wrong spool — reject before any gcode goes out.
        # Case-insensitive: mobile sends uppercase hex, parsers store lowercase.
        # Omitted uid = legacy client, keep the old unchecked behavior.
        if req.uid and req.uid.lower() != (pending.get("uid") or "").lower():
            raise HTTPException(status_code=409, detail={
                "code": "pending_spool_changed",
                "message": "Pending spool changed since scan — rescan and try again",
            })
        # Don't clear the pending slot here — toolchanger_status.py watcher
        # consumes it when it detects the ASSIGN_SPOOL macro variable change

    moonraker = app_state.cfg.get("moonraker_url", "")
    if not moonraker:
        return ApiResponse(success=False, message="Moonraker URL not configured")

    try:
        send_gcode(moonraker, f"ASSIGN_SPOOL TOOL={toolhead}")
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
        os.makedirs(os.path.dirname(app_state.DEDUCTIONS_FILE), exist_ok=True)
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
    # New in 1.8.4 — shipped mobile 1.0.0 requires success + cleared_g to stay;
    # extra fields are ignored by its decoder
    remaining_pending_g: float = 0.0


@app.get("/api/deductions/{uid}", response_model=DeductionResponse)
def get_deduction(uid: str) -> DeductionResponse:
    """Return pending deduction for a UID. Returns 0 if none."""
    with app_state.state_lock:
        pending = app_state.pending_mobile_deductions.get(uid.lower(), 0.0)
    return DeductionResponse(pending_g=pending)


@app.post("/api/deductions/{uid}/applied", response_model=DeductionConfirmResponse)
async def confirm_deduction(uid: str, request: Request) -> DeductionConfirmResponse:
    """Acknowledge a pending deduction after the mobile app writes the tag.

    No body / empty ``{}``: clear the full pending value (legacy behavior —
    shipped mobile 1.0.0 posts an empty body and expects a full clear).

    ``{"applied_g": <float>}``: subtract only what the app actually applied
    and retain the remainder as still-pending, so a partial tag write no
    longer forfeits (or double-counts on retry) the rest. An ``applied_g``
    above the pending value clears everything — clock skew between the
    phone's apply and a concurrent scanner deduction makes small overshoots
    legitimate — and can never drive pending negative.

    NOT idempotent: each post subtracts again. A retry after an ambiguous
    outcome (timeout, transport error) must re-GET /api/deductions/{uid}
    and reconcile before posting, rather than blindly reposting.
    """
    uid_lower = uid.lower()

    # Parse the body by hand: FastAPI binds a top-level JSON `null` to the
    # parameter default, making it indistinguishable from an absent body —
    # and a malformed payload must never alias the destructive legacy clear.
    raw = await request.body()
    if not raw or not raw.strip():
        body: dict = {}
    else:
        try:
            body = json.loads(raw)
        except ValueError:
            body = None  # type: ignore[assignment]
        if not isinstance(body, dict):
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "invalid_body",
                    "message": "body must be a JSON object",
                },
            )

    if not body:
        # Legacy full clear — only an absent or empty body qualifies. A
        # nonempty body without a valid applied_g is rejected below so a
        # misspelled field can't silently clear everything.
        with app_state.state_lock:
            cleared = app_state.pending_mobile_deductions.pop(uid_lower, 0.0)
        if cleared > 0:
            _save_deductions()
            logger.info(f"Mobile deduction: cleared {cleared:.1f}g for {uid_lower}")
        return DeductionConfirmResponse(success=True, cleared_g=cleared,
                                        remaining_pending_g=0.0)

    raw_applied = body.get("applied_g")
    # bool is an int subclass — reject it along with everything non-numeric.
    # NaN/Infinity pass numeric comparisons and an arbitrarily large JSON
    # integer overflows float conversion, so normalize to a finite float
    # before any state is touched.
    applied: Optional[float] = None
    if not isinstance(raw_applied, bool) and isinstance(raw_applied, (int, float)):
        try:
            applied = float(raw_applied)
        except OverflowError:
            applied = None
    if applied is None or not math.isfinite(applied) or applied <= 0:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "invalid_applied_g",
                "message": "applied_g must be a finite number greater than 0",
            },
        )

    with app_state.state_lock:
        pending = app_state.pending_mobile_deductions.get(uid_lower, 0.0)
        cleared = min(applied, pending)
        remaining = pending - cleared
        if remaining <= 1e-9:  # float dust from the subtraction is a full clear
            remaining = 0.0
            app_state.pending_mobile_deductions.pop(uid_lower, None)
        else:
            app_state.pending_mobile_deductions[uid_lower] = remaining
    if cleared > 0:
        _save_deductions()
        logger.info(
            f"Mobile deduction: applied {cleared:.1f}g for {uid_lower} "
            f"({remaining:.1f}g still pending)"
        )
    return DeductionConfirmResponse(success=True, cleared_g=cleared,
                                    remaining_pending_g=remaining)


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

        # Restart so the new config takes effect. In Docker there is no
        # systemctl — a clean self-terminate lets the container's restart
        # policy relaunch with the new config (SPOOLSENSE_IN_DOCKER is set
        # by the image). The short delay lets this response reach the client.
        if os.environ.get("SPOOLSENSE_IN_DOCKER"):
            threading.Timer(0.5, lambda: os.kill(os.getpid(), signal.SIGTERM)).start()
        else:
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
