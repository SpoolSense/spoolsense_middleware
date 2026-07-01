"""
Tests for happy_hare.py — MMU gate binding via Spoolman PATCH + sync trigger.
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
import happy_hare  # noqa: E402
from happy_hare import bind_spool_to_current_gate  # noqa: E402


def _reset(*, enabled=True, printer_name="muffin"):
    app_state.cfg = {
        "moonraker_url": "http://moonraker:7125",
        "happy_hare": {"enabled": enabled, "printer_name": printer_name},
        "scanners": {},
    }
    app_state.state_lock = threading.Lock()
    app_state.spoolman_client = MagicMock()
    app_state.spoolman_client.update_spool_extras = MagicMock(return_value=True)
    happy_hare._reset_mode_cache_for_testing()


def _mmu_status(**overrides):
    base = {
        "enabled": True,
        "spoolman_support": "pull",
        "gate": 4,
        "num_gates": 8,
    }
    base.update(overrides)
    return base


def _mock_response(payload):
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value={"result": {"status": {"mmu": payload}}})
    return resp


class TestBindHappyPath(unittest.TestCase):
    """Successful end-to-end bind path."""

    def setUp(self):
        _reset()

    def test_bind_patches_spoolman_with_gate_and_printer_name(self):
        with patch("happy_hare.requests.get", return_value=_mock_response(_mmu_status())), \
             patch("happy_hare._send_gcode"):
            result = bind_spool_to_current_gate(spool_id=42)
        assert result is True
        app_state.spoolman_client.update_spool_extras.assert_called_once_with(
            42, {"mmu_gate": 4, "printer_name": "muffin"}
        )

    def test_bind_fires_mmu_spoolman_sync(self):
        with patch("happy_hare.requests.get", return_value=_mock_response(_mmu_status())), \
             patch("happy_hare._send_gcode") as mock_gcode:
            bind_spool_to_current_gate(spool_id=42)
        mock_gcode.assert_called_once_with("http://moonraker:7125", "MMU_SPOOLMAN SYNC=1")

    def test_bind_sync_failure_does_not_fail_overall_bind(self):
        # The PATCH already landed; a missing sync just means Happy Hare
        # picks it up on the next periodic pull. Still report success.
        with patch("happy_hare.requests.get", return_value=_mock_response(_mmu_status())), \
             patch("happy_hare._send_gcode", side_effect=Exception("boom")):
            result = bind_spool_to_current_gate(spool_id=42)
        assert result is True


class TestBindGuards(unittest.TestCase):
    """Refusal paths — each should return False without writing to Spoolman."""

    def setUp(self):
        _reset()

    def _assert_no_patch_called(self):
        app_state.spoolman_client.update_spool_extras.assert_not_called()

    def test_skipped_when_integration_disabled(self):
        _reset(enabled=False)
        result = bind_spool_to_current_gate(spool_id=42)
        assert result is False
        self._assert_no_patch_called()

    def test_skipped_when_printer_name_empty(self):
        _reset(printer_name="")
        with patch("happy_hare.requests.get", return_value=_mock_response(_mmu_status())):
            result = bind_spool_to_current_gate(spool_id=42)
        assert result is False
        self._assert_no_patch_called()

    def test_skipped_when_moonraker_unreachable(self):
        import requests
        with patch("happy_hare.requests.get", side_effect=requests.ConnectionError("refused")):
            result = bind_spool_to_current_gate(spool_id=42)
        assert result is False
        self._assert_no_patch_called()

    def test_refused_when_spoolman_support_is_push(self):
        with patch("happy_hare.requests.get",
                   return_value=_mock_response(_mmu_status(spoolman_support="push"))):
            result = bind_spool_to_current_gate(spool_id=42)
        assert result is False
        self._assert_no_patch_called()

    def test_refused_when_spoolman_support_is_readonly(self):
        with patch("happy_hare.requests.get",
                   return_value=_mock_response(_mmu_status(spoolman_support="readonly"))):
            result = bind_spool_to_current_gate(spool_id=42)
        assert result is False
        self._assert_no_patch_called()

    def test_skipped_when_mmu_disabled(self):
        with patch("happy_hare.requests.get",
                   return_value=_mock_response(_mmu_status(enabled=False))):
            result = bind_spool_to_current_gate(spool_id=42)
        assert result is False
        self._assert_no_patch_called()

    def test_skipped_when_no_gate_selected(self):
        # Happy Hare reports gate=-1 (or any non-int) when no gate is selected
        with patch("happy_hare.requests.get",
                   return_value=_mock_response(_mmu_status(gate=-1))):
            result = bind_spool_to_current_gate(spool_id=42)
        assert result is False
        self._assert_no_patch_called()

    def test_skipped_when_gate_out_of_range(self):
        with patch("happy_hare.requests.get",
                   return_value=_mock_response(_mmu_status(gate=10, num_gates=8))):
            result = bind_spool_to_current_gate(spool_id=42)
        assert result is False
        self._assert_no_patch_called()

    def test_skipped_when_spoolman_client_missing(self):
        app_state.spoolman_client = None
        with patch("happy_hare.requests.get", return_value=_mock_response(_mmu_status())):
            result = bind_spool_to_current_gate(spool_id=42)
        assert result is False

    def test_returns_false_when_spoolman_patch_fails(self):
        app_state.spoolman_client.update_spool_extras = MagicMock(return_value=False)
        with patch("happy_hare.requests.get", return_value=_mock_response(_mmu_status())), \
             patch("happy_hare._send_gcode"):
            result = bind_spool_to_current_gate(spool_id=42)
        assert result is False


class TestModeCheckCaching(unittest.TestCase):
    """Mode check should cache success but re-fetch on mismatch, and log
    the mismatch error exactly once per wrong-mode run."""

    def setUp(self):
        _reset()

    def test_mismatch_logs_exactly_once(self):
        # Two calls while mode is wrong — error logged on the first, suppressed
        # on the second. (Spamming logs every scan would be noisy.)
        with patch("happy_hare.requests.get",
                   return_value=_mock_response(_mmu_status(spoolman_support="push"))):
            with self.assertLogs("happy_hare", level="ERROR") as captured:
                # Need at least one ERROR for assertLogs to pass; we'll count after.
                bind_spool_to_current_gate(spool_id=1)
                bind_spool_to_current_gate(spool_id=2)
        mismatch_errors = [r for r in captured.records if "spoolman_support" in r.getMessage()]
        assert len(mismatch_errors) == 1, f"expected 1 mismatch log, got {len(mismatch_errors)}"

    def test_pull_mode_cached_after_first_success(self):
        # First call confirms pull → cached. Second call should not re-query Moonraker.
        with patch("happy_hare.requests.get",
                   return_value=_mock_response(_mmu_status())) as mock_get, \
             patch("happy_hare._send_gcode"):
            assert bind_spool_to_current_gate(spool_id=1) is True
            first_call_count = mock_get.call_count
            assert bind_spool_to_current_gate(spool_id=2) is True
            # We expect a second call (the bind path always fetches gate info)
            # but the mode-check shouldn't add an extra one — the assertion is
            # cleaner via the public flag.
        assert happy_hare._cached_pull_mode is True

    def test_mismatch_does_not_cache_allowing_recovery(self):
        # If Happy Hare's mode is wrong, we must not cache it permanently —
        # the user could fix mmu_parameters.cfg and we should recover without
        # restarting the middleware.
        with patch("happy_hare.requests.get",
                   return_value=_mock_response(_mmu_status(spoolman_support="push"))):
            bind_spool_to_current_gate(spool_id=1)
        assert happy_hare._cached_pull_mode is False

        # User flips Happy Hare to pull mode. Next bind should succeed.
        with patch("happy_hare.requests.get",
                   return_value=_mock_response(_mmu_status(spoolman_support="pull"))), \
             patch("happy_hare._send_gcode"):
            assert bind_spool_to_current_gate(spool_id=1) is True
        assert happy_hare._cached_pull_mode is True

    def test_missing_spoolman_support_field_does_not_cache(self):
        # If Moonraker returns the mmu object but the spoolman_support key is
        # missing (old Happy Hare, partial init, etc), we should NOT cache —
        # next call retries.
        bad_status = _mmu_status()
        del bad_status["spoolman_support"]
        with patch("happy_hare.requests.get",
                   return_value=_mock_response(bad_status)):
            assert bind_spool_to_current_gate(spool_id=1) is False
        assert happy_hare._cached_pull_mode is False


if __name__ == "__main__":
    unittest.main()
