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

import app_state  # noqa: E402

from config import (  # noqa: E402
    _apply_scanner_defaults,
    _validate_scanners,
    _validate_happy_hare,
    _derive_toolheads,
    _migrate_legacy_config,
    has_afc_scanners,
    has_happy_hare_scanners,
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
        # Single-toolhead users shouldn't need to specify toolhead: "T0" (#44).
        # Defaulting runs in load_config() via _apply_scanner_defaults() before
        # validation — mirror that order here.
        config = {"scanners": {"abc123": {"action": "toolhead"}}}
        _apply_scanner_defaults(config)
        self._ok(config)
        self.assertEqual(config["scanners"]["abc123"]["toolhead"], "T0")

    def test_explicit_toolhead_not_overwritten_by_default(self):
        config = {"scanners": {"abc123": {"action": "toolhead", "toolhead": "T3"}}}
        _apply_scanner_defaults(config)
        self.assertEqual(config["scanners"]["abc123"]["toolhead"], "T3")

    def test_defaults_do_not_touch_other_actions(self):
        config = {"scanners": {"abc123": {"action": "afc_stage"}}}
        _apply_scanner_defaults(config)
        self.assertNotIn("toolhead", config["scanners"]["abc123"])

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

    def test_happy_hare_stage_no_target_accepted(self):
        self._ok({"scanners": {"abc123": {"action": "happy_hare_stage"}}})

    def test_happy_hare_stage_with_lane_rejected(self):
        self._call({"scanners": {"abc123": {"action": "happy_hare_stage", "lane": "lane1"}}})

    def test_happy_hare_stage_with_toolhead_rejected(self):
        self._call({"scanners": {"abc123": {"action": "happy_hare_stage", "toolhead": "T0"}}})

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


class TestValidateHappyHare(unittest.TestCase):
    """The optional `happy_hare:` section + cross-check against scanner actions."""

    def _call(self, config):
        with self.assertRaises(SystemExit):
            _validate_happy_hare(config)

    def _ok(self, config):
        _validate_happy_hare(config)

    def test_section_absent_is_ok(self):
        # No happy_hare section, no happy_hare_stage scanners → defaults applied, no error
        cfg = {"scanners": {"abc123": {"action": "afc_stage"}}}
        self._ok(cfg)
        assert cfg["happy_hare"]["enabled"] is False

    def test_disabled_with_no_hh_scanner_is_ok(self):
        self._ok({
            "scanners": {"abc123": {"action": "afc_stage"}},
            "happy_hare": {"enabled": False},
        })

    def test_enabled_without_printer_name_rejected(self):
        self._call({
            "scanners": {"abc123": {"action": "happy_hare_stage"}},
            "happy_hare": {"enabled": True},
        })

    def test_hh_scanner_without_enabled_rejected(self):
        # Cross-check: scanner action present but integration disabled → fail loud
        self._call({
            "scanners": {"abc123": {"action": "happy_hare_stage"}},
            "happy_hare": {"enabled": False},
        })

    def test_hh_scanner_without_spoolman_url_rejected(self):
        # HH bind writes to Spoolman — tag-only mode isn't supported
        self._call({
            "scanners": {"abc123": {"action": "happy_hare_stage"}},
            "happy_hare": {"enabled": True, "printer_name": "muffin"},
        })

    def test_hh_scanner_with_spoolman_url_accepted(self):
        self._ok({
            "scanners": {"abc123": {"action": "happy_hare_stage"}},
            "happy_hare": {"enabled": True, "printer_name": "muffin"},
            "spoolman_url": "http://spoolman:7912",
        })

    def test_null_happy_hare_section_rejected(self):
        # `happy_hare: null` in YAML would crash setdefault otherwise
        self._call({
            "scanners": {"abc123": {"action": "afc_stage"}},
            "happy_hare": None,
        })

    def test_string_happy_hare_section_rejected(self):
        # `happy_hare: "yes"` in YAML would crash setdefault otherwise
        self._call({
            "scanners": {"abc123": {"action": "afc_stage"}},
            "happy_hare": "yes",
        })


class TestHasHappyHareScanners(unittest.TestCase):
    def test_returns_true_when_present(self):
        assert has_happy_hare_scanners(
            {"scanners": {"a": {"action": "happy_hare_stage"}}}
        ) is True

    def test_returns_false_when_only_other_actions(self):
        assert has_happy_hare_scanners(
            {"scanners": {"a": {"action": "afc_stage"}, "b": {"action": "toolhead"}}}
        ) is False

    def test_returns_false_when_no_scanners(self):
        assert has_happy_hare_scanners({}) is False


if __name__ == "__main__":
    unittest.main()
