"""
app_state.py — Shared mutable process state.

All runtime state lives here: config, caches, locks, sync service references.
Every other module reads/writes state via `import app_state`. Multi-thread
access to shared fields is protected by state_lock.
"""
from __future__ import annotations

import threading

import paho.mqtt.client as mqtt
from watchdog.observers import Observer

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
watcher: Observer | None = None
afc_status_sync: AfcStatusSync | None = None
toolchanger_status_sync: ToolchangerStatusSync | None = None
toolhead_status_sync: ToolheadStatusSync | None = None
filament_usage_sync: FilamentUsageSync | None = None
publisher_manager: PublisherManager | None = None
moonraker_ws: MoonrakerWebsocket | None = None

# Spoolman cache
spool_cache: dict = {}
last_cache_refresh: float = 0.0
CACHE_TTL: int = 3600

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

# Pending spool data from afc_stage or toolhead_stage scans.
# Set by _activate_from_scan(), consumed by afc_status (on lane load)
# or toolchanger_status (on tool pickup). Protected by state_lock.
#
# NOTE: This is a single shared slot. If a user has both afc_stage and
# toolhead_stage scanners in the same config, a scan on one could be
# consumed by the other's poller. In practice this is unlikely — the user
# would need to scan on an AFC scanner then pick up a toolhead (or vice
# versa) before the first action completes. If this becomes a reported
# issue, split into pending_spool_afc and pending_spool_toolchanger.
pending_spool: dict | None = None

# Tag writeback cooldown — tracks recent writes to prevent loops.
# Maps uid → timestamp of the last write command sent.
# Protected by state_lock.
WRITE_COOLDOWN_SECONDS: int = 10
tag_write_timestamps: dict[str, float] = {}

# Filament usage tracking — used by UPDATE_TAG to calculate deductions.
# Records the initial tag weight, UID, scanner device_id, and filament
# properties per target at scan time. Protected by state_lock.
active_spool_weights: dict[str, float] = {}
active_spool_uids: dict[str, str] = {}
active_spool_devices: dict[str, str] = {}
active_spool_diameters: dict[str, float] = {}   # mm, default 1.75
active_spool_densities: dict[str, float] = {}   # g/cm³, default 1.24
