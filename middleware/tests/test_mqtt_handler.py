"""Tests for mqtt_handler.py — topic parsing, scanner resolution, tag routing."""
from __future__ import annotations

import os
import sys
import threading
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

sys.modules.setdefault("paho", MagicMock())
sys.modules.setdefault("paho.mqtt", MagicMock())
sys.modules.setdefault("paho.mqtt.client", MagicMock())
sys.modules.setdefault("watchdog", MagicMock())
sys.modules.setdefault("watchdog.observers", MagicMock())
sys.modules.setdefault("watchdog.events", MagicMock())

# Stub optional heavy dependencies before importing mqtt_handler
for mod in (
    "var_watcher",
    "adapters",
    "adapters.dispatcher",
    "tag_sync",
    "tag_sync.policy",
    "tag_sync.scanner_writer",
    "state",
    "state.models",
    "spoolman",
    "spoolman.client",
    "yaml",
):
    sys.modules.setdefault(mod, MagicMock())

import app_state  # noqa: E402

# Patch app_state.DISPATCHER_AVAILABLE before mqtt_handler is imported
app_state.DISPATCHER_AVAILABLE = False

from mqtt_handler import (  # noqa: E402
    _extract_scanner_device_id,
    _resolve_scanner_from_topic,
    _get_scanner_target,
    on_connect,
)


def _reset_app_state(prefix="spoolsense", scanners=None):
    app_state.cfg = {
        "moonraker_url": "http://moonraker:7125",
        "scanner_topic_prefix": prefix,
        "scanners": scanners or {},
        "low_spool_threshold": 100,
    }
    app_state.lane_locks = {}
    app_state.active_spools = {}
    app_state.pending_spool = None
    app_state.state_lock = threading.Lock()


class TestExtractScannerDeviceId(unittest.TestCase):

    def setUp(self):
        _reset_app_state(prefix="spoolsense")

    def test_valid_topic_returns_device_id(self):
        result = _extract_scanner_device_id("spoolsense/ecb338/tag/state")
        assert result == "ecb338"

    def test_valid_topic_mixed_case_device_id(self):
        result = _extract_scanner_device_id("spoolsense/ECB338AB/tag/state")
        assert result == "ECB338AB"

    def test_wrong_prefix_returns_none(self):
        result = _extract_scanner_device_id("othertopic/ecb338/tag/state")
        assert result is None

    def test_wrong_suffix_returns_none(self):
        result = _extract_scanner_device_id("spoolsense/ecb338/tag/event")
        assert result is None

    def test_too_short_topic_returns_none(self):
        result = _extract_scanner_device_id("spoolsense/ecb338/state")
        assert result is None

    def test_too_long_topic_returns_none(self):
        result = _extract_scanner_device_id("spoolsense/ecb338/tag/state/extra")
        assert result is None

    def test_empty_string_returns_none(self):
        result = _extract_scanner_device_id("")
        assert result is None

    def test_custom_prefix(self):
        _reset_app_state(prefix="myprinter")
        result = _extract_scanner_device_id("myprinter/abc123/tag/state")
        assert result == "abc123"

    def test_custom_prefix_wrong_prefix_returns_none(self):
        _reset_app_state(prefix="myprinter")
        result = _extract_scanner_device_id("spoolsense/abc123/tag/state")
        assert result is None


class TestResolveScannerFromTopic(unittest.TestCase):

    def setUp(self):
        _reset_app_state(
            scanners={
                "ecb338": {"action": "afc_lane", "lane": "lane1"},
                "abcd12": {"action": "toolhead", "toolhead": "T0"},
            }
        )

    def test_known_scanner_returns_config(self):
        result = _resolve_scanner_from_topic("spoolsense/ecb338/tag/state")
        assert result == {"action": "afc_lane", "lane": "lane1"}

    def test_another_known_scanner_returns_config(self):
        result = _resolve_scanner_from_topic("spoolsense/abcd12/tag/state")
        assert result == {"action": "toolhead", "toolhead": "T0"}

    def test_unknown_scanner_returns_none(self):
        result = _resolve_scanner_from_topic("spoolsense/unknown99/tag/state")
        assert result is None

    def test_invalid_topic_format_returns_none(self):
        result = _resolve_scanner_from_topic("spoolsense/ecb338/tag")
        assert result is None

    def test_empty_scanners_config_returns_none(self):
        _reset_app_state(scanners={})
        result = _resolve_scanner_from_topic("spoolsense/ecb338/tag/state")
        assert result is None


class TestGetScannerTarget(unittest.TestCase):

    def test_returns_lane_for_afc_lane(self):
        scanner_cfg = {"action": "afc_lane", "lane": "lane1"}
        assert _get_scanner_target(scanner_cfg) == "lane1"

    def test_returns_toolhead_for_toolhead(self):
        scanner_cfg = {"action": "toolhead", "toolhead": "T0"}
        assert _get_scanner_target(scanner_cfg) == "T0"

    def test_returns_none_for_afc_stage(self):
        scanner_cfg = {"action": "afc_stage"}
        assert _get_scanner_target(scanner_cfg) is None

    def test_returns_none_for_toolhead_stage(self):
        scanner_cfg = {"action": "toolhead_stage"}
        assert _get_scanner_target(scanner_cfg) is None

    def test_lane_takes_precedence_when_both_set(self):
        # Shouldn't happen in valid config, but test the logic
        scanner_cfg = {"action": "afc_lane", "lane": "lane2", "toolhead": "T0"}
        # lane is checked first via `or`
        assert _get_scanner_target(scanner_cfg) == "lane2"


class TestOnConnect(unittest.TestCase):

    def setUp(self):
        _reset_app_state(
            scanners={"f08538": {"action": "toolhead", "toolhead": "T0"}},
        )
        app_state.DISPATCHER_AVAILABLE = True
        app_state.spoolman_client = None
        app_state.watcher = None

    def test_on_connect_toolhead_action_does_not_raise(self):
        client = MagicMock()
        with patch("mqtt_handler.discover_klipper_var_path", return_value="/tmp/x.cfg"):
            on_connect(client, None, {}, 0)
        client.subscribe.assert_called_once_with("spoolsense/f08538/tag/state")


if __name__ == "__main__":
    unittest.main()
