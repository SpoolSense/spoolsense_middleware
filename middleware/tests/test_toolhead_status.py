"""
Tests for toolhead_status.py — Moonraker spool ID polling and eject detection.

Covers _fetch_active_spool_id() (network layer) and ToolheadStatusSync._check_transition()
(state machine). The poll loop itself is not tested directly — threading tests are slow and
fragile; the logic under test lives in the two functions above.
"""
from __future__ import annotations

import os
import sys
import threading
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

sys.modules.setdefault("paho", MagicMock())
sys.modules.setdefault("paho.mqtt", MagicMock())
sys.modules.setdefault("paho.mqtt.client", MagicMock())
sys.modules.setdefault("watchdog", MagicMock())
sys.modules.setdefault("watchdog.observers", MagicMock())
sys.modules.setdefault("watchdog.events", MagicMock())

import app_state  # noqa: E402
import toolhead_status  # noqa: E402
from toolhead_status import _fetch_active_spool_id, ToolheadStatusSync  # noqa: E402


def _reset_app_state(moonraker_url: str = "http://moonraker:7125") -> None:
    app_state.cfg = {
        "moonraker_url": moonraker_url,
        "scanners": {},
    }
    app_state.lane_locks = {}
    app_state.active_spools = {}
    app_state.state_lock = threading.Lock()


class TestFetchActiveSpoolId(unittest.TestCase):
    """Tests for _fetch_active_spool_id — the Moonraker HTTP layer."""

    def setUp(self) -> None:
        _reset_app_state()

    @patch("requests.get")
    def test_returns_spool_id_from_moonraker(self, mock_get: MagicMock) -> None:
        # Moonraker wraps the result in {"result": {"spool_id": N}}
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"result": {"spool_id": 42}}
        mock_resp.raise_for_status = lambda: None
        mock_get.return_value = mock_resp

        result = _fetch_active_spool_id()

        self.assertEqual(result, 42)

    @patch("requests.get")
    def test_returns_none_when_spool_id_is_zero(self, mock_get: MagicMock) -> None:
        # Moonraker returns 0 (not null) when the spool is cleared — treat as no spool
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"result": {"spool_id": 0}}
        mock_resp.raise_for_status = lambda: None
        mock_get.return_value = mock_resp

        result = _fetch_active_spool_id()

        self.assertIsNone(result)

    @patch("requests.get")
    def test_returns_none_when_spool_id_is_null(self, mock_get: MagicMock) -> None:
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"result": {"spool_id": None}}
        mock_resp.raise_for_status = lambda: None
        mock_get.return_value = mock_resp

        result = _fetch_active_spool_id()

        self.assertIsNone(result)

    @patch("requests.get")
    def test_returns_fetch_error_on_connection_error(self, mock_get: MagicMock) -> None:
        import requests as req
        mock_get.side_effect = req.ConnectionError("refused")

        result = _fetch_active_spool_id()

        # Must return the sentinel, not raise — poll loop must keep running
        self.assertIs(result, toolhead_status._FETCH_ERROR)

    @patch("requests.get")
    def test_returns_fetch_error_on_timeout(self, mock_get: MagicMock) -> None:
        import requests as req
        mock_get.side_effect = req.Timeout()

        result = _fetch_active_spool_id()

        self.assertIs(result, toolhead_status._FETCH_ERROR)

    @patch("requests.get")
    def test_returns_fetch_error_on_http_error(self, mock_get: MagicMock) -> None:
        import requests as req
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_get.side_effect = req.HTTPError(response=mock_resp)

        result = _fetch_active_spool_id()

        self.assertIs(result, toolhead_status._FETCH_ERROR)

    def test_returns_fetch_error_when_no_moonraker_url(self) -> None:
        # Empty URL means Moonraker is not configured — treat as fetch failure,
        # not as "no spool", so we do not inadvertently clear locks
        app_state.cfg["moonraker_url"] = ""

        result = _fetch_active_spool_id()

        self.assertIs(result, toolhead_status._FETCH_ERROR)

    @patch("requests.get")
    def test_returns_fetch_error_on_unexpected_exception(self, mock_get: MagicMock) -> None:
        mock_get.side_effect = RuntimeError("boom")

        result = _fetch_active_spool_id()

        self.assertIs(result, toolhead_status._FETCH_ERROR)


class TestCheckTransition(unittest.TestCase):
    """Tests for ToolheadStatusSync._check_transition — the eject/assign state machine."""

    def setUp(self) -> None:
        _reset_app_state()

    def _make_sync(self, last_spool_id: int | None = None) -> ToolheadStatusSync:
        sync = ToolheadStatusSync()
        sync._last_spool_id = last_spool_id
        return sync

    @patch("toolhead_status.publish_lock")
    def test_detects_spool_change_clears_toolhead_lock(self, mock_publish_lock: MagicMock) -> None:
        # Transition from spool 5 → None should clear whichever toolhead held spool 5
        app_state.active_spools["T0"] = 5
        sync = self._make_sync(last_spool_id=5)

        sync._check_transition(None)

        mock_publish_lock.assert_called_once_with("T0", "clear")
        self.assertIsNone(app_state.active_spools["T0"])

    @patch("toolhead_status.publish_lock")
    def test_ignores_same_spool_id(self, mock_publish_lock: MagicMock) -> None:
        # Same ID seen on consecutive polls — no state change, no lock action
        app_state.active_spools["T0"] = 7
        sync = self._make_sync(last_spool_id=7)

        sync._check_transition(7)

        mock_publish_lock.assert_not_called()

    @patch("toolhead_status.publish_lock")
    def test_handles_no_previous_spool(self, mock_publish_lock: MagicMock) -> None:
        # First poll after startup — no previous state, new spool assigned externally.
        # Should track but not clear any locks.
        sync = self._make_sync(last_spool_id=None)

        sync._check_transition(12)

        mock_publish_lock.assert_not_called()
        self.assertEqual(sync._last_spool_id, 12)

    @patch("toolhead_status.publish_lock")
    def test_global_spool_change_multi_toolhead_does_not_clear_locks(self, mock_publish_lock: MagicMock) -> None:
        # Multi-toolhead config: spool changed from one ID to another (e.g., a
        # different tool activated in Mainsail). Ambiguous — could be a different
        # tool getting assigned. Do not touch any locks.
        app_state.cfg["scanners"] = {
            "scanner_t0": {"action": "toolhead", "toolhead": "T0"},
            "scanner_t1": {"action": "toolhead", "toolhead": "T1"},
        }
        app_state.active_spools["T0"] = 3
        app_state.lane_locks["T0"] = True
        sync = self._make_sync(last_spool_id=3)

        sync._check_transition(9)

        mock_publish_lock.assert_not_called()

    @patch("toolhead_status.publish_lock")
    def test_global_spool_change_single_toolhead_clears_lock(self, mock_publish_lock: MagicMock) -> None:
        # Single-toolhead config: spool swap (e.g., Mainsail UI change) is
        # authoritative — there's only one slot, so clear the lock and update
        # active_spools so the next scan can go through (#76).
        app_state.cfg["scanners"] = {
            "scanner_t0": {"action": "toolhead", "toolhead": "T0"},
        }
        app_state.active_spools["T0"] = 3
        app_state.lane_locks["T0"] = True
        sync = self._make_sync(last_spool_id=3)

        sync._check_transition(9)

        mock_publish_lock.assert_called_once_with("T0", "clear")
        self.assertEqual(app_state.active_spools["T0"], 9)

    @patch("toolhead_status.publish_lock")
    def test_global_spool_change_single_toolhead_no_lock_to_clear(self, mock_publish_lock: MagicMock) -> None:
        # Single-toolhead, but the toolhead isn't locked — transition does not
        # try to clear a lock that isn't there, BUT it still updates tracked
        # active_spools so consecutive Mainsail swaps stay consistent.
        app_state.cfg["scanners"] = {
            "scanner_t0": {"action": "toolhead", "toolhead": "T0"},
        }
        app_state.active_spools["T0"] = 3
        app_state.lane_locks["T0"] = False
        sync = self._make_sync(last_spool_id=3)

        sync._check_transition(9)

        mock_publish_lock.assert_not_called()
        self.assertEqual(app_state.active_spools["T0"], 9)

    @patch("toolhead_status.publish_lock")
    def test_eject_with_no_matching_toolhead_clears_lane_locks(self, mock_publish_lock: MagicMock) -> None:
        # Ejected spool ID is not tracked in active_spools — fall back to clearing
        # any locked toolhead scanners from the config
        app_state.active_spools = {}
        app_state.cfg["scanners"] = {
            "scanner_t0": {"action": "toolhead", "toolhead": "T0"},
        }
        app_state.lane_locks["T0"] = True
        sync = self._make_sync(last_spool_id=99)

        sync._check_transition(None)

        mock_publish_lock.assert_called_once_with("T0", "clear")

    @patch("toolhead_status.publish_lock")
    def test_eject_fallback_skips_unlocked_toolheads(self, mock_publish_lock: MagicMock) -> None:
        # Only toolheads with an active lock should be cleared during fallback
        app_state.active_spools = {}
        app_state.cfg["scanners"] = {
            "scanner_t0": {"action": "toolhead", "toolhead": "T0"},
        }
        app_state.lane_locks["T0"] = False  # not locked
        sync = self._make_sync(last_spool_id=99)

        sync._check_transition(None)

        mock_publish_lock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
