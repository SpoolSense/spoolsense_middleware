"""
Tests for backoff.py and the retry loops that use it (#128).

After a long printer outage the failure counter passes 1024 and the old
`2 ** (failures - 1)` math raised OverflowError, which killed the thread for
good. Each loop test drives the real loop past that point with a fake stop
event, so no thread or real sleep is needed.
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

import afc_status  # noqa: E402
import app_state  # noqa: E402
import filament_usage  # noqa: E402
import moonraker_ws  # noqa: E402
import toolchanger_status  # noqa: E402
import toolhead_status  # noqa: E402
from backoff import backoff_delay  # noqa: E402

# The old math first overflows at failure 1025; go well past it.
OUTAGE_ITERATIONS: int = 1100


class _FakeStopEvent:
    """Stands in for threading.Event: records each wait and stops after N."""

    def __init__(self, max_waits: int) -> None:
        self.waits: list[float] = []
        self._max_waits = max_waits
        self._set = False

    def is_set(self) -> bool:
        return self._set

    def set(self) -> None:
        self._set = True

    def wait(self, timeout: float | None = None) -> bool:
        self.waits.append(timeout)
        if len(self.waits) >= self._max_waits:
            self._set = True
        return self._set


def _reset_app_state() -> None:
    app_state.cfg = {"moonraker_url": "http://moonraker:7125", "scanners": {}}
    app_state.state_lock = threading.Lock()


class TestBackoffDelay(unittest.TestCase):

    def test_first_failure_waits_base(self) -> None:
        self.assertEqual(backoff_delay(1, 2.0, 30.0), 2.0)

    def test_second_failure_doubles(self) -> None:
        self.assertEqual(backoff_delay(2, 2.0, 30.0), 4.0)

    def test_same_delays_as_before_up_to_cap(self) -> None:
        for failures in range(1, 40):
            old = min(2.0 * (2 ** (failures - 1)), 30.0)
            self.assertEqual(backoff_delay(failures, 2.0, 30.0), old, failures)

    def test_ten_failures_capped(self) -> None:
        self.assertEqual(backoff_delay(10, 2.0, 30.0), 30.0)

    def test_1024_failures_capped(self) -> None:
        self.assertEqual(backoff_delay(1024, 2.0, 30.0), 30.0)

    def test_overflow_point_capped(self) -> None:
        self.assertEqual(backoff_delay(1025, 2.0, 30.0), 30.0)

    def test_million_failures_capped(self) -> None:
        self.assertEqual(backoff_delay(10 ** 6, 2.0, 30.0), 30.0)

    def test_zero_or_negative_failures_waits_base(self) -> None:
        self.assertEqual(backoff_delay(0, 2.0, 30.0), 2.0)
        self.assertEqual(backoff_delay(-5, 2.0, 30.0), 2.0)

    def test_large_cap_still_reached(self) -> None:
        # The exponent clamp must not stop the delay short of a big cap.
        self.assertEqual(backoff_delay(10 ** 6, 1.0, 3600.0), 3600.0)


class TestPollLoopsSurviveLongOutage(unittest.TestCase):
    """Each loop keeps running past the old overflow point, waits capped."""

    def setUp(self) -> None:
        _reset_app_state()

    def _assert_capped(self, stop: _FakeStopEvent, retry_max: float) -> None:
        self.assertEqual(len(stop.waits), OUTAGE_ITERATIONS)
        self.assertTrue(all(0 < w <= retry_max for w in stop.waits))
        self.assertEqual(stop.waits[-1], retry_max)

    def test_toolhead_status(self) -> None:
        sync = toolhead_status.ToolheadStatusSync()
        sync._stop_event = _FakeStopEvent(OUTAGE_ITERATIONS)
        with patch.object(toolhead_status, "_fetch_active_spool_id",
                          return_value=toolhead_status._FETCH_ERROR):
            sync._poll_loop()
        self._assert_capped(sync._stop_event, toolhead_status.RETRY_MAX)

    def test_afc_status(self) -> None:
        sync = afc_status.AfcStatusSync()
        sync._stop_event = _FakeStopEvent(OUTAGE_ITERATIONS)
        with patch.object(afc_status, "_fetch_afc_status", return_value=None):
            sync._poll_loop()
        self._assert_capped(sync._stop_event, afc_status.RETRY_MAX)

    def test_filament_usage(self) -> None:
        sync = filament_usage.FilamentUsageSync()
        sync._stop_event = _FakeStopEvent(OUTAGE_ITERATIONS)
        with patch.object(filament_usage, "_fetch_pending", return_value=None):
            sync._poll_loop()
        self._assert_capped(sync._stop_event, filament_usage.RETRY_MAX)

    def test_toolchanger_status(self) -> None:
        sync = toolchanger_status.ToolchangerStatusSync()
        sync._stop_event = _FakeStopEvent(OUTAGE_ITERATIONS)
        with patch.object(toolchanger_status, "_fetch_pending_tool", return_value=None):
            sync._poll_loop()
        self._assert_capped(sync._stop_event, toolchanger_status.RETRY_MAX)

    def test_moonraker_ws(self) -> None:
        ws = moonraker_ws.MoonrakerWebsocket("ws://moonraker:7125/websocket")
        ws._stop_event = _FakeStopEvent(OUTAGE_ITERATIONS)
        fake_websocket = MagicMock()
        with patch.object(moonraker_ws, "websocket", fake_websocket, create=True):
            ws._run_loop()
        self._assert_capped(ws._stop_event, moonraker_ws.RETRY_MAX)
        self.assertEqual(ws._consecutive_failures, OUTAGE_ITERATIONS)


class TestPollLoopsSurviveUnexpectedError(unittest.TestCase):
    """One iteration that raises must not kill the thread."""

    def setUp(self) -> None:
        _reset_app_state()

    def _run(self, sync, module, fetch_name: str, ok_value) -> _FakeStopEvent:
        sync._stop_event = _FakeStopEvent(3)
        fetch = MagicMock(side_effect=[RuntimeError("boom"), ok_value, ok_value])
        with patch.object(module, fetch_name, fetch):
            sync._poll_loop()
        self.assertEqual(fetch.call_count, 3)
        return sync._stop_event

    def test_toolhead_status(self) -> None:
        sync = toolhead_status.ToolheadStatusSync()
        stop = self._run(sync, toolhead_status, "_fetch_active_spool_id", None)
        self.assertEqual(stop.waits[1:], [toolhead_status.POLL_INTERVAL] * 2)

    def test_afc_status(self) -> None:
        sync = afc_status.AfcStatusSync()
        with patch.object(afc_status, "_sync_lane_state"):
            stop = self._run(sync, afc_status, "_fetch_afc_status", {})
        self.assertEqual(stop.waits[1:], [afc_status.POLL_INTERVAL] * 2)

    def test_filament_usage(self) -> None:
        sync = filament_usage.FilamentUsageSync()
        stop = self._run(sync, filament_usage, "_fetch_pending", 0)
        self.assertEqual(stop.waits[1:], [filament_usage.POLL_INTERVAL] * 2)

    def test_toolchanger_status(self) -> None:
        sync = toolchanger_status.ToolchangerStatusSync()
        stop = self._run(sync, toolchanger_status, "_fetch_pending_tool", "")
        self.assertEqual(stop.waits[1:], [toolchanger_status.POLL_INTERVAL] * 2)

    def test_toolchanger_status_process_error(self) -> None:
        # The assign path ran unguarded inside the loop before #128.
        sync = toolchanger_status.ToolchangerStatusSync()
        sync._stop_event = _FakeStopEvent(2)
        with patch.object(toolchanger_status, "_fetch_pending_tool", return_value="T0"), \
                patch.object(toolchanger_status, "_process_pending_tool",
                             side_effect=RuntimeError("boom")) as process, \
                patch.object(toolchanger_status, "_clear_pending_tool") as clear:
            sync._poll_loop()
        self.assertEqual(process.call_count, 2)
        # The macro request is cleared even when the assign raised.
        self.assertEqual(clear.call_count, 2)
        self.assertEqual(sync._stop_event.waits, [toolchanger_status.POLL_INTERVAL] * 2)


class TestAssignErrorRestoresPendingSpool(unittest.TestCase):
    """An assign that raises is a failed assign: the staged spool comes back."""

    def setUp(self) -> None:
        _reset_app_state()
        self.pending = {"spoolman_id": 7, "uid": "04AABB"}
        app_state.pending_spool_toolhead = self.pending

    def tearDown(self) -> None:
        app_state.pending_spool_toolhead = None

    def test_pending_spool_restored(self) -> None:
        with patch.object(toolchanger_status, "_assign_spool_to_tool",
                          side_effect=RuntimeError("boom")):
            toolchanger_status._process_pending_tool("T0", "Macro assign")
        self.assertIs(app_state.pending_spool_toolhead, self.pending)

    def test_newer_scan_kept(self) -> None:
        newer = {"spoolman_id": 8, "uid": "04CCDD"}

        def scan_then_raise(tool_name: str, pending: dict) -> bool:
            app_state.pending_spool_toolhead = newer
            raise RuntimeError("boom")

        with patch.object(toolchanger_status, "_assign_spool_to_tool",
                          side_effect=scan_then_raise):
            toolchanger_status._process_pending_tool("T0", "Macro assign")
        self.assertIs(app_state.pending_spool_toolhead, newer)

    def test_poll_loop_keeps_spool_on_assign_error(self) -> None:
        sync = toolchanger_status.ToolchangerStatusSync()
        sync._stop_event = _FakeStopEvent(1)
        with patch.object(toolchanger_status, "_fetch_pending_tool", return_value="T0"), \
                patch.object(toolchanger_status, "_assign_spool_to_tool",
                             side_effect=RuntimeError("boom")), \
                patch.object(toolchanger_status, "_clear_pending_tool") as clear:
            sync._poll_loop()
        self.assertIs(app_state.pending_spool_toolhead, self.pending)
        clear.assert_called_once()


if __name__ == "__main__":
    unittest.main()
