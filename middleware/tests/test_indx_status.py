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
    indx_status._last_synced_spool = None


def _run_sync(variables: dict) -> None:
    """Call on_active_tool with the worker thread made synchronous."""
    with patch.object(indx_status.threading, "Thread",
                      side_effect=lambda target, **kw: MagicMock(start=target)):
        on_active_tool(variables)


class TestOnActiveTool(unittest.TestCase):

    def setUp(self):
        _reset()

    def test_pickup_sets_state_and_pushes_bound_spool(self):
        with patch.object(indx_status, "set_active_spool_id") as post:
            _run_sync({"active_tool": 1, "t1_spool_id": 42})
        self.assertEqual(app_state.indx_active_tool, 1)
        post.assert_called_once_with("http://moon", 42)
        self.assertEqual(indx_status._last_synced_spool, 42)

    def test_park_updates_state_without_spoolman_call(self):
        _run_sync({"active_tool": 2, "t2_spool_id": 7})
        with patch.object(indx_status, "set_active_spool_id") as post:
            on_active_tool({"active_tool": -1})
        self.assertIsNone(app_state.indx_active_tool)
        post.assert_not_called()

    def test_resend_of_same_tool_is_ignored(self):
        with patch.object(indx_status, "set_active_spool_id") as post:
            _run_sync({"active_tool": 0, "t0_spool_id": 5})
            _run_sync({"active_tool": 0, "t0_spool_id": 5})
        self.assertEqual(post.call_count, 1)

    def test_unbound_tool_leaves_spoolman_alone(self):
        with patch.object(indx_status, "set_active_spool_id") as post:
            _run_sync({"active_tool": 1})
        self.assertEqual(app_state.indx_active_tool, 1)
        post.assert_not_called()

    def test_falls_back_to_active_spools_binding(self):
        app_state.active_spools["T3"] = 99
        with patch.object(indx_status, "set_active_spool_id") as post:
            _run_sync({"active_tool": 3})
        post.assert_called_once_with("http://moon", 99)

    def test_sync_disabled_still_tracks_tool(self):
        _reset(active_tool_sync=False)
        with patch.object(indx_status, "set_active_spool_id") as post:
            _run_sync({"active_tool": 1, "t1_spool_id": 42})
        self.assertEqual(app_state.indx_active_tool, 1)
        post.assert_not_called()

    def test_spoolman_failure_logged_not_raised(self):
        with patch.object(indx_status, "set_active_spool_id",
                          side_effect=RuntimeError("boom")):
            _run_sync({"active_tool": 1, "t1_spool_id": 42})
        # failure must not mark the spool as synced
        self.assertIsNone(indx_status._last_synced_spool)

    def test_repeat_pickup_same_spool_skips_post(self):
        with patch.object(indx_status, "set_active_spool_id") as post:
            _run_sync({"active_tool": 0, "t0_spool_id": 5, "t1_spool_id": 5})
            on_active_tool({"active_tool": -1})
            _run_sync({"active_tool": 1, "t0_spool_id": 5, "t1_spool_id": 5})
        self.assertEqual(post.call_count, 1)  # same spool on both tools

    def test_garbage_active_tool_ignored(self):
        with patch.object(indx_status, "set_active_spool_id") as post:
            on_active_tool({"active_tool": "banana"})
        self.assertIsNone(app_state.indx_active_tool)
        post.assert_not_called()

    def test_zero_spool_id_treated_as_unbound(self):
        with patch.object(indx_status, "set_active_spool_id") as post:
            _run_sync({"active_tool": 1, "t1_spool_id": 0})
        post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
