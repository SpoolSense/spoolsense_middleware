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
    _is_printer_idle,
    _resolve_scanner_from_topic,
    _get_scanner_target,
    _should_auto_release_lock,
    on_connect,
    on_message,
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
    app_state.active_spool_uids = {}
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


class TestIsPrinterIdle(unittest.TestCase):
    """Verifies the Klipper print-state probe used as a safety guard before
    auto-releasing a toolhead lock. Idle = standby; everything else (including
    fetch failure) is treated as busy."""

    def setUp(self):
        _reset_app_state()

    def _resp(self, state: str) -> MagicMock:
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json = MagicMock(return_value={
            "result": {"status": {"print_stats": {"state": state}}}
        })
        return resp

    def test_returns_true_when_standby(self):
        with patch("mqtt_handler.requests.get") as mock_get:
            mock_get.return_value = self._resp("standby")
            assert _is_printer_idle() is True

    def test_returns_false_when_printing(self):
        with patch("mqtt_handler.requests.get") as mock_get:
            mock_get.return_value = self._resp("printing")
            assert _is_printer_idle() is False

    def test_returns_false_when_paused(self):
        with patch("mqtt_handler.requests.get") as mock_get:
            mock_get.return_value = self._resp("paused")
            assert _is_printer_idle() is False

    def test_returns_false_on_network_error(self):
        import requests
        with patch("mqtt_handler.requests.get", side_effect=requests.ConnectionError("boom")):
            assert _is_printer_idle() is False

    def test_returns_false_when_no_moonraker_url(self):
        app_state.cfg["moonraker_url"] = ""
        assert _is_printer_idle() is False


class TestShouldAutoReleaseLock(unittest.TestCase):
    """Decision logic for whether a locked target releases on an incoming scan."""

    def setUp(self):
        _reset_app_state()
        app_state.active_spool_uids["T0"] = "abc123"

    def test_same_uid_does_not_auto_release(self):
        # Same tag scanned twice — no swap intent, lock stays
        with patch("mqtt_handler._is_printer_idle", return_value=True):
            assert _should_auto_release_lock("T0", {"uid": "abc123"}) is False

    def test_same_uid_case_insensitive(self):
        with patch("mqtt_handler._is_printer_idle", return_value=True):
            assert _should_auto_release_lock("T0", {"uid": "ABC123"}) is False

    def test_no_uid_does_not_auto_release(self):
        # Tag-removed event or empty payload — leave lock alone
        with patch("mqtt_handler._is_printer_idle", return_value=True):
            assert _should_auto_release_lock("T0", {"uid": None}) is False
            assert _should_auto_release_lock("T0", {}) is False

    def test_different_uid_idle_releases(self):
        with patch("mqtt_handler._is_printer_idle", return_value=True):
            assert _should_auto_release_lock("T0", {"uid": "DIFFER"}) is True

    def test_different_uid_busy_holds_lock(self):
        with patch("mqtt_handler._is_printer_idle", return_value=False):
            assert _should_auto_release_lock("T0", {"uid": "DIFFER"}) is False

    def test_different_uid_idle_releases_when_no_active_uid_tracked(self):
        # Edge: lock is set but tracking is empty (e.g. lock set without
        # _record_spool_tracking running). Different incoming UID + idle
        # printer should still release.
        app_state.active_spool_uids = {}
        with patch("mqtt_handler._is_printer_idle", return_value=True):
            assert _should_auto_release_lock("T0", {"uid": "anything"}) is True


class TestOnMessageLockBehavior(unittest.TestCase):
    """End-to-end on_message lock-gate behavior covering #76 repros."""

    def setUp(self):
        _reset_app_state(
            scanners={"f08538": {"action": "toolhead", "toolhead": "T0"}},
        )
        app_state.DISPATCHER_AVAILABLE = True
        app_state.lane_locks["T0"] = True
        app_state.active_spool_uids["T0"] = "abc123"

    def _msg(self, uid: str | None) -> MagicMock:
        m = MagicMock()
        body = {"uid": uid} if uid is not None else {}
        m.payload = MagicMock()
        m.payload.decode = MagicMock(return_value=__import__("json").dumps(body))
        m.topic = "spoolsense/f08538/tag/state"
        return m

    def test_locked_same_uid_is_dropped(self):
        # Same tag rescanned — no auto-release, no further processing
        with patch("mqtt_handler._is_printer_idle", return_value=True), \
             patch("mqtt_handler._handle_rich_tag") as mock_handle:
            on_message(MagicMock(), None, self._msg("abc123"))
            mock_handle.assert_not_called()
        assert app_state.lane_locks["T0"] is True

    def test_locked_different_uid_idle_auto_releases_and_processes(self):
        # The #76 repro: scan A is locked, scan B is different UID, printer idle.
        # Should release the lock and fall through to _handle_rich_tag.
        with patch("mqtt_handler._is_printer_idle", return_value=True), \
             patch("mqtt_handler._handle_rich_tag") as mock_handle:
            on_message(MagicMock(), None, self._msg("DIFFER"))
            mock_handle.assert_called_once()
        assert app_state.lane_locks["T0"] is False

    def test_locked_different_uid_printing_holds_lock(self):
        # Stray scan during a print — lock holds, scan dropped.
        with patch("mqtt_handler._is_printer_idle", return_value=False), \
             patch("mqtt_handler._handle_rich_tag") as mock_handle:
            on_message(MagicMock(), None, self._msg("DIFFER"))
            mock_handle.assert_not_called()
        assert app_state.lane_locks["T0"] is True

    def test_unlocked_scan_processes_normally(self):
        # Baseline: no lock means we always process.
        app_state.lane_locks["T0"] = False
        with patch("mqtt_handler._handle_rich_tag") as mock_handle:
            on_message(MagicMock(), None, self._msg("anything"))
            mock_handle.assert_called_once()

    def test_locked_blank_tag_idle_releases_and_handler_called(self):
        # Edge: incoming scan has a different UID but is a blank tag. The
        # auto-release logic only knows the UID at this stage — it can't tell
        # the tag is unusable. Expected: lock released, _handle_rich_tag is
        # invoked (where it would log the blank tag and return without
        # re-locking). End state is unlocked-with-no-active-spool, which is
        # acceptable: the user clearly removed the original tag.
        import json
        blank_payload = {
            "uid": "BLANK01",
            "present": True,
            "tag_data_valid": False,
            "blank": True,
        }
        msg = MagicMock()
        msg.payload = MagicMock()
        msg.payload.decode = MagicMock(return_value=json.dumps(blank_payload))
        msg.topic = "spoolsense/f08538/tag/state"

        with patch("mqtt_handler._is_printer_idle", return_value=True), \
             patch("mqtt_handler._handle_rich_tag") as mock_handle:
            on_message(MagicMock(), None, msg)
            mock_handle.assert_called_once()
        assert app_state.lane_locks["T0"] is False


if __name__ == "__main__":
    unittest.main()
