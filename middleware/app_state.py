"""
app_state.py — Shared mutable process state.

All runtime state lives here: config, caches, locks, sync service references.
Every other module reads/writes state via `import app_state`. Multi-thread
access to shared fields is protected by state_lock.
"""
from __future__ import annotations

import os
import threading
from dataclasses import dataclass

import paho.mqtt.client as mqtt

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from afc_status import AfcStatusSync
    from moonraker_ws import MoonrakerWebsocket
    from publisher_manager import PublisherManager
    from toolchanger_status import ToolchangerStatusSync
    from toolhead_status import ToolheadStatusSync
    from filament_usage import FilamentUsageSync

# Dispatcher availability — set at import time
try:
    from adapters.dispatcher import detect_and_parse, detect_format  # noqa: F401
    from state.models import ScanEvent  # noqa: F401
    from spoolman.client import SpoolmanClient
    DISPATCHER_AVAILABLE: bool = True
except ImportError:
    DISPATCHER_AVAILABLE: bool = False
    SpoolmanClient = None  # type: ignore[assignment,misc]

# Runtime configuration — populated by main() in spoolsense.py
cfg: dict = {}
spoolman_client: SpoolmanClient | None = None
mqtt_client: mqtt.Client | None = None
afc_status_sync: AfcStatusSync | None = None
toolchanger_status_sync: ToolchangerStatusSync | None = None
toolhead_status_sync: ToolheadStatusSync | None = None
filament_usage_sync: FilamentUsageSync | None = None
publisher_manager: PublisherManager | None = None
moonraker_ws: MoonrakerWebsocket | None = None

# Lane state — protected by state_lock for thread-safe access
# from MQTT callback thread and AFC polling thread
state_lock: threading.Lock = threading.Lock()
lane_locks: dict = {}
active_spools: dict = {}
lane_statuses: dict = {}

# Tracks the physical load state (True/False) per lane from the last AFC poll.
# Used to detect load transitions (False → True) for pending spool delivery.
# Protected by state_lock.
lane_load_states: dict[str, bool] = {}

# Pending spool data from staged scans, one slot per consumer so a scan
# meant for AFC can never be consumed by the toolchanger poller (or vice
# versa) in mixed-scanner configs. afc_status consumes the AFC slot on
# lane load; toolchanger_status (and the mobile assign flow) consume the
# toolhead slot on ASSIGN_SPOOL. Protected by state_lock.
pending_spool_afc: dict | None = None

# Currently mounted INDX tool (int) or None when parked / not an INDX
# setup. Written by indx_status.on_active_tool. Protected by state_lock.
indx_active_tool: int | None = None
pending_spool_toolhead: dict | None = None

# Tag writeback cooldown — tracks recent writes to prevent loops.
# Maps uid → timestamp of the last write command sent.
# Protected by state_lock.
WRITE_COOLDOWN_SECONDS: int = 10
tag_write_timestamps: dict[str, float] = {}

# Scan-time tracking for the spool active on each target (toolhead or lane).
# One record per target — written atomically by _record_spool_tracking() so
# the fields can never drift apart. Used by UPDATE_TAG deduction math and
# the lock auto-release UID comparison. Protected by state_lock.
@dataclass
class ActiveSpool:
    uid: str
    device_id: str = ""              # "" = mobile-scanned, no scanner device
    weight_g: float | None = None    # remaining at scan; re-baselined after each deduction
    diameter_mm: float = 1.75
    density: float = 1.24            # g/cm³
    tag_format: str = "unknown"      # "openprinttag", "opentag3d", "uid_only", ...


active_spool_tracking: dict[str, ActiveSpool] = {}

# Low-spool LED state — tracks whether the low-spool retained MQTT command
# has been sent to each scanner. Edge-triggered: we only publish on state
# transitions to avoid flooding the broker. Keyed by device_id (not lane)
# because the MQTT cmd topic is per-scanner.
low_spool_latched: dict[str, bool] = {}

# Per-service health for the MQTT status topic (#41). "unknown" until the
# first check; updated via health.set_health() which publishes retained
# snapshots on transitions. Protected by state_lock.
service_health: dict[str, str] = {
    "mqtt": "unknown",
    "moonraker": "unknown",
    "spoolman": "unknown",
    "klipper": "unknown",
}

# Pending deductions for mobile-scanned spools (no scanner to receive MQTT).
# Maps UID → grams pending. Persisted to ~/SpoolSense/deductions.json.
# Protected by state_lock.
pending_mobile_deductions: dict[str, float] = {}
DEDUCTIONS_FILE: str = os.path.expanduser("~/SpoolSense/deductions.json")

# active_spool_tracking persistence (#91) — deduction baselines survive
# middleware restarts. Spools can stay mounted for weeks (toolchangers,
# INDX); losing the baseline on every service restart silently broke
# deduction until the next rescan.
TRACKING_FILE: str = os.path.expanduser("~/SpoolSense/tracking.json")
