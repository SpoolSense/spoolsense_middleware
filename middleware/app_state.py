from __future__ import annotations

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

# Lane state
lane_locks: dict = {}
active_spools: dict = {}
lane_statuses: dict = {}
