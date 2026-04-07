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
    _fetch_tool_filament_used,
    _mm_to_grams,
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
    app_state.active_spool_diameters = {}
    app_state.active_spool_densities = {}
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
                        "filament_used": 5000.0,
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
    def test_returns_weights_from_cancelled_job_with_extrusion(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "result": {
                "jobs": [{
                    "status": "cancelled",
                    "filament_used": 3000.0,
                    "metadata": {"filament_weights": [10.0]},
                }]
            }
        }
        mock_resp.raise_for_status = lambda: None
        mock_get.return_value = mock_resp

        weights = _fetch_last_job_weights()
        self.assertEqual(weights, [10.0])

    @patch("requests.get")
    def test_returns_none_for_job_with_no_extrusion(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "result": {
                "jobs": [{
                    "status": "cancelled",
                    "filament_used": 0,
                    "metadata": {"filament_weights": [10.0]},
                }]
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


class TestHandleToolchangerPrimary(unittest.TestCase):
    """Tests for per-tool filament_used path (klipper-toolchanger mod installed)."""

    def setUp(self):
        _reset_app_state()

    @patch("filament_usage._clear_pending")
    @patch("filament_usage._fetch_tool_filament_used")
    def test_sends_deduction_from_tool_objects(self, mock_fetch_tool, mock_clear):
        mock_fetch_tool.return_value = {"T0": 5000.0}  # 5000mm
        app_state.active_spool_uids["T0"] = "abc123"
        app_state.active_spool_devices["T0"] = "f3d360"
        app_state.active_spool_diameters["T0"] = 1.75
        app_state.active_spool_densities["T0"] = 1.24

        _handle_toolchanger()

        app_state.mqtt_client.publish.assert_called_once()
        call_args = app_state.mqtt_client.publish.call_args
        self.assertEqual(call_args[0][0], "spoolsense/f3d360/cmd/deduct/abc123")

    @patch("filament_usage._clear_pending")
    @patch("filament_usage._fetch_tool_filament_used")
    def test_sends_per_tool_deductions_from_tool_objects(self, mock_fetch_tool, mock_clear):
        mock_fetch_tool.return_value = {"T0": 3000.0, "T2": 2000.0}
        app_state.active_spool_uids["T0"] = "uid-aaa"
        app_state.active_spool_devices["T0"] = "scanner1"
        app_state.active_spool_diameters["T0"] = 1.75
        app_state.active_spool_densities["T0"] = 1.24
        app_state.active_spool_uids["T2"] = "uid-bbb"
        app_state.active_spool_devices["T2"] = "scanner1"
        app_state.active_spool_diameters["T2"] = 1.75
        app_state.active_spool_densities["T2"] = 1.24

        _handle_toolchanger()

        self.assertEqual(app_state.mqtt_client.publish.call_count, 2)
        topics = [c[0][0] for c in app_state.mqtt_client.publish.call_args_list]
        self.assertIn("spoolsense/scanner1/cmd/deduct/uid-aaa", topics)
        self.assertIn("spoolsense/scanner1/cmd/deduct/uid-bbb", topics)

    @patch("filament_usage._clear_pending")
    @patch("filament_usage._fetch_tool_filament_used")
    def test_uses_tag_diameter_and_density(self, mock_fetch_tool, mock_clear):
        mock_fetch_tool.return_value = {"T0": 1000.0}  # 1000mm
        app_state.active_spool_uids["T0"] = "abc123"
        app_state.active_spool_devices["T0"] = "f3d360"
        app_state.active_spool_diameters["T0"] = 2.85
        app_state.active_spool_densities["T0"] = 1.27

        _handle_toolchanger()

        app_state.mqtt_client.publish.assert_called_once()
        # Verify the conversion used the correct values
        import json
        payload = json.loads(app_state.mqtt_client.publish.call_args[0][1])
        expected_g = _mm_to_grams(1000.0, 2.85, 1.27)
        self.assertAlmostEqual(payload["deduct_g"], round(expected_g, 2), places=2)

    @patch("filament_usage._clear_pending")
    @patch("filament_usage._fetch_tool_filament_used")
    def test_skips_tools_with_zero_usage(self, mock_fetch_tool, mock_clear):
        mock_fetch_tool.return_value = {"T0": 0.0, "T1": 0.0}
        app_state.active_spool_uids["T0"] = "uid-aaa"
        app_state.active_spool_devices["T0"] = "scanner1"

        _handle_toolchanger()

        app_state.mqtt_client.publish.assert_not_called()


class TestHandleToolchangerFallback(unittest.TestCase):
    """Tests for filament_weights fallback path (mod not installed)."""

    def setUp(self):
        _reset_app_state()

    @patch("filament_usage._clear_pending")
    @patch("filament_usage._fetch_last_job_weights")
    @patch("filament_usage._fetch_tool_filament_used")
    def test_falls_back_to_slicer_weights(self, mock_fetch_tool, mock_fetch_job, mock_clear):
        mock_fetch_tool.return_value = None  # mod not installed
        mock_fetch_job.return_value = [25.5]
        app_state.active_spool_uids["T0"] = "abc123"
        app_state.active_spool_devices["T0"] = "f3d360"

        _handle_toolchanger()

        app_state.mqtt_client.publish.assert_called_once()
        call_args = app_state.mqtt_client.publish.call_args
        self.assertEqual(call_args[0][0], "spoolsense/f3d360/cmd/deduct/abc123")

    @patch("filament_usage._clear_pending")
    @patch("filament_usage._fetch_last_job_weights")
    @patch("filament_usage._fetch_tool_filament_used")
    def test_fallback_per_tool_deductions(self, mock_fetch_tool, mock_fetch_job, mock_clear):
        mock_fetch_tool.return_value = None
        mock_fetch_job.return_value = [50.0, 0.0, 30.0, 0.0]
        app_state.active_spool_uids["T0"] = "uid-aaa"
        app_state.active_spool_devices["T0"] = "scanner1"
        app_state.active_spool_uids["T2"] = "uid-bbb"
        app_state.active_spool_devices["T2"] = "scanner1"

        _handle_toolchanger()

        self.assertEqual(app_state.mqtt_client.publish.call_count, 2)

    @patch("filament_usage._clear_pending")
    @patch("filament_usage._fetch_last_job_weights")
    @patch("filament_usage._fetch_tool_filament_used")
    def test_fallback_no_job_does_nothing(self, mock_fetch_tool, mock_fetch_job, mock_clear):
        mock_fetch_tool.return_value = None
        mock_fetch_job.return_value = None

        _handle_toolchanger()

        app_state.mqtt_client.publish.assert_not_called()

    @patch("filament_usage._clear_pending")
    @patch("filament_usage._fetch_last_job_weights")
    @patch("filament_usage._fetch_tool_filament_used")
    def test_fallback_skips_tool_without_active_spool(self, mock_fetch_tool, mock_fetch_job, mock_clear):
        mock_fetch_tool.return_value = None
        mock_fetch_job.return_value = [25.0]
        # No active spool on T0

        _handle_toolchanger()

        app_state.mqtt_client.publish.assert_not_called()


class TestMmToGrams(unittest.TestCase):

    def test_pla_1_75mm(self):
        # 1000mm of 1.75mm PLA at 1.24 g/cm³
        result = _mm_to_grams(1000.0, 1.75, 1.24)
        self.assertAlmostEqual(result, 2.98, places=1)

    def test_petg_2_85mm(self):
        # 1000mm of 2.85mm PETG at 1.27 g/cm³
        result = _mm_to_grams(1000.0, 2.85, 1.27)
        self.assertAlmostEqual(result, 8.10, places=1)


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
