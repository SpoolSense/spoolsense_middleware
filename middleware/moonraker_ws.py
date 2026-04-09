"""
moonraker_ws.py — Moonraker websocket connection for real-time printer object updates.

Subscribes to AFC_stepper and gcode_macro ASSIGN_SPOOL objects.
Dispatches state deltas to registered callbacks. Auto-reconnects
with exponential backoff and full state re-sync.

Replaces HTTP polling in afc_status.py and toolchanger_status.py (#11).
"""
from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable

logger = logging.getLogger(__name__)

try:
    import websocket
    WEBSOCKET_AVAILABLE = True
except ImportError:
    WEBSOCKET_AVAILABLE = False
    logger.warning(
        "websocket-client not installed — Moonraker websocket disabled. "
        "Using HTTP polling fallback. Run: pip install -r requirements.txt"
    )

# Reconnect backoff
RETRY_BASE: float = 2.0
RETRY_MAX: float = 30.0


class MoonrakerWebsocket:
    """
    Single websocket connection to Moonraker for real-time printer object updates.

    Usage:
        ws = MoonrakerWebsocket("ws://localhost:7125/websocket")
        ws.set_lane_names(["lane1", "lane2", "lane3", "lane4"])
        ws.on_lane_update = my_lane_handler
        ws.on_assign_spool = my_assign_handler
        ws.start()
        ...
        ws.stop()
    """

    def __init__(self, url: str) -> None:
        self._url = url
        self._lane_names: list[str] = []
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._subscribe_id: int = 1
        self._ws = None

        # Callbacks — set by consumers
        self.on_lane_update: Callable[[str, dict], None] | None = None
        self.on_assign_spool: Callable[[str], None] | None = None
        self.on_update_tag: Callable[[int], None] | None = None

    def set_lane_names(self, names: list[str]) -> None:
        """Set AFC lane names to subscribe to (e.g. ['lane1', 'lane2'])."""
        self._lane_names = list(names)

    def start(self) -> None:
        """Start the websocket connection thread."""
        if not WEBSOCKET_AVAILABLE:
            logger.warning("MoonrakerWebsocket: websocket-client not available, skipping")
            return

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="moonraker-ws",
            daemon=True,
        )
        self._thread.start()
        logger.info(f"MoonrakerWebsocket: started ({self._url})")

    def stop(self) -> None:
        """Stop the websocket connection and thread."""
        self._stop_event.set()
        if self._ws:
            self._ws.close()
        if self._thread:
            self._thread.join(timeout=5)
            if self._thread.is_alive():
                logger.warning("MoonrakerWebsocket: thread did not stop cleanly")
            self._thread = None
        logger.info("MoonrakerWebsocket: stopped")

    def _run_loop(self) -> None:
        """Reconnect loop with exponential backoff."""
        self._consecutive_failures = 0

        while not self._stop_event.is_set():
            try:
                self._ws = websocket.WebSocketApp(
                    self._url,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_close=self._on_close,
                    on_error=self._on_error,
                )
                # run_forever blocks until disconnect
                self._ws.run_forever(ping_interval=10, ping_timeout=5)
            except Exception:
                logger.exception("MoonrakerWebsocket: unexpected error in run loop")

            if self._stop_event.is_set():
                break

            self._consecutive_failures += 1
            wait = min(RETRY_BASE * (2 ** (self._consecutive_failures - 1)), RETRY_MAX)
            logger.warning(
                f"MoonrakerWebsocket: disconnected, reconnecting in {wait:.0f}s "
                f"(attempt {self._consecutive_failures})"
            )
            self._stop_event.wait(timeout=wait)

    def _on_open(self, ws) -> None:
        """Connected — send object subscription."""
        logger.info("MoonrakerWebsocket: connected")
        self._consecutive_failures = 0
        objects = self._build_subscribe_objects()
        self._subscribe_id += 1
        subscribe_msg = {
            "jsonrpc": "2.0",
            "method": "printer.objects.subscribe",
            "params": {"objects": objects},
            "id": self._subscribe_id,
        }
        ws.send(json.dumps(subscribe_msg))
        logger.info(f"MoonrakerWebsocket: subscribed to {len(objects)} objects")

    def _on_message(self, ws, message: str) -> None:
        """Handle incoming websocket messages."""
        try:
            data = json.loads(message)
        except json.JSONDecodeError:
            logger.warning("MoonrakerWebsocket: invalid JSON received")
            return

        # Subscription response — contains full initial state
        if "id" in data and data.get("id") == self._subscribe_id:
            status = data.get("result", {}).get("status", {})
            self._dispatch_status(status)
            logger.info("MoonrakerWebsocket: initial state received")
            return

        # Real-time delta update
        method = data.get("method", "")
        if method == "notify_status_update":
            params = data.get("params", [])
            if params and isinstance(params[0], dict):
                self._dispatch_status(params[0])

        # Klipper restarted — re-subscribe to get fresh object state
        elif method == "notify_klippy_ready":
            logger.info("MoonrakerWebsocket: Klipper ready — re-subscribing to printer objects")
            self._on_open(ws)

        elif method == "notify_klippy_disconnected":
            logger.warning("MoonrakerWebsocket: Klipper disconnected — waiting for reconnect")

    def _on_close(self, ws, close_status_code, close_msg) -> None:
        logger.info(f"MoonrakerWebsocket: connection closed ({close_status_code})")

    def _on_error(self, ws, error) -> None:
        if not self._stop_event.is_set():
            logger.warning(f"MoonrakerWebsocket: error — {error}")

    def _build_subscribe_objects(self) -> dict:
        """Build the printer.objects.subscribe objects dict."""
        objects = {}
        for lane in self._lane_names:
            objects[f"AFC_stepper {lane}"] = None
        objects["gcode_macro ASSIGN_SPOOL"] = None
        objects["gcode_macro UPDATE_TAG"] = None
        return objects

    def _dispatch_status(self, status: dict) -> None:
        """Route status updates to registered callbacks."""
        for key, value in status.items():
            if value is None:
                continue
            if key.startswith("AFC_stepper ") and self.on_lane_update:
                lane_name = key[len("AFC_stepper "):]
                self.on_lane_update(lane_name, value)
            elif key == "gcode_macro ASSIGN_SPOOL" and self.on_assign_spool:
                pending_tool = value.get("pending_tool", "")
                self.on_assign_spool(pending_tool)
            elif key == "gcode_macro UPDATE_TAG" and self.on_update_tag:
                pending = value.get("pending", 0)
                self.on_update_tag(pending)
