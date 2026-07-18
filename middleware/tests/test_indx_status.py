"""Tests for indx_status.py — INDX active_tool → Spoolman sync."""
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

import app_state  # noqa: E402
import indx_status  # noqa: E402
from indx_status import on_active_tool  # noqa: E402


def _reset(active_tool_sync: bool = True) -> None:
    app_state.cfg = {
        "moonraker_url": "http://moon",
        "toolheads": ["T0", "T1"],
        "active_tool_sync": active_tool_sync,
    }
    app_state.active_spools = {}
    app_state.state_lock = threading.Lock()
    app_state.indx_active_tool = None
    indx_status._last_observed = None
    indx_status._pending_sync = None
    indx_status._worker_running = False


class _SyncThread:
    """threading.Thread stand-in that runs the worker synchronously."""

    def __init__(self, target, **kwargs) -> None:
        self._target = target

    def start(self) -> None:
        self._target()


class TestOnActiveTool(unittest.TestCase):

    def setUp(self) -> None:
        _reset()
        self._thread_patch = patch.object(
            indx_status.threading, "Thread", _SyncThread)
        self._thread_patch.start()

    def tearDown(self) -> None:
        self._thread_patch.stop()

    def test_pickup_sets_state_and_pushes_bound_spool(self) -> None:
        with patch.object(indx_status, "set_active_spool_id") as post:
            on_active_tool({"active_tool": 1, "t1_spool_id": 42})
        self.assertEqual(app_state.indx_active_tool, 1)
        post.assert_called_once_with("http://moon", 42)

    def test_park_updates_state_without_spoolman_call(self) -> None:
        with patch.object(indx_status, "set_active_spool_id") as post:
            on_active_tool({"active_tool": 2, "t2_spool_id": 7})
            post.reset_mock()
            on_active_tool({"active_tool": -1})
            self.assertIsNone(app_state.indx_active_tool)
            post.assert_not_called()

    def test_resend_of_same_pair_is_ignored(self) -> None:
        with patch.object(indx_status, "set_active_spool_id") as post:
            on_active_tool({"active_tool": 0, "t0_spool_id": 5})
            on_active_tool({"active_tool": 0, "t0_spool_id": 5})
        self.assertEqual(post.call_count, 1)

    def test_rebind_of_mounted_tool_triggers_sync(self) -> None:
        with patch.object(indx_status, "set_active_spool_id") as post:
            on_active_tool({"active_tool": 0, "t0_spool_id": 5})
            on_active_tool({"active_tool": 0, "t0_spool_id": 9})
        self.assertEqual(post.call_count, 2)
        post.assert_called_with("http://moon", 9)

    def test_repickup_after_park_reposts(self) -> None:
        # Other components also write Spoolman's active spool — a genuine
        # pickup must always POST even if the pair matches the last mount.
        with patch.object(indx_status, "set_active_spool_id") as post:
            on_active_tool({"active_tool": 0, "t0_spool_id": 5})
            on_active_tool({"active_tool": -1})
            on_active_tool({"active_tool": 0, "t0_spool_id": 5})
        self.assertEqual(post.call_count, 2)

    def test_unbound_tool_leaves_spoolman_alone(self) -> None:
        with patch.object(indx_status, "set_active_spool_id") as post:
            on_active_tool({"active_tool": 1})
        self.assertEqual(app_state.indx_active_tool, 1)
        post.assert_not_called()

    def test_falls_back_to_active_spools_binding(self) -> None:
        app_state.active_spools["T3"] = 99
        with patch.object(indx_status, "set_active_spool_id") as post:
            on_active_tool({"active_tool": 3})
        post.assert_called_once_with("http://moon", 99)

    def test_sync_disabled_still_tracks_tool(self) -> None:
        _reset(active_tool_sync=False)
        with patch.object(indx_status, "set_active_spool_id") as post:
            on_active_tool({"active_tool": 1, "t1_spool_id": 42})
        self.assertEqual(app_state.indx_active_tool, 1)
        post.assert_not_called()

    def test_spoolman_failure_logged_and_next_pickup_retries(self) -> None:
        with patch.object(indx_status, "set_active_spool_id",
                          side_effect=RuntimeError("boom")) as post:
            with self.assertLogs("indx_status", level="ERROR") as logs:
                on_active_tool({"active_tool": 1, "t1_spool_id": 42})
        self.assertIn("failed to set Spoolman active spool", logs.output[0])
        # a later genuine transition retries
        with patch.object(indx_status, "set_active_spool_id") as post:
            on_active_tool({"active_tool": -1})
            on_active_tool({"active_tool": 1, "t1_spool_id": 42})
        post.assert_called_once_with("http://moon", 42)

    def test_latest_wins_when_worker_backlogged(self) -> None:
        # Simulate transitions arriving while the worker has not run yet:
        # only the newest target must be applied.
        with patch.object(indx_status.threading, "Thread") as never_runs:
            never_runs.return_value = MagicMock()  # start() does nothing
            on_active_tool({"active_tool": 0, "t0_spool_id": 5})
            on_active_tool({"active_tool": -1})
            on_active_tool({"active_tool": 1, "t1_spool_id": 8})
        with patch.object(indx_status, "set_active_spool_id") as post:
            indx_status._sync_worker()
        post.assert_called_once_with("http://moon", 8)

    def test_garbage_active_tool_ignored(self) -> None:
        with patch.object(indx_status, "set_active_spool_id") as post:
            on_active_tool({"active_tool": "banana"})
        self.assertIsNone(app_state.indx_active_tool)
        post.assert_not_called()

    def test_zero_spool_id_treated_as_unbound(self) -> None:
        with patch.object(indx_status, "set_active_spool_id") as post:
            on_active_tool({"active_tool": 1, "t1_spool_id": 0})
        post.assert_not_called()


class TestSaveVariablesWiring(unittest.TestCase):
    """The save_variables callback must be wired for toolhead_stage (INDX)
    configs, not only direct-toolhead ones (codex High finding)."""

    def test_stage_only_config_wires_save_variables(self) -> None:
        import config as config_mod
        cfg = {"scanners": {"abc123": {"action": "toolhead_stage"}}}
        self.assertFalse(config_mod.has_toolhead_scanners(cfg))
        self.assertTrue(config_mod.has_toolhead_stage_scanners(cfg))
        src = open(os.path.join(os.path.dirname(os.path.dirname(__file__)),
                                "spoolsense.py")).read()
        self.assertIn(
            "has_toolhead_scanners(cfg) or has_toolhead_stage_scanners(cfg)",
            src,
            "save_variables wiring must cover toolhead_stage configs",
        )


if __name__ == "__main__":
    unittest.main()
