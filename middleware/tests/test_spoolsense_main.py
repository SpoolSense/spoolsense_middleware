"""
Tests for spoolsense.py — entry-point startup helpers.

Covers the extracted helper functions that wire up services at startup:
_setup_spoolman(), _discover_afc_lanes(), _setup_websocket(), and
_print_config_summary(). The main() loop itself is not tested here —
that requires integration infrastructure. These helpers are the seams
where config drives service creation and are easy to unit-test in isolation.
"""
from __future__ import annotations

import os
import sys
import threading
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

# Block all heavy third-party imports before any middleware module is touched
sys.modules.setdefault("paho", MagicMock())
sys.modules.setdefault("paho.mqtt", MagicMock())
sys.modules.setdefault("paho.mqtt.client", MagicMock())
sys.modules.setdefault("watchdog", MagicMock())
sys.modules.setdefault("watchdog.observers", MagicMock())

# FileSystemEventHandler needs a real base so var_watcher.KlipperVarHandler
# is importable as a real class (same trick as test_var_watcher.py)
_watchdog_events_mock = MagicMock()
_watchdog_events_mock.FileSystemEventHandler = object
sys.modules["watchdog.events"] = _watchdog_events_mock

# spoolsense.py configures file logging at import time — redirect to avoid
# creating ~/SpoolSense/middleware/spoolsense.log during tests
sys.modules.setdefault("uvicorn", MagicMock())

import app_state  # noqa: E402

# Import the helpers we want to test after all stubs are in place
from spoolsense import (  # noqa: E402
    _setup_spoolman,
    _discover_afc_lanes,
    _setup_websocket,
    _print_config_summary,
)


def _reset_app_state(
    spoolman_url: str = "",
    moonraker_url: str = "http://moonraker:7125",
    scanners: dict | None = None,
) -> None:
    app_state.cfg = {
        "spoolman_url": spoolman_url,
        "moonraker_url": moonraker_url,
        "scanners": scanners or {},
        "toolheads": [],
        "mqtt": {"broker": "localhost", "port": 1883},
        "tag_writeback_enabled": False,
        "publish_lane_data": False,
    }
    app_state.spoolman_client = None
    app_state.moonraker_ws = None
    app_state.state_lock = threading.Lock()
    app_state.DISPATCHER_AVAILABLE = False


# ── _setup_spoolman ───────────────────────────────────────────────────────────

class TestSetupSpoolman(unittest.TestCase):

    def setUp(self) -> None:
        _reset_app_state()

    def test_creates_client_when_url_configured(self) -> None:
        # Spoolman is optional — only instantiate the client when a URL is given
        _reset_app_state(spoolman_url="http://spoolman:7912")

        mock_client_cls = MagicMock()
        mock_client_cls.return_value = MagicMock()

        with patch.dict(sys.modules, {"spoolman": MagicMock(), "spoolman.client": MagicMock()}):
            sys.modules["spoolman.client"].SpoolmanClient = mock_client_cls
            _setup_spoolman()

        mock_client_cls.assert_called_once_with("http://spoolman:7912")
        self.assertIsNotNone(app_state.spoolman_client)

    def test_skips_when_no_url_configured(self) -> None:
        # Tag-only mode: no Spoolman URL means we never try to connect
        _reset_app_state(spoolman_url="")

        _setup_spoolman()

        # spoolman_client stays None — no import attempted
        self.assertIsNone(app_state.spoolman_client)


# ── _discover_afc_lanes ───────────────────────────────────────────────────────

def _make_afc_scanner_cfg() -> dict:
    """Minimal scanner config that satisfies has_afc_scanners()."""
    return {"scanner1": {"action": "afc_lane", "lane": "lane1"}}


class TestDiscoverAfcLanes(unittest.TestCase):

    def setUp(self) -> None:
        _reset_app_state()

    def test_returns_lane_names_from_moonraker(self) -> None:
        # Happy path: Moonraker responds with AFC_stepper objects
        _reset_app_state(
            moonraker_url="http://moonraker:7125",
            scanners=_make_afc_scanner_cfg(),
        )

        payload = {
            "result": {
                "objects": [
                    "AFC_stepper lane1",
                    "AFC_stepper lane2",
                    "toolhead",           # non-AFC objects must be filtered out
                    "extruder",
                ]
            }
        }
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = payload

        with patch("requests.get", return_value=mock_resp):
            lanes = _discover_afc_lanes()

        self.assertEqual(sorted(lanes), ["lane1", "lane2"])

    def test_returns_empty_list_when_no_afc_scanners(self) -> None:
        # Non-AFC configs (toolhead mode) should not query Moonraker at all
        _reset_app_state(scanners={"s1": {"action": "toolhead", "toolhead": "T0"}})

        lanes = _discover_afc_lanes()

        self.assertEqual(lanes, [])

    def test_returns_empty_list_when_no_moonraker_url(self) -> None:
        # Edge case: AFC scanners configured but no Moonraker URL (misconfigured)
        _reset_app_state(moonraker_url="", scanners=_make_afc_scanner_cfg())

        lanes = _discover_afc_lanes()

        self.assertEqual(lanes, [])

    def test_returns_empty_list_on_http_error(self) -> None:
        # Moonraker not yet up — should degrade gracefully, not crash startup
        _reset_app_state(moonraker_url="http://moonraker:7125", scanners=_make_afc_scanner_cfg())

        mock_resp = MagicMock()
        mock_resp.ok = False

        with patch("requests.get", return_value=mock_resp):
            lanes = _discover_afc_lanes()

        self.assertEqual(lanes, [])

    def test_returns_empty_list_on_connection_error(self) -> None:
        # Network failure at startup must not prevent the rest of the boot sequence
        _reset_app_state(moonraker_url="http://moonraker:7125", scanners=_make_afc_scanner_cfg())

        import requests as req
        with patch("requests.get", side_effect=req.ConnectionError("refused")):
            lanes = _discover_afc_lanes()

        self.assertEqual(lanes, [])

    def test_returns_empty_list_on_timeout(self) -> None:
        # Slow Moonraker response should not block startup indefinitely
        _reset_app_state(moonraker_url="http://moonraker:7125", scanners=_make_afc_scanner_cfg())

        import requests as req
        with patch("requests.get", side_effect=req.Timeout()):
            lanes = _discover_afc_lanes()

        self.assertEqual(lanes, [])

    def test_strips_afc_stepper_prefix_from_names(self) -> None:
        # Moonraker returns "AFC_stepper lane1" — we only want "lane1" for subscriptions
        _reset_app_state(moonraker_url="http://moonraker:7125", scanners=_make_afc_scanner_cfg())

        payload = {"result": {"objects": ["AFC_stepper T0", "AFC_stepper T1"]}}
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = payload

        with patch("requests.get", return_value=mock_resp):
            lanes = _discover_afc_lanes()

        self.assertNotIn("AFC_stepper T0", lanes)
        self.assertIn("T0", lanes)
        self.assertIn("T1", lanes)


# ── _setup_websocket ──────────────────────────────────────────────────────────

class TestSetupWebsocket(unittest.TestCase):

    def setUp(self) -> None:
        _reset_app_state(moonraker_url="http://moonraker:7125")

    def test_returns_false_when_websocket_unavailable(self) -> None:
        # websocket-client not installed → fall back to HTTP polling silently
        with patch("spoolsense.WEBSOCKET_AVAILABLE", False):
            result = _setup_websocket([])

        self.assertFalse(result)
        self.assertIsNone(app_state.moonraker_ws)

    def test_returns_false_when_no_moonraker_url(self) -> None:
        # Even with websocket-client installed, no URL means we can't connect
        _reset_app_state(moonraker_url="")

        with patch("spoolsense.WEBSOCKET_AVAILABLE", True):
            result = _setup_websocket([])

        self.assertFalse(result)

    def test_returns_true_and_creates_websocket_when_available(self) -> None:
        # Happy path: websocket-client installed and Moonraker URL configured
        _reset_app_state(moonraker_url="http://moonraker:7125")

        mock_ws_instance = MagicMock()
        mock_ws_cls = MagicMock(return_value=mock_ws_instance)

        with patch("spoolsense.WEBSOCKET_AVAILABLE", True), \
             patch("spoolsense.MoonrakerWebsocket", mock_ws_cls):
            result = _setup_websocket(["lane1", "lane2"])

        self.assertTrue(result)
        self.assertIs(app_state.moonraker_ws, mock_ws_instance)

    def test_converts_http_url_to_ws_url(self) -> None:
        # http:// must become ws:// — Moonraker websocket endpoint is /websocket
        _reset_app_state(moonraker_url="http://moonraker:7125")

        captured_urls: list[str] = []

        def capture_ws(url: str) -> MagicMock:
            captured_urls.append(url)
            return MagicMock()

        with patch("spoolsense.WEBSOCKET_AVAILABLE", True), \
             patch("spoolsense.MoonrakerWebsocket", side_effect=capture_ws):
            _setup_websocket([])

        self.assertEqual(len(captured_urls), 1)
        self.assertTrue(captured_urls[0].startswith("ws://"))
        self.assertIn("/websocket", captured_urls[0])

    def test_converts_https_url_to_wss_url(self) -> None:
        # https:// must become wss:// for TLS websocket connections
        _reset_app_state(moonraker_url="https://moonraker.local")

        captured_urls: list[str] = []

        def capture_ws(url: str) -> MagicMock:
            captured_urls.append(url)
            return MagicMock()

        with patch("spoolsense.WEBSOCKET_AVAILABLE", True), \
             patch("spoolsense.MoonrakerWebsocket", side_effect=capture_ws):
            _setup_websocket([])

        self.assertTrue(captured_urls[0].startswith("wss://"))

    def test_sets_lane_names_on_websocket(self) -> None:
        # Lane names must be registered on the websocket before it starts
        # so it knows which AFC_stepper objects to subscribe to
        _reset_app_state(moonraker_url="http://moonraker:7125")

        mock_ws = MagicMock()

        with patch("spoolsense.WEBSOCKET_AVAILABLE", True), \
             patch("spoolsense.MoonrakerWebsocket", return_value=mock_ws):
            _setup_websocket(["lane1", "lane2"])

        mock_ws.set_lane_names.assert_called_once_with(["lane1", "lane2"])


# ── _print_config_summary ─────────────────────────────────────────────────────

class TestPrintConfigSummary(unittest.TestCase):

    def setUp(self) -> None:
        _reset_app_state()

    def test_does_not_crash_with_minimal_config(self) -> None:
        # Smoke test: --check-config should never raise, regardless of config shape
        _reset_app_state(spoolman_url="", moonraker_url="http://moonraker:7125")
        app_state.DISPATCHER_AVAILABLE = False

        try:
            _print_config_summary()
        except Exception as exc:
            self.fail(f"_print_config_summary() raised unexpectedly: {exc}")

    def test_does_not_crash_with_scanners_configured(self) -> None:
        # Verify scanner rows render without KeyError when action/lane/toolhead present
        _reset_app_state(
            spoolman_url="http://spoolman:7912",
            moonraker_url="http://moonraker:7125",
            scanners={
                "device1": {"action": "afc_lane", "lane": "lane1"},
                "device2": {"action": "toolhead", "toolhead": "T0"},
                "device3": {"action": "afc_stage"},
            },
        )
        app_state.cfg["toolheads"] = ["T0", "T1"]
        app_state.cfg["tag_writeback_enabled"] = True
        app_state.cfg["mobile"] = {"enabled": True, "port": 5001, "action": "afc_stage"}
        app_state.DISPATCHER_AVAILABLE = True

        try:
            _print_config_summary()
        except Exception as exc:
            self.fail(f"_print_config_summary() raised unexpectedly with scanners: {exc}")


if __name__ == "__main__":
    unittest.main()
