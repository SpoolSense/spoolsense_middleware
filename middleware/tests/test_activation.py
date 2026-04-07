"""Tests for activation.py — spool activation, lock management, publisher routing."""
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
from activation import activate_spool  # noqa: E402
from publishers.klipper import _validate_color_hex, _validate_material  # noqa: E402


def _setup_app_state(moonraker_url="http://moonraker:7125"):
    app_state.cfg = {
        "moonraker_url": moonraker_url,
        "low_spool_threshold": 100,
    }
    app_state.lane_locks = {}
    app_state.active_spools = {}
    app_state.pending_spool = None
    app_state.state_lock = threading.Lock()


class TestActivateSpool(unittest.TestCase):

    def setUp(self):
        _setup_app_state()

    @patch("requests.post")
    def test_afc_stage_sends_correct_gcode(self, mock_post):
        mock_post.return_value = MagicMock(raise_for_status=lambda: None)
        result = activate_spool(42, "afc_stage")
        assert result is True
        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args
        assert "SET_NEXT_SPOOL_ID SPOOL_ID=42" in kwargs["json"]["script"]

    @patch("requests.post")
    def test_afc_lane_sends_correct_gcode(self, mock_post):
        mock_post.return_value = MagicMock(raise_for_status=lambda: None)
        result = activate_spool(7, "afc_lane", target="lane1")
        assert result is True
        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args
        assert "SET_SPOOL_ID LANE=lane1 SPOOL_ID=7" in kwargs["json"]["script"]

    @patch("requests.post")
    def test_toolhead_sends_correct_gcode(self, mock_post):
        mock_post.return_value = MagicMock(raise_for_status=lambda: None)
        result = activate_spool(99, "toolhead", target="T0")
        assert result is True
        assert mock_post.call_count == 2
        # First call sets active spool in Moonraker's spoolman endpoint
        first_call_url = mock_post.call_args_list[0][0][0]
        assert "/server/spoolman/spool_id" in first_call_url
        # Second call saves the variable
        second_script = mock_post.call_args_list[1][1]["json"]["script"]
        assert "SAVE_VARIABLE VARIABLE=t0_spool_id VALUE=99" in second_script

    @patch("requests.post")
    def test_toolhead_stage_logs_staging_returns_true(self, mock_post):
        result = activate_spool(55, "toolhead_stage")
        assert result is True
        # No HTTP calls for toolhead_stage
        mock_post.assert_not_called()

    def test_no_moonraker_url_returns_false(self):
        app_state.cfg["moonraker_url"] = ""
        result = activate_spool(1, "afc_lane", target="lane1")
        assert result is False

    def test_afc_lane_no_target_returns_false(self):
        result = activate_spool(1, "afc_lane", target=None)
        assert result is False

    def test_toolhead_no_target_returns_false(self):
        result = activate_spool(1, "toolhead", target=None)
        assert result is False

    @patch("requests.post")
    def test_moonraker_error_returns_false(self, mock_post):
        import requests as req
        mock_post.side_effect = req.ConnectionError("refused")
        result = activate_spool(1, "afc_lane", target="lane1")
        assert result is False

    @patch("requests.post")
    def test_moonraker_http_error_returns_false(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = Exception("500 Server Error")
        mock_post.return_value = mock_resp
        result = activate_spool(1, "afc_stage")
        assert result is False

    @patch("requests.post")
    def test_unknown_action_returns_false(self, mock_post):
        result = activate_spool(1, "not_an_action")
        assert result is False
        mock_post.assert_not_called()


class TestValidateColorHex(unittest.TestCase):

    def test_valid_lowercase_hex(self):
        assert _validate_color_hex("ff0000") == "FF0000"

    def test_valid_uppercase_hex(self):
        assert _validate_color_hex("ABCDEF") == "ABCDEF"

    def test_valid_with_hash_prefix(self):
        assert _validate_color_hex("#1a2b3c") == "1A2B3C"

    def test_valid_all_zeros(self):
        assert _validate_color_hex("000000") == "000000"

    def test_invalid_too_short(self):
        assert _validate_color_hex("ff00") is None

    def test_invalid_too_long(self):
        assert _validate_color_hex("ff0000ff") is None

    def test_invalid_non_hex_chars(self):
        assert _validate_color_hex("gggggg") is None

    def test_invalid_empty_string(self):
        assert _validate_color_hex("") is None

    def test_invalid_spaces(self):
        assert _validate_color_hex("ff 000") is None


class TestValidateMaterial(unittest.TestCase):

    def test_valid_simple_material(self):
        assert _validate_material("PLA") is True

    def test_valid_material_with_numbers(self):
        assert _validate_material("PLA95") is True

    def test_valid_material_with_space(self):
        assert _validate_material("PLA Pro") is True

    def test_valid_material_with_dash(self):
        assert _validate_material("PLA-Plus") is True

    def test_valid_material_with_underscore(self):
        assert _validate_material("PLA_HF") is True

    def test_valid_exactly_50_chars(self):
        assert _validate_material("A" * 50) is True

    def test_too_long_returns_false(self):
        assert _validate_material("A" * 51) is False

    def test_empty_string_returns_false(self):
        assert _validate_material("") is False

    def test_special_chars_returns_false(self):
        assert _validate_material("PLA!") is False

    def test_sql_injection_returns_false(self):
        assert _validate_material("PLA'; DROP TABLE") is False

    def test_newline_returns_false(self):
        assert _validate_material("PLA\n") is False


if __name__ == "__main__":
    unittest.main()
