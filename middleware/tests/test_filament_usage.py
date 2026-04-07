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
from filament_usage import (  # noqa: E402
    _fetch_last_job_weights,
    _fetch_afc_lane_weights,
    _handle_update_tag,
    _handle_toolchanger,
    _handle_afc,
)


def _reset_app_state():
    app_state.cfg = {
        "moonraker_url": "http://moonraker:7125",
        "scanner_topic_prefix": "spoolsense",
    }
    app_state.state_lock = threading.Lock()
    app_state.active_spool_weights = {}
    app_state.active_spool_uids = {}
    app_state.active_spool_devices = {}
    app_state.mqtt_client = MagicMock()


class TestFetchLastJobWeights(unittest.TestCase):

    def setUp(self):
        _reset_app_state()

    @patch("requests.get")
    def test_returns_weights_from_completed_job(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "result": {
                "count": 1,
                "jobs": [
                    {
                        "status": "completed",
                        "metadata": {"filament_weights": [50.0, 0.0, 30.0, 0.0]},
                    }
                ],
            }
        }
        mock_resp.raise_for_status = lambda: None
        mock_get.return_value = mock_resp

        weights = _fetch_last_job_weights()
        self.assertEqual(weights, [50.0, 0.0, 30.0, 0.0])

    @patch("requests.get")
    def test_returns_none_for_non_completed_job(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "result": {
                "jobs": [{"status": "cancelled", "metadata": {"filament_weights": [10.0]}}]
            }
        }
        mock_resp.raise_for_status = lambda: None
        mock_get.return_value = mock_resp

        self.assertIsNone(_fetch_last_job_weights())

    @patch("requests.get")
    def test_returns_none_for_empty_jobs(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"result": {"count": 0, "jobs": []}}
        mock_resp.raise_for_status = lambda: None
        mock_get.return_value = mock_resp

        self.assertIsNone(_fetch_last_job_weights())

    def test_returns_none_when_no_moonraker_url(self):
        app_state.cfg["moonraker_url"] = ""
        self.assertIsNone(_fetch_last_job_weights())


class TestFetchAfcLaneWeights(unittest.TestCase):

    def setUp(self):
        _reset_app_state()

    @patch("requests.get")
    def test_returns_lane_weights(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "result": {
                "status:": {
                    "AFC": {
                        "Turtle_1": {
                            "lane1": {"weight": 550.0, "status": "Loaded"},
                            "lane2": {"weight": 720.0, "status": "Loaded"},
                            "system": {"some": "data"},
                        }
                    }
                }
            }
        }
        mock_resp.raise_for_status = lambda: None
        mock_get.return_value = mock_resp

        weights = _fetch_afc_lane_weights()
        self.assertEqual(weights, {"lane1": 550.0, "lane2": 720.0})

    @patch("requests.get")
    def test_skips_system_and_tools_keys(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "result": {
                "status:": {
                    "AFC": {
                        "system": {"top": "level"},
                        "Tools": {"T0": "data"},
                        "Turtle_1": {
                            "lane1": {"weight": 100.0},
                        },
                    }
                }
            }
        }
        mock_resp.raise_for_status = lambda: None
        mock_get.return_value = mock_resp

        weights = _fetch_afc_lane_weights()
        self.assertEqual(weights, {"lane1": 100.0})


class TestHandleToolchanger(unittest.TestCase):

    def setUp(self):
        _reset_app_state()

    @patch("filament_usage._clear_pending")
    @patch("filament_usage._fetch_last_job_weights")
    def test_sends_deduction_for_single_tool(self, mock_fetch, mock_clear):
        mock_fetch.return_value = [25.5]
        app_state.active_spool_uids["T0"] = "abc123"
        app_state.active_spool_devices["T0"] = "f3d360"

        _handle_toolchanger()

        app_state.mqtt_client.publish.assert_called_once()
        call_args = app_state.mqtt_client.publish.call_args
        self.assertEqual(call_args[0][0], "spoolsense/f3d360/cmd/deduct/abc123")

    @patch("filament_usage._clear_pending")
    @patch("filament_usage._fetch_last_job_weights")
    def test_sends_per_tool_deductions(self, mock_fetch, mock_clear):
        mock_fetch.return_value = [50.0, 0.0, 30.0, 0.0]
        app_state.active_spool_uids["T0"] = "uid-aaa"
        app_state.active_spool_devices["T0"] = "scanner1"
        app_state.active_spool_uids["T2"] = "uid-bbb"
        app_state.active_spool_devices["T2"] = "scanner1"

        _handle_toolchanger()

        self.assertEqual(app_state.mqtt_client.publish.call_count, 2)
        topics = [c[0][0] for c in app_state.mqtt_client.publish.call_args_list]
        self.assertIn("spoolsense/scanner1/cmd/deduct/uid-aaa", topics)
        self.assertIn("spoolsense/scanner1/cmd/deduct/uid-bbb", topics)

    @patch("filament_usage._clear_pending")
    @patch("filament_usage._fetch_last_job_weights")
    def test_skips_tools_with_zero_usage(self, mock_fetch, mock_clear):
        mock_fetch.return_value = [0.0, 0.0, 0.0, 0.0]
        app_state.active_spool_uids["T0"] = "uid-aaa"
        app_state.active_spool_devices["T0"] = "scanner1"

        _handle_toolchanger()

        app_state.mqtt_client.publish.assert_not_called()

    @patch("filament_usage._clear_pending")
    @patch("filament_usage._fetch_last_job_weights")
    def test_skips_tool_without_active_spool(self, mock_fetch, mock_clear):
        mock_fetch.return_value = [25.0]
        # No active spool on T0

        _handle_toolchanger()

        app_state.mqtt_client.publish.assert_not_called()

    @patch("filament_usage._clear_pending")
    @patch("filament_usage._fetch_last_job_weights")
    def test_no_completed_job_does_nothing(self, mock_fetch, mock_clear):
        mock_fetch.return_value = None

        _handle_toolchanger()

        app_state.mqtt_client.publish.assert_not_called()


class TestHandleAfc(unittest.TestCase):

    def setUp(self):
        _reset_app_state()

    @patch("filament_usage._clear_pending")
    @patch("filament_usage._fetch_afc_lane_weights")
    def test_sends_deduction_from_weight_delta(self, mock_fetch, mock_clear):
        mock_fetch.return_value = {"lane1": 550.0, "lane2": 720.0}
        app_state.active_spool_weights = {"lane1": 800.0, "lane2": 750.0}
        app_state.active_spool_uids = {"lane1": "uid-aaa", "lane2": "uid-bbb"}
        app_state.active_spool_devices = {"lane1": "scanner1", "lane2": "scanner1"}

        _handle_afc()

        self.assertEqual(app_state.mqtt_client.publish.call_count, 2)
        topics = [c[0][0] for c in app_state.mqtt_client.publish.call_args_list]
        self.assertIn("spoolsense/scanner1/cmd/deduct/uid-aaa", topics)
        self.assertIn("spoolsense/scanner1/cmd/deduct/uid-bbb", topics)

    @patch("filament_usage._clear_pending")
    @patch("filament_usage._fetch_afc_lane_weights")
    def test_updates_initial_weight_after_deduction(self, mock_fetch, mock_clear):
        mock_fetch.return_value = {"lane1": 550.0}
        app_state.active_spool_weights = {"lane1": 800.0}
        app_state.active_spool_uids = {"lane1": "uid-aaa"}
        app_state.active_spool_devices = {"lane1": "scanner1"}

        _handle_afc()

        self.assertEqual(app_state.active_spool_weights["lane1"], 550.0)

    @patch("filament_usage._clear_pending")
    @patch("filament_usage._fetch_afc_lane_weights")
    def test_skips_lanes_with_no_usage(self, mock_fetch, mock_clear):
        mock_fetch.return_value = {"lane1": 800.0}  # same as initial
        app_state.active_spool_weights = {"lane1": 800.0}
        app_state.active_spool_uids = {"lane1": "uid-aaa"}
        app_state.active_spool_devices = {"lane1": "scanner1"}

        _handle_afc()

        app_state.mqtt_client.publish.assert_not_called()

    @patch("filament_usage._clear_pending")
    @patch("filament_usage._fetch_afc_lane_weights")
    def test_skips_lanes_without_initial_weight(self, mock_fetch, mock_clear):
        mock_fetch.return_value = {"lane1": 550.0}
        # No initial weight recorded
        app_state.active_spool_weights = {}

        _handle_afc()

        app_state.mqtt_client.publish.assert_not_called()


if __name__ == "__main__":
    unittest.main()
