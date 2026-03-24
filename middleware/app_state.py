from __future__ import annotations

import threading

import paho.mqtt.client as mqtt
from watchdog.observers import Observer

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from afc_status import AfcStatusSync

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

# Pending spool data from afc_stage scans — held until a lane loads.
# Set by _activate_from_scan() on afc_stage, consumed by afc_status._sync_lane_state()
# when it detects a newly loaded lane. Protected by state_lock.
pending_spool: dict | None = None
