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
from toolchanger_status import _fetch_tool_number, _assign_spool_to_tool  # noqa: E402


def _reset_app_state(moonraker_url="http://moonraker:7125"):
    app_state.cfg = {"moonraker_url": moonraker_url, "low_spool_threshold": 100}
    app_state.lane_locks = {}
    app_state.active_spools = {}
    app_state.pending_spool = None
    app_state.state_lock = threading.Lock()


class TestFetchToolNumber(unittest.TestCase):

    def setUp(self):
        _reset_app_state()

    @patch("requests.get")
    def test_success_returns_tool_number(self, mock_get):
        payload = {
            "result": {
                "status": {
                    "toolchanger": {"tool_number": 2}
                }
            }
        }
        mock_resp = MagicMock()
        mock_resp.json.return_value = payload
        mock_resp.raise_for_status = lambda: None
        mock_get.return_value = mock_resp

        result = _fetch_tool_number()
        assert result == 2

    @patch("requests.get")
    def test_returns_minus_one_for_no_tool(self, mock_get):
        payload = {
            "result": {
                "status": {
                    "toolchanger": {"tool_number": -1}
                }
            }
        }
        mock_resp = MagicMock()
        mock_resp.json.return_value = payload
        mock_resp.raise_for_status = lambda: None
        mock_get.return_value = mock_resp

        result = _fetch_tool_number()
        assert result == -1

    @patch("requests.get")
    def test_connection_error_returns_none(self, mock_get):
        import requests as req
        mock_get.side_effect = req.ConnectionError("refused")
        result = _fetch_tool_number()
        assert result is None

    @patch("requests.get")
    def test_timeout_returns_none(self, mock_get):
        import requests as req
        mock_get.side_effect = req.Timeout()
        result = _fetch_tool_number()
        assert result is None

    @patch("requests.get")
    def test_generic_exception_returns_none(self, mock_get):
        mock_get.side_effect = RuntimeError("unexpected")
        result = _fetch_tool_number()
        assert result is None

    def test_no_moonraker_url_returns_none(self):
        app_state.cfg["moonraker_url"] = ""
        result = _fetch_tool_number()
        assert result is None

    @patch("requests.get")
    def test_missing_toolchanger_key_returns_none(self, mock_get):
        # toolchanger key missing from response
        payload = {"result": {"status": {}}}
        mock_resp = MagicMock()
        mock_resp.json.return_value = payload
        mock_resp.raise_for_status = lambda: None
        mock_get.return_value = mock_resp

        result = _fetch_tool_number()
        assert result is None


class TestAssignSpoolToTool(unittest.TestCase):

    def setUp(self):
        _reset_app_state()

    @patch("requests.post")
    def test_with_spoolman_id_sets_spool_id_and_save_variable(self, mock_post):
        mock_post.return_value = MagicMock(raise_for_status=lambda: None)
        pending = {"spoolman_id": 10, "color_hex": "FF0000", "material": "PLA", "remaining_g": 300.0}
        _assign_spool_to_tool(0, pending)

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
        _assign_spool_to_tool(1, pending)

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
        _assign_spool_to_tool(2, pending)

        scripts = [
            c[1]["json"].get("script", "")
            for c in mock_post.call_args_list
            if "json" in c[1]
        ]
        assert not any("VARIABLE=color" in s for s in scripts)

    @patch("requests.post")
    def test_white_color_skips_color_command(self, mock_post):
        mock_post.return_value = MagicMock(raise_for_status=lambda: None)
        pending = {"spoolman_id": None, "color_hex": "FFFFFF", "material": "PLA", "remaining_g": None}
        _assign_spool_to_tool(0, pending)

        scripts = [
            c[1]["json"].get("script", "")
            for c in mock_post.call_args_list
            if "json" in c[1]
        ]
        assert not any("VARIABLE=color" in s for s in scripts)

    @patch("requests.post")
    def test_no_moonraker_url_does_nothing(self, mock_post):
        app_state.cfg["moonraker_url"] = ""
        pending = {"spoolman_id": 5, "color_hex": "FF0000", "material": "PLA", "remaining_g": 100.0}
        _assign_spool_to_tool(0, pending)
        mock_post.assert_not_called()

    @patch("requests.post")
    def test_correct_macro_name_for_tool_number(self, mock_post):
        mock_post.return_value = MagicMock(raise_for_status=lambda: None)
        pending = {"spoolman_id": 3, "color_hex": "0000FF", "material": "TPU", "remaining_g": None}
        _assign_spool_to_tool(3, pending)

        scripts = [
            c[1]["json"].get("script", "")
            for c in mock_post.call_args_list
            if "json" in c[1]
        ]
        # Macro should be T3
        assert any("MACRO=T3" in s for s in scripts)
        assert any("t3_spool_id" in s for s in scripts)


if __name__ == "__main__":
    unittest.main()
