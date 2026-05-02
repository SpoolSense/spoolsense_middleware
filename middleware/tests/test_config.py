"""Tests for config.py — scanner validation, legacy migration, toolhead derivation."""
from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

# Mock paho.mqtt and watchdog before importing anything from middleware
sys.modules.setdefault("paho", MagicMock())
sys.modules.setdefault("paho.mqtt", MagicMock())
sys.modules.setdefault("paho.mqtt.client", MagicMock())
sys.modules.setdefault("watchdog", MagicMock())
sys.modules.setdefault("watchdog.observers", MagicMock())
sys.modules.setdefault("watchdog.events", MagicMock())

from unittest.mock import patch  # noqa: E402

import requests  # noqa: E402

import app_state  # noqa: E402

from config import (  # noqa: E402
    _validate_scanners,
    _derive_toolheads,
    _migrate_legacy_config,
    discover_klipper_var_path,
    has_afc_scanners,
    has_toolhead_scanners,
    has_toolhead_stage_scanners,
)


class TestValidateScanners(unittest.TestCase):

    def _call(self, config):
        with self.assertRaises(SystemExit):
            _validate_scanners(config)

    def _ok(self, config):
        _validate_scanners(config)  # should not raise

    def test_valid_afc_stage(self):
        self._ok({"scanners": {"abc123": {"action": "afc_stage"}}})

    def test_valid_afc_lane(self):
        self._ok({"scanners": {"abc123": {"action": "afc_lane", "lane": "lane1"}}})

    def test_valid_toolhead(self):
        self._ok({"scanners": {"abc123": {"action": "toolhead", "toolhead": "T0"}}})

    def test_toolhead_defaults_to_t0(self):
        # Single-toolhead users shouldn't need to specify toolhead: "T0" (#44)
        config = {"scanners": {"abc123": {"action": "toolhead"}}}
        self._ok(config)
        self.assertEqual(config["scanners"]["abc123"]["toolhead"], "T0")

    def test_valid_toolhead_stage(self):
        self._ok({"scanners": {"abc123": {"action": "toolhead_stage"}}})

    def test_multiple_valid_scanners(self):
        config = {
            "scanners": {
                "aaa": {"action": "afc_lane", "lane": "lane1"},
                "bbb": {"action": "afc_lane", "lane": "lane2"},
                "ccc": {"action": "afc_stage"},
            }
        }
        self._ok(config)

    def test_invalid_action_rejected(self):
        self._call({"scanners": {"abc123": {"action": "badaction"}}})

    def test_none_action_rejected(self):
        self._call({"scanners": {"abc123": {"action": None}}})

    def test_missing_action_rejected(self):
        self._call({"scanners": {"abc123": {}}})

    def test_afc_lane_missing_lane(self):
        self._call({"scanners": {"abc123": {"action": "afc_lane"}}})

    def test_afc_lane_empty_lane(self):
        self._call({"scanners": {"abc123": {"action": "afc_lane", "lane": ""}}})

    def test_toolhead_missing_toolhead(self):
        self._call({"scanners": {"abc123": {"action": "toolhead"}}})

    def test_toolhead_empty_toolhead(self):
        self._call({"scanners": {"abc123": {"action": "toolhead", "toolhead": ""}}})

    def test_afc_lane_with_toolhead_field_rejected(self):
        self._call({"scanners": {"abc123": {"action": "afc_lane", "lane": "lane1", "toolhead": "T0"}}})

    def test_toolhead_with_lane_field_rejected(self):
        self._call({"scanners": {"abc123": {"action": "toolhead", "toolhead": "T0", "lane": "lane1"}}})

    def test_afc_stage_with_lane_rejected(self):
        self._call({"scanners": {"abc123": {"action": "afc_stage", "lane": "lane1"}}})

    def test_afc_stage_with_toolhead_rejected(self):
        self._call({"scanners": {"abc123": {"action": "afc_stage", "toolhead": "T0"}}})

    def test_toolhead_stage_with_lane_rejected(self):
        self._call({"scanners": {"abc123": {"action": "toolhead_stage", "lane": "lane1"}}})

    def test_empty_scanners_dict_rejected(self):
        self._call({"scanners": {}})

    def test_missing_scanners_key_rejected(self):
        self._call({})


class TestDeriveToolheads(unittest.TestCase):

    def test_derives_lanes_from_afc_lane_scanners(self):
        config = {
            "scanners": {
                "aaa": {"action": "afc_lane", "lane": "lane1"},
                "bbb": {"action": "afc_lane", "lane": "lane2"},
            }
        }
        result = _derive_toolheads(config)
        assert result == ["lane1", "lane2"]

    def test_derives_toolheads_from_toolhead_scanners(self):
        config = {
            "scanners": {
                "aaa": {"action": "toolhead", "toolhead": "T0"},
                "bbb": {"action": "toolhead", "toolhead": "T1"},
            }
        }
        result = _derive_toolheads(config)
        assert result == ["T0", "T1"]

    def test_afc_stage_does_not_contribute(self):
        config = {
            "scanners": {
                "stage": {"action": "afc_stage"},
                "lane": {"action": "afc_lane", "lane": "lane1"},
            }
        }
        result = _derive_toolheads(config)
        assert result == ["lane1"]

    def test_toolhead_stage_does_not_contribute(self):
        config = {
            "scanners": {
                "stage": {"action": "toolhead_stage"},
                "th": {"action": "toolhead", "toolhead": "T0"},
            }
        }
        result = _derive_toolheads(config)
        assert result == ["T0"]

    def test_deduplicates_targets(self):
        config = {
            "scanners": {
                "aaa": {"action": "afc_lane", "lane": "lane1"},
                "bbb": {"action": "afc_lane", "lane": "lane1"},
            }
        }
        result = _derive_toolheads(config)
        assert result == ["lane1"]

    def test_empty_scanners_returns_empty(self):
        assert _derive_toolheads({"scanners": {}}) == []

    def test_no_scanners_key_returns_empty(self):
        assert _derive_toolheads({}) == []


class TestMigrateLegacyConfig(unittest.TestCase):

    def test_afc_mode_converts_to_afc_lane(self):
        config = {
            "toolhead_mode": "afc",
            "scanner_lane_map": {"ecb338": "lane1", "abcd12": "lane2"},
        }
        result = _migrate_legacy_config(config)
        assert result["scanners"] == {
            "ecb338": {"action": "afc_lane", "lane": "lane1"},
            "abcd12": {"action": "afc_lane", "lane": "lane2"},
        }

    def test_toolchanger_mode_converts_to_toolhead(self):
        config = {
            "toolhead_mode": "toolchanger",
            "scanner_lane_map": {"ecb338": "T0"},
        }
        result = _migrate_legacy_config(config)
        assert result["scanners"] == {
            "ecb338": {"action": "toolhead", "toolhead": "T0"},
        }

    def test_single_mode_converts_to_toolhead(self):
        config = {
            "toolhead_mode": "single",
            "scanner_lane_map": {"aaa": "T0"},
        }
        result = _migrate_legacy_config(config)
        assert result["scanners"] == {
            "aaa": {"action": "toolhead", "toolhead": "T0"},
        }

    def test_legacy_and_scanners_present_scanners_wins(self):
        existing_scanners = {"xyz": {"action": "afc_lane", "lane": "lane3"}}
        config = {
            "scanners": existing_scanners,
            "toolhead_mode": "afc",
            "scanner_lane_map": {"ecb338": "lane1"},
        }
        result = _migrate_legacy_config(config)
        assert result["scanners"] is existing_scanners

    def test_no_legacy_keys_returns_unchanged(self):
        config = {"scanners": {"abc": {"action": "afc_stage"}}}
        result = _migrate_legacy_config(config)
        assert result is config

    def test_legacy_key_with_empty_map_returns_unchanged(self):
        config = {"toolhead_mode": "afc", "scanner_lane_map": {}}
        result = _migrate_legacy_config(config)
        assert "scanners" not in result

    def test_afc_var_path_key_triggers_migration(self):
        config = {
            "afc_var_path": "/some/path",
            "scanner_lane_map": {"aaa": "lane1"},
        }
        result = _migrate_legacy_config(config)
        # default mode is "afc" when toolhead_mode absent
        assert result["scanners"]["aaa"]["action"] == "afc_lane"


class TestHasScannerFunctions(unittest.TestCase):

    def test_has_afc_scanners_true_for_afc_stage(self):
        config = {"scanners": {"a": {"action": "afc_stage"}}}
        assert has_afc_scanners(config) is True

    def test_has_afc_scanners_true_for_afc_lane(self):
        config = {"scanners": {"a": {"action": "afc_lane", "lane": "lane1"}}}
        assert has_afc_scanners(config) is True

    def test_has_afc_scanners_false_for_toolhead(self):
        config = {"scanners": {"a": {"action": "toolhead", "toolhead": "T0"}}}
        assert has_afc_scanners(config) is False

    def test_has_afc_scanners_empty_config(self):
        assert has_afc_scanners({}) is False

    def test_has_toolhead_scanners_true(self):
        config = {"scanners": {"a": {"action": "toolhead", "toolhead": "T0"}}}
        assert has_toolhead_scanners(config) is True

    def test_has_toolhead_scanners_false_for_afc(self):
        config = {"scanners": {"a": {"action": "afc_lane", "lane": "lane1"}}}
        assert has_toolhead_scanners(config) is False

    def test_has_toolhead_scanners_false_for_toolhead_stage(self):
        config = {"scanners": {"a": {"action": "toolhead_stage"}}}
        assert has_toolhead_scanners(config) is False

    def test_has_toolhead_stage_scanners_true(self):
        config = {"scanners": {"a": {"action": "toolhead_stage"}}}
        assert has_toolhead_stage_scanners(config) is True

    def test_has_toolhead_stage_scanners_false_for_toolhead(self):
        config = {"scanners": {"a": {"action": "toolhead", "toolhead": "T0"}}}
        assert has_toolhead_stage_scanners(config) is False

    def test_has_toolhead_stage_scanners_empty(self):
        assert has_toolhead_stage_scanners({}) is False

    def test_mixed_scanners_all_functions(self):
        config = {
            "scanners": {
                "a": {"action": "afc_lane", "lane": "lane1"},
                "b": {"action": "toolhead", "toolhead": "T0"},
                "c": {"action": "toolhead_stage"},
            }
        }
        assert has_afc_scanners(config) is True
        assert has_toolhead_scanners(config) is True
        assert has_toolhead_stage_scanners(config) is True


class TestDiscoverKlipperVarPath(unittest.TestCase):
    """Verifies discover_klipper_var_path() hits the correct Moonraker endpoint
    and parses the response shape correctly (#77)."""

    def setUp(self):
        app_state.cfg = {"moonraker_url": "http://moonraker:7125"}

    def _moonraker_response(self, filename: str | None) -> MagicMock:
        save_variables = {"filename": filename} if filename is not None else {}
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json = MagicMock(return_value={
            "result": {
                "status": {
                    "configfile": {
                        "settings": {
                            "save_variables": save_variables,
                        }
                    }
                }
            }
        })
        return resp

    def test_uses_correct_moonraker_endpoint(self):
        with patch("config.requests.get") as mock_get:
            mock_get.return_value = self._moonraker_response("/home/pi/printer_data/config/variables.cfg")
            discover_klipper_var_path()
            args, kwargs = mock_get.call_args
            assert "printer/objects/query?configfile=settings" in args[0], args[0]

    def test_parses_absolute_filename(self):
        with patch("config.requests.get") as mock_get:
            mock_get.return_value = self._moonraker_response("/home/pi/printer_data/config/variables.cfg")
            result = discover_klipper_var_path()
            assert result == "/home/pi/printer_data/config/variables.cfg"

    def test_relative_filename_resolved_to_default_config_dir(self):
        with patch("config.requests.get") as mock_get:
            mock_get.return_value = self._moonraker_response("variables.cfg")
            result = discover_klipper_var_path()
            assert result.endswith("/printer_data/config/variables.cfg")

    def test_tilde_filename_expanded_to_home(self):
        # Klipper's configfile.settings can report paths with a literal `~`.
        # Without expansion, os.path.join with the default config dir produces
        # a broken concatenated path (verified against a real toolchanger
        # printer reporting `~/printer_data/config/...`).
        with patch("config.requests.get") as mock_get:
            mock_get.return_value = self._moonraker_response("~/printer_data/config/variables.cfg")
            result = discover_klipper_var_path()
            assert result == os.path.expanduser("~/printer_data/config/variables.cfg")
            assert "~" not in result
            assert result.startswith("/")

    def test_missing_save_variables_returns_none(self):
        with patch("config.requests.get") as mock_get:
            mock_get.return_value = self._moonraker_response(None)
            assert discover_klipper_var_path() is None

    def test_network_failure_returns_none(self):
        with patch("config.requests.get", side_effect=requests.ConnectionError("connection refused")):
            assert discover_klipper_var_path() is None

    def test_http_error_returns_none(self):
        with patch("config.requests.get") as mock_get:
            resp = MagicMock()
            resp.raise_for_status = MagicMock(side_effect=requests.HTTPError("404"))
            mock_get.return_value = resp
            assert discover_klipper_var_path() is None

    def test_invalid_json_returns_none(self):
        with patch("config.requests.get") as mock_get:
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            resp.json = MagicMock(side_effect=ValueError("not json"))
            mock_get.return_value = resp
            assert discover_klipper_var_path() is None

    def test_unexpected_exception_propagates(self):
        # Programming errors (KeyError, AttributeError) should NOT be silently
        # swallowed — they indicate bugs we want to surface, not hide.
        with patch("config.requests.get", side_effect=KeyError("config")):
            with self.assertRaises(KeyError):
                discover_klipper_var_path()

    def test_returns_cached_path_without_querying_moonraker(self):
        app_state.cfg["klipper_var_path"] = "/cached/path/variables.cfg"
        with patch("config.requests.get") as mock_get:
            result = discover_klipper_var_path()
            assert result == "/cached/path/variables.cfg"
            mock_get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
