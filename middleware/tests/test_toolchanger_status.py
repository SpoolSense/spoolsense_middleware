from __future__ import annotations

import os
import sys
import threading
import unittest
from unittest.mock import patch, MagicMock, call

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

sys.modules.setdefault("paho", MagicMock())
sys.modules.setdefault("paho.mqtt", MagicMock())
sys.modules.setdefault("paho.mqtt.client", MagicMock())
sys.modules.setdefault("watchdog", MagicMock())
sys.modules.setdefault("watchdog.observers", MagicMock())
sys.modules.setdefault("watchdog.events", MagicMock())

import app_state  # noqa: E402
from toolchanger_status import _fetch_pending_tool, _assign_spool_to_tool  # noqa: E402


def _reset_app_state(moonraker_url="http://moonraker:7125"):
    app_state.cfg = {
        "moonraker_url": moonraker_url,
        "low_spool_threshold": 100,
        "publish_lane_data": False,
    }
    app_state.lane_locks = {}
    app_state.active_spools = {}
    app_state.pending_spool = None
    app_state.state_lock = threading.Lock()


class TestFetchPendingTool(unittest.TestCase):
    """Tests for _fetch_pending_tool which polls the ASSIGN_SPOOL macro."""

    def setUp(self):
        _reset_app_state()

    @patch("requests.get")
    def test_success_returns_tool_name(self, mock_get):
        payload = {
            "result": {
                "status": {
                    "gcode_macro ASSIGN_SPOOL": {"pending_tool": "T2"}
                }
            }
        }
        mock_resp = MagicMock()
        mock_resp.json.return_value = payload
        mock_resp.raise_for_status = lambda: None
        mock_get.return_value = mock_resp

        result = _fetch_pending_tool()
        assert result == "T2"

    @patch("requests.get")
    def test_returns_empty_for_no_pending(self, mock_get):
        payload = {
            "result": {
                "status": {
                    "gcode_macro ASSIGN_SPOOL": {"pending_tool": ""}
                }
            }
        }
        mock_resp = MagicMock()
        mock_resp.json.return_value = payload
        mock_resp.raise_for_status = lambda: None
        mock_get.return_value = mock_resp

        result = _fetch_pending_tool()
        assert result == ""

    @patch("requests.get")
    def test_connection_error_returns_none(self, mock_get):
        import requests as req
        mock_get.side_effect = req.ConnectionError("refused")
        result = _fetch_pending_tool()
        assert result is None

    @patch("requests.get")
    def test_timeout_returns_none(self, mock_get):
        import requests as req
        mock_get.side_effect = req.Timeout()
        result = _fetch_pending_tool()
        assert result is None

    @patch("requests.get")
    def test_generic_exception_returns_none(self, mock_get):
        mock_get.side_effect = RuntimeError("unexpected")
        result = _fetch_pending_tool()
        assert result is None

    def test_no_moonraker_url_returns_none(self):
        app_state.cfg["moonraker_url"] = ""
        result = _fetch_pending_tool()
        assert result is None

    @patch("requests.get")
    def test_missing_macro_key_returns_empty(self, mock_get):
        payload = {"result": {"status": {}}}
        mock_resp = MagicMock()
        mock_resp.json.return_value = payload
        mock_resp.raise_for_status = lambda: None
        mock_get.return_value = mock_resp

        result = _fetch_pending_tool()
        assert result == ""


class TestAssignSpoolToTool(unittest.TestCase):

    def setUp(self):
        _reset_app_state()

    @patch("requests.post")
    def test_with_spoolman_id_sets_spool_id_and_save_variable(self, mock_post):
        mock_post.return_value = MagicMock(raise_for_status=lambda: None)
        pending = {"spoolman_id": 10, "color_hex": "FF0000", "material": "PLA", "remaining_g": 300.0}
        _assign_spool_to_tool("T0", pending)

        urls = [c[0][0] for c in mock_post.call_args_list]
        scripts = [
            c[1]["json"].get("script", "")
            for c in mock_post.call_args_list
            if "json" in c[1]
        ]

        # Active spool set via spoolman endpoint
        assert any("/server/spoolman/spool_id" in u for u in urls)
        # SET_GCODE_VARIABLE spool_id
        assert any("SET_GCODE_VARIABLE MACRO=T0 VARIABLE=spool_id VALUE=10" in s for s in scripts)
        # SAVE_VARIABLE persists it
        assert any("SAVE_VARIABLE VARIABLE=t0_spool_id VALUE=10" in s for s in scripts)
        # Color set
        assert any("SET_GCODE_VARIABLE MACRO=T0 VARIABLE=color" in s for s in scripts)

    @patch("requests.post")
    def test_without_spoolman_id_sets_color_only(self, mock_post):
        mock_post.return_value = MagicMock(raise_for_status=lambda: None)
        pending = {"spoolman_id": None, "color_hex": "00FF00", "material": "PETG", "remaining_g": None}
        _assign_spool_to_tool("T1", pending)

        scripts = [
            c[1]["json"].get("script", "")
            for c in mock_post.call_args_list
            if "json" in c[1]
        ]

        # No spool_id or SAVE_VARIABLE calls
        assert not any("spool_id" in s.lower() for s in scripts)
        assert not any("SAVE_VARIABLE" in s for s in scripts)
        # Color still set
        assert any("SET_GCODE_VARIABLE MACRO=T1 VARIABLE=color" in s for s in scripts)

    @patch("requests.post")
    def test_empty_color_skips_color_command(self, mock_post):
        mock_post.return_value = MagicMock(raise_for_status=lambda: None)
        pending = {"spoolman_id": None, "color_hex": "", "material": "ABS", "remaining_g": None}
        _assign_spool_to_tool("T2", pending)

        scripts = [
            c[1]["json"].get("script", "")
            for c in mock_post.call_args_list
            if "json" in c[1]
        ]
        assert not any("VARIABLE=color" in s for s in scripts)

    @patch("requests.post")
    def test_white_color_emits_color_command(self, mock_post):
        """White is a valid filament color — should be sent."""
        mock_post.return_value = MagicMock(raise_for_status=lambda: None)
        pending = {"spoolman_id": None, "color_hex": "FFFFFF", "material": "PLA", "remaining_g": None}
        _assign_spool_to_tool("T0", pending)

        scripts = [
            c[1]["json"].get("script", "")
            for c in mock_post.call_args_list
            if "json" in c[1]
        ]
        assert any("VARIABLE=color" in s and "FFFFFF" in s for s in scripts)

    @patch("requests.post")
    def test_black_color_substituted_with_dim_white(self, mock_post):
        """Black can't display on LED — substituted with dim white (#333333)."""
        mock_post.return_value = MagicMock(raise_for_status=lambda: None)
        pending = {"spoolman_id": None, "color_hex": "000000", "material": "PLA", "remaining_g": None}
        _assign_spool_to_tool("T0", pending)

        scripts = [
            c[1]["json"].get("script", "")
            for c in mock_post.call_args_list
            if "json" in c[1]
        ]
        assert any("VARIABLE=color" in s and "333333" in s for s in scripts)

    @patch("requests.post")
    def test_no_moonraker_url_does_nothing(self, mock_post):
        app_state.cfg["moonraker_url"] = ""
        pending = {"spoolman_id": 5, "color_hex": "FF0000", "material": "PLA", "remaining_g": 100.0}
        _assign_spool_to_tool("T0", pending)
        mock_post.assert_not_called()

    @patch("requests.post")
    def test_correct_macro_name_for_tool(self, mock_post):
        mock_post.return_value = MagicMock(raise_for_status=lambda: None)
        pending = {"spoolman_id": 3, "color_hex": "0000FF", "material": "TPU", "remaining_g": None}
        _assign_spool_to_tool("T3", pending)

        scripts = [
            c[1]["json"].get("script", "")
            for c in mock_post.call_args_list
            if "json" in c[1]
        ]
        assert any("MACRO=T3" in s for s in scripts)
        assert any("t3_spool_id" in s for s in scripts)

    @patch("requests.post")
    def test_lane_data_written_when_enabled(self, mock_post):
        """When publish_lane_data is true, spool data is written to Moonraker lane_data."""
        app_state.cfg["publish_lane_data"] = True
        mock_post.return_value = MagicMock(raise_for_status=lambda: None)
        pending = {"spoolman_id": 42, "color_hex": "FF0000", "material": "PETG", "remaining_g": 850.0}
        _assign_spool_to_tool("T2", pending)

        # Check that a POST to /server/database/item was made with lane_data namespace
        lane_data_calls = [
            c for c in mock_post.call_args_list
            if "json" in c[1] and c[1]["json"].get("namespace") == "lane_data"
        ]
        assert len(lane_data_calls) == 1
        value = lane_data_calls[0][1]["json"]["value"]
        assert value["material"] == "PETG"
        assert value["spool_id"] == 42

    @patch("requests.post")
    def test_lane_data_not_written_when_disabled(self, mock_post):
        """When publish_lane_data is false, no lane_data write."""
        app_state.cfg["publish_lane_data"] = False
        mock_post.return_value = MagicMock(raise_for_status=lambda: None)
        pending = {"spoolman_id": 42, "color_hex": "FF0000", "material": "PETG", "remaining_g": 850.0}
        _assign_spool_to_tool("T2", pending)

        lane_data_calls = [
            c for c in mock_post.call_args_list
            if "json" in c[1] and c[1]["json"].get("namespace") == "lane_data"
        ]
        assert len(lane_data_calls) == 0


if __name__ == "__main__":
    unittest.main()
