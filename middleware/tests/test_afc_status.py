"""Tests for afc_status.py — AFC lane state sync, lock/clear publishing, resync on reconnect."""
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

import app_state  # noqa: E402
from afc_status import _sync_lane_state, _fetch_afc_status, resync_lock_state  # noqa: E402


def _reset_app_state():
    app_state.cfg = {"moonraker_url": "http://moonraker:7125", "low_spool_threshold": 100}
    app_state.lane_locks = {}
    app_state.active_spools = {}
    app_state.lane_statuses = {}
    app_state.lane_load_states = {}
    app_state.pending_spool = None
    app_state.state_lock = threading.Lock()


def _make_afc_data(unit="Turtle_1", lane="lane1", spool_id=None, load=False, status=None):
    """Helper to construct a minimal AFC status payload."""
    lane_data = {"load": load}
    if spool_id is not None:
        lane_data["spool_id"] = spool_id
    if status is not None:
        lane_data["status"] = status
    return {
        "status:": {
            "AFC": {
                unit: {
                    lane: lane_data,
                    "system": {"some": "data"},  # should be skipped
                }
            }
        }
    }


class TestSyncLaneState(unittest.TestCase):

    def setUp(self):
        _reset_app_state()

    def test_spool_id_present_triggers_lock(self):
        data = _make_afc_data(spool_id=42, load=True)
        with patch("afc_status.publish_lock") as mock_lock:
            _sync_lane_state(data)
            mock_lock.assert_called_once_with("lane1", "lock")
        assert app_state.active_spools.get("lane1") == 42

    def test_spool_id_none_was_locked_triggers_clear(self):
        app_state.lane_locks["lane1"] = True
        data = _make_afc_data(spool_id=None, load=False)
        with patch("afc_status.publish_lock") as mock_lock:
            _sync_lane_state(data)
            mock_lock.assert_called_once_with("lane1", "clear")
        assert app_state.active_spools.get("lane1") is None

    def test_spool_id_present_already_locked_no_duplicate_lock(self):
        app_state.lane_locks["lane1"] = True
        data = _make_afc_data(spool_id=42, load=True)
        with patch("afc_status.publish_lock") as mock_lock:
            _sync_lane_state(data)
            # Already locked — should not re-lock
            mock_lock.assert_not_called()

    def test_newly_loaded_lane_with_pending_spool_sends_data(self):
        app_state.lane_load_states["lane1"] = False  # was unloaded
        app_state.pending_spool = {
            "color_hex": "FF0000",
            "material": "PLA",
            "remaining_g": 250.0,
            "spoolman_id": None,
        }
        data = _make_afc_data(spool_id=None, load=True)  # now loaded

        # _send_afc_lane_data is called via threading.Timer inside _send_lane_data_delayed.
        # Patch Timer so it fires synchronously (0s delay) to make the test deterministic.
        def immediate_timer(delay, func, args=(), kwargs=None):
            func(*args, **(kwargs or {}))
            t = MagicMock()
            t.start = MagicMock()
            return t

        with patch("afc_status.threading.Timer", side_effect=immediate_timer):
            with patch("afc_status._send_afc_lane_data") as mock_send:
                with patch("afc_status.publish_lock"):
                    _sync_lane_state(data)
                mock_send.assert_called_once_with("lane1", "FF0000", "PLA", 250.0)
        # pending_spool consumed
        assert app_state.pending_spool is None

    def test_already_loaded_lane_no_false_trigger(self):
        app_state.lane_load_states["lane1"] = True  # already loaded
        app_state.pending_spool = {
            "color_hex": "00FF00",
            "material": "PETG",
            "remaining_g": 150.0,
            "spoolman_id": None,
        }
        data = _make_afc_data(spool_id=None, load=True)
        with patch("afc_status._send_afc_lane_data") as mock_send:
            with patch("afc_status.publish_lock"):
                _sync_lane_state(data)
            # Already loaded — no send triggered
            mock_send.assert_not_called()
        # pending_spool should remain untouched
        assert app_state.pending_spool is not None

    def test_system_key_skipped(self):
        data = {
            "status:": {
                "AFC": {
                    "system": {"some": "top-level-system-data"},
                    "Turtle_1": {
                        "lane1": {"spool_id": 5, "load": True},
                    },
                }
            }
        }
        with patch("afc_status.publish_lock") as mock_lock:
            _sync_lane_state(data)
        # system key should be skipped; only lane1 processed
        assert app_state.active_spools.get("lane1") == 5

    def test_tools_key_skipped(self):
        data = {
            "status:": {
                "AFC": {
                    "Tools": {"T0": "something"},
                    "Turtle_1": {
                        "lane1": {"spool_id": 3, "load": True},
                    },
                }
            }
        }
        with patch("afc_status.publish_lock"):
            _sync_lane_state(data)
        assert app_state.active_spools.get("lane1") == 3

    def test_status_field_stored(self):
        data = _make_afc_data(spool_id=10, load=True, status="loaded")
        with patch("afc_status.publish_lock"):
            _sync_lane_state(data)
        assert app_state.lane_statuses.get("lane1") == "loaded"

    def test_alt_status_key_without_colon(self):
        data = {
            "status": {
                "AFC": {
                    "Turtle_1": {
                        "lane1": {"spool_id": 7, "load": True},
                    }
                }
            }
        }
        with patch("afc_status.publish_lock"):
            _sync_lane_state(data)
        assert app_state.active_spools.get("lane1") == 7


class TestResyncLockState(unittest.TestCase):

    def setUp(self):
        _reset_app_state()

    def test_resync_publishes_locked_lanes(self):
        app_state.lane_locks["lane1"] = True
        app_state.lane_locks["lane2"] = False
        with patch("afc_status.publish_lock") as mock_lock:
            resync_lock_state()
            calls = {(c[0][0], c[0][1]) for c in mock_lock.call_args_list}
            assert ("lane1", "lock") in calls
            assert ("lane2", "clear") in calls

    def test_resync_no_lanes_no_calls(self):
        with patch("afc_status.publish_lock") as mock_lock:
            resync_lock_state()
            mock_lock.assert_not_called()

    def test_resync_all_locked(self):
        app_state.lane_locks["lane1"] = True
        app_state.lane_locks["lane2"] = True
        with patch("afc_status.publish_lock") as mock_lock:
            resync_lock_state()
            calls = {(c[0][0], c[0][1]) for c in mock_lock.call_args_list}
            assert ("lane1", "lock") in calls
            assert ("lane2", "lock") in calls
            assert len(mock_lock.call_args_list) == 2


class TestFetchAfcStatus(unittest.TestCase):

    def setUp(self):
        _reset_app_state()

    @patch("requests.get")
    def test_success_returns_parsed_data(self, mock_get):
        payload = {"result": {"status:": {"AFC": {"unit1": {"lane1": {"spool_id": 1}}}}}}
        mock_resp = MagicMock()
        mock_resp.json.return_value = payload
        mock_resp.raise_for_status = lambda: None
        mock_get.return_value = mock_resp

        result = _fetch_afc_status()
        assert result == payload["result"]

    @patch("requests.get")
    def test_connection_error_returns_none(self, mock_get):
        import requests as req
        mock_get.side_effect = req.ConnectionError("refused")
        result = _fetch_afc_status()
        assert result is None

    @patch("requests.get")
    def test_timeout_returns_none(self, mock_get):
        import requests as req
        mock_get.side_effect = req.Timeout()
        result = _fetch_afc_status()
        assert result is None

    @patch("requests.get")
    def test_404_returns_none(self, mock_get):
        import requests as req
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        http_err = req.HTTPError(response=mock_resp)
        mock_resp.raise_for_status.side_effect = http_err
        mock_get.return_value = mock_resp
        result = _fetch_afc_status()
        assert result is None

    @patch("requests.get")
    def test_response_without_result_envelope_returned_directly(self, mock_get):
        payload = {"status:": {"AFC": {}}}
        mock_resp = MagicMock()
        mock_resp.json.return_value = payload
        mock_resp.raise_for_status = lambda: None
        mock_get.return_value = mock_resp

        result = _fetch_afc_status()
        assert result == payload

    def test_no_moonraker_url_returns_none(self):
        app_state.cfg["moonraker_url"] = ""
        result = _fetch_afc_status()
        assert result is None


if __name__ == "__main__":
    unittest.main()
