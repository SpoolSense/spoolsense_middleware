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
from afc_status import (  # noqa: E402
    _sync_lane_state,
    _sync_lane_state_single,
    _fetch_afc_status,
    resync_lock_state,
)


def _reset_app_state():
    app_state.cfg = {"moonraker_url": "http://moonraker:7125", "low_spool_threshold": 100}
    app_state.lane_locks = {}
    app_state.active_spools = {}
    app_state.active_spool_tracking = {}
    app_state.lane_statuses = {}
    app_state.lane_load_states = {}
    app_state.pending_spool_afc = None
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
        app_state.pending_spool_afc = {
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
                mock_send.assert_called_once_with("http://moonraker:7125", "lane1", "FF0000", "PLA", 250.0)
        # pending_spool consumed
        assert app_state.pending_spool_afc is None

    def test_already_loaded_lane_no_false_trigger(self):
        app_state.lane_load_states["lane1"] = True  # already loaded
        app_state.pending_spool_afc = {
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
        assert app_state.pending_spool_afc is not None

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


class TestLaneLoadRecordsTracking(unittest.TestCase):
    """An afc_stage scan consumed on lane load records a deduction baseline
    for that lane — previously only dedicated afc_lane scans got one (#109)."""

    def setUp(self):
        _reset_app_state()

    def _load_lane_with_pending(self, pending):
        app_state.lane_load_states["lane1"] = False
        app_state.pending_spool_afc = pending
        data = _make_afc_data(spool_id=None, load=True)
        with patch("afc_status.threading.Timer"):
            with patch("afc_status.publish_lock"):
                _sync_lane_state(data)

    def test_staged_scan_with_uid_records_baseline(self):
        self._load_lane_with_pending({
            "color_hex": "FF0000", "material": "PLA", "remaining_g": 250.0,
            "baseline_g": 250.0,
            "spoolman_id": None, "uid": "AABBCC", "device_id": "4d9620",
            "diameter_mm": 1.75, "density": 1.24, "tag_format": "openprinttag",
        })
        rec = app_state.active_spool_tracking.get("lane1")
        self.assertIsNotNone(rec)
        self.assertEqual(rec.uid, "aabbcc")
        self.assertEqual(rec.weight_g, 250.0)

    def test_staged_scan_without_uid_records_nothing(self):
        self._load_lane_with_pending({
            "color_hex": "FF0000", "material": "PLA", "remaining_g": 250.0,
            "spoolman_id": None,
        })
        self.assertNotIn("lane1", app_state.active_spool_tracking)

    def test_staged_scan_without_baseline_records_uid_only(self):
        # #119: no baseline (e.g. nominal tag, no Spoolman match) still
        # records the uid so deduction routing works; weight stays None
        self._load_lane_with_pending({
            "color_hex": "FF0000", "material": "PLA", "remaining_g": 1000.0,
            "baseline_g": None, "spoolman_id": 5, "uid": "AABBCC",
        })
        rec = app_state.active_spool_tracking.get("lane1")
        self.assertIsNotNone(rec)
        self.assertEqual(rec.uid, "aabbcc")
        self.assertIsNone(rec.weight_g)

    def test_unrecordable_pending_clears_previous_baseline(self):
        # A new spool loaded but with no baseline data — the OLD spool's
        # record must not survive to receive its deductions
        app_state.active_spool_tracking["lane1"] = app_state.ActiveSpool(
            uid="oldspool", weight_g=400.0)
        self._load_lane_with_pending({
            "color_hex": "FF0000", "material": "PLA", "remaining_g": None,
            "spoolman_id": None,
        })
        self.assertNotIn("lane1", app_state.active_spool_tracking)

    def test_poll_unload_clears_tag_only_baseline(self):
        # Tag-only staged lanes never lock and have no spool_id, so the
        # action-based clear can't fire — the load transition must clear
        app_state.lane_load_states["lane1"] = True
        app_state.active_spool_tracking["lane1"] = app_state.ActiveSpool(
            uid="aabbcc", weight_g=250.0)
        data = _make_afc_data(spool_id=None, load=False)
        with patch("afc_status.publish_lock"):
            _sync_lane_state(data)
        self.assertNotIn("lane1", app_state.active_spool_tracking)

    def test_ws_unload_clears_tag_only_baseline(self):
        app_state.lane_load_states["lane1"] = True
        app_state.active_spool_tracking["lane1"] = app_state.ActiveSpool(
            uid="aabbcc", weight_g=250.0)
        with patch("afc_status.publish_lock"):
            _sync_lane_state_single("lane1", {"load": False})
        self.assertNotIn("lane1", app_state.active_spool_tracking)

    def test_poll_mismatched_spool_id_leaves_pending_staged(self):
        # Spool A staged, but the lane loads spool B (assigned externally) —
        # A must stay staged and B's lane must not get A's data or baseline
        app_state.lane_load_states["lane1"] = False
        staged = {
            "color_hex": "FF0000", "material": "PLA", "remaining_g": 250.0,
            "spoolman_id": 42, "uid": "AABBCC", "tag_format": "openprinttag",
        }
        app_state.pending_spool_afc = staged
        data = _make_afc_data(spool_id=99, load=True)
        with patch("afc_status.threading.Timer"):
            with patch("afc_status.publish_lock"):
                _sync_lane_state(data)
        self.assertIs(app_state.pending_spool_afc, staged)
        self.assertNotIn("lane1", app_state.active_spool_tracking)

    def test_ws_mismatched_spool_id_leaves_pending_staged(self):
        app_state.lane_load_states["lane1"] = False
        staged = {"color_hex": "FF0000", "material": "PLA",
                  "remaining_g": 250.0, "spoolman_id": 42, "uid": "AABBCC"}
        app_state.pending_spool_afc = staged
        with patch("afc_status.threading.Timer"):
            with patch("afc_status.publish_lock"):
                _sync_lane_state_single("lane1", {"spool_id": 99, "load": True})
        self.assertIs(app_state.pending_spool_afc, staged)

    def test_ws_load_only_delta_respects_cached_spool_id(self):
        # Partial delta {"load": true} omits spool_id (= unchanged) — the
        # lane's CACHED id (99) must block consuming staged spool 42
        app_state.lane_load_states["lane1"] = False
        app_state.active_spools["lane1"] = 99
        staged = {"color_hex": "FF0000", "material": "PLA",
                  "remaining_g": 250.0, "spoolman_id": 42, "uid": "AABBCC"}
        app_state.pending_spool_afc = staged
        with patch("afc_status.threading.Timer"):
            with patch("afc_status.publish_lock"):
                _sync_lane_state_single("lane1", {"load": True})
        self.assertIs(app_state.pending_spool_afc, staged)

    def test_ws_load_only_delta_consumes_when_cached_id_matches(self):
        app_state.lane_load_states["lane1"] = False
        app_state.active_spools["lane1"] = 42
        staged = {"color_hex": "FF0000", "material": "PLA",
                  "remaining_g": 250.0, "spoolman_id": 42, "uid": "AABBCC",
                  "tag_format": "openprinttag"}
        app_state.pending_spool_afc = staged
        with patch("afc_status.threading.Timer"):
            with patch("afc_status.publish_lock"):
                _sync_lane_state_single("lane1", {"load": True})
        self.assertIsNone(app_state.pending_spool_afc)
        self.assertIn("lane1", app_state.active_spool_tracking)

    def test_poll_tag_only_staged_not_claimed_by_spoolman_lane(self):
        # Tag-only spool staged; a lane loads an externally-assigned
        # Spoolman spool — the staged record must not be claimed by it
        app_state.lane_load_states["lane1"] = False
        staged = {"color_hex": "FF0000", "material": "PLA",
                  "remaining_g": 250.0, "spoolman_id": None, "uid": "AABBCC"}
        app_state.pending_spool_afc = staged
        data = _make_afc_data(spool_id=99, load=True)
        with patch("afc_status.threading.Timer"):
            with patch("afc_status.publish_lock"):
                _sync_lane_state(data)
        self.assertIs(app_state.pending_spool_afc, staged)

    def test_ws_explicit_null_clears_cached_id_then_staged_consumes(self):
        # Removal delta {"spool_id": null} must clear the cached id, so a
        # later load-only delta can consume freshly staged data instead of
        # being blocked by a stale id forever
        app_state.lane_load_states["lane1"] = True
        app_state.active_spools["lane1"] = 99
        with patch("afc_status.publish_lock"):
            _sync_lane_state_single("lane1", {"spool_id": None, "load": False})
        self.assertIsNone(app_state.active_spools.get("lane1"))
        staged = {"color_hex": "FF0000", "material": "PLA",
                  "remaining_g": 250.0, "spoolman_id": 42, "uid": "AABBCC",
                  "tag_format": "openprinttag"}
        app_state.pending_spool_afc = staged
        with patch("afc_status.threading.Timer"):
            with patch("afc_status.publish_lock"):
                _sync_lane_state_single("lane1", {"load": True})
        self.assertIsNone(app_state.pending_spool_afc)

    def test_poll_zero_spool_id_treated_as_empty(self):
        # AFC reports 0 for "no spool" — polling must not lock an empty
        # lane, and a locked lane reporting 0 must clear (parity with the
        # websocket path, which always treated 0 as removal)
        app_state.lane_locks["lane1"] = True
        app_state.active_spools["lane1"] = 42
        data = _make_afc_data(spool_id=0, load=False)
        with patch("afc_status.publish_lock") as mock_lock:
            _sync_lane_state(data)
            mock_lock.assert_called_once_with("lane1", "clear")
        self.assertIsNone(app_state.active_spools.get("lane1"))

    def test_poll_zero_spool_id_load_consumes_tag_only_staged(self):
        app_state.lane_load_states["lane1"] = False
        staged = {"color_hex": "FF0000", "material": "PLA",
                  "remaining_g": 250.0, "spoolman_id": None, "uid": "AABBCC",
                  "tag_format": "openprinttag"}
        app_state.pending_spool_afc = staged
        data = _make_afc_data(spool_id=0, load=True)
        with patch("afc_status.threading.Timer"):
            with patch("afc_status.publish_lock"):
                _sync_lane_state(data)
        self.assertIsNone(app_state.pending_spool_afc)
        self.assertIn("lane1", app_state.active_spool_tracking)

    def test_ws_zero_spool_id_load_consumes_tag_only_staged(self):
        # AFC reports spool_id 0 for "no spool" — a tag-only staged spool
        # must still be consumed when the lane loads with a zero id
        app_state.lane_load_states["lane1"] = False
        staged = {"color_hex": "FF0000", "material": "PLA",
                  "remaining_g": 250.0, "spoolman_id": None, "uid": "AABBCC",
                  "tag_format": "openprinttag"}
        app_state.pending_spool_afc = staged
        with patch("afc_status.threading.Timer"):
            with patch("afc_status.publish_lock"):
                _sync_lane_state_single("lane1", {"spool_id": 0, "load": True})
        self.assertIsNone(app_state.pending_spool_afc)
        self.assertIn("lane1", app_state.active_spool_tracking)

    def test_poll_spoolman_backed_load_consumes_pending(self):
        # SET_NEXT_SPOOL_ID lands before the poll sees load=true, so the
        # same poll reports BOTH — staged data must still be consumed and
        # the baseline recorded, not left for a later lane to steal
        app_state.lane_load_states["lane1"] = False
        app_state.pending_spool_afc = {
            "color_hex": "FF0000", "material": "PLA", "remaining_g": 250.0,
            "spoolman_id": 42, "uid": "AABBCC", "device_id": "4d9620",
            "tag_format": "openprinttag",
        }
        data = _make_afc_data(spool_id=42, load=True)
        with patch("afc_status.threading.Timer"):
            with patch("afc_status.publish_lock"):
                _sync_lane_state(data)
        self.assertIsNone(app_state.pending_spool_afc)
        rec = app_state.active_spool_tracking.get("lane1")
        self.assertIsNotNone(rec)
        self.assertEqual(rec.uid, "aabbcc")
        self.assertEqual(app_state.active_spools.get("lane1"), 42)


class TestSwapClearsTracking(unittest.TestCase):
    """An externally-driven spool swap (Mainsail SET_SPOOL_ID, no scan) must
    drop the lane's deduction baseline — it describes the removed spool. A
    scan-driven change updates active_spools first, so the incoming id matches
    and the fresh baseline must survive."""

    def setUp(self):
        _reset_app_state()

    def _baseline(self, lane="lane1"):
        app_state.active_spool_tracking[lane] = app_state.ActiveSpool(
            uid="aaa", weight_g=500.0)

    def test_ws_external_swap_clears_baseline(self):
        app_state.active_spools["lane1"] = 3
        self._baseline()
        with patch("afc_status.publish_lock"):
            _sync_lane_state_single("lane1", {"spool_id": 9})
        self.assertNotIn("lane1", app_state.active_spool_tracking)
        self.assertEqual(app_state.active_spools["lane1"], 9)

    def test_ws_scan_driven_id_keeps_baseline(self):
        # The scan already wrote active_spools=9 and the baseline — the
        # delta echoing the same id must not destroy it
        app_state.active_spools["lane1"] = 9
        self._baseline()
        with patch("afc_status.publish_lock"):
            _sync_lane_state_single("lane1", {"spool_id": 9})
        self.assertIn("lane1", app_state.active_spool_tracking)

    def test_poll_external_swap_clears_baseline(self):
        app_state.lane_locks["lane1"] = True
        app_state.active_spools["lane1"] = 3
        self._baseline()
        data = _make_afc_data(spool_id=9, load=True)
        with patch("afc_status.publish_lock"):
            _sync_lane_state(data)
        self.assertNotIn("lane1", app_state.active_spool_tracking)
        self.assertEqual(app_state.active_spools["lane1"], 9)

    def test_poll_scan_driven_id_keeps_baseline(self):
        app_state.lane_locks["lane1"] = True
        app_state.active_spools["lane1"] = 9
        self._baseline()
        data = _make_afc_data(spool_id=9, load=True)
        with patch("afc_status.publish_lock"):
            _sync_lane_state(data)
        self.assertIn("lane1", app_state.active_spool_tracking)


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
