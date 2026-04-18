"""
Tests for rest_api.py — FastAPI HTTP endpoints: config summary, mobile-scan
processing, and assign-tool ASSIGN_SPOOL macro dispatch.
"""
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

# Stub heavy optional modules so rest_api.py can be imported in isolation
for _mod in (
    "var_watcher",
    "tag_sync",
    "tag_sync.policy",
    "tag_sync.scanner_writer",
    "spoolman",
    "spoolman.client",
    "yaml",
):
    sys.modules.setdefault(_mod, MagicMock())

import app_state  # noqa: E402
app_state.DISPATCHER_AVAILABLE = True

from fastapi.testclient import TestClient  # noqa: E402
from state.models import ScanEvent  # noqa: E402

# rest_api imports activation._activate_from_scan and publishers.klipper._send_gcode;
# both require live Moonraker — patch them out for unit tests
with (
    patch("activation._activate_from_scan"),
    patch("publishers.klipper._send_gcode"),
):
    from rest_api import app  # noqa: E402

client = TestClient(app)


def _reset_app_state(
    mobile_enabled: bool = True,
    mobile_action: str = "afc_stage",
) -> None:
    app_state.cfg = {
        "moonraker_url": "http://moonraker:7125",
        "spoolman_url": "http://spoolman:7912",
        "mobile": {
            "enabled": mobile_enabled,
            "action": mobile_action,
        },
        "scanners": {
            "ecb338": {"action": "afc_lane", "lane": "lane1"},
        },
        "toolheads": ["T0", "T1"],
        "low_spool_threshold": 100,
    }
    app_state.lane_locks = {}
    app_state.active_spools = {}
    app_state.pending_spool = None
    app_state.state_lock = threading.Lock()
    app_state.spoolman_client = None


def _make_scan_event(**kwargs) -> ScanEvent:
    """Minimal ScanEvent for route-level tests."""
    from datetime import datetime, timezone
    defaults = dict(
        source="mobile",
        target_id="mobile",
        scanned_at=datetime.now(timezone.utc).isoformat(),
        uid="aabbccdd",
        present=True,
        tag_data_valid=True,
        blank=False,
        material_type="PLA",
        material_name="PLA Basic",
        color_hex="FF0000",
        remaining_weight_g=200.0,
        scanner_spoolman_id=42,
    )
    defaults.update(kwargs)
    return ScanEvent(**defaults)


# ---------------------------------------------------------------------------
# GET /api/config
# ---------------------------------------------------------------------------

class TestGetConfig(unittest.TestCase):

    def setUp(self):
        _reset_app_state()

    def test_returns_200(self):
        resp = client.get("/api/config")
        self.assertEqual(resp.status_code, 200)

    def test_contains_mobile_section(self):
        resp = client.get("/api/config")
        data = resp.json()
        self.assertIn("mobile", data)
        self.assertEqual(data["mobile"]["enabled"], True)

    def test_scanners_view_is_list(self):
        resp = client.get("/api/config")
        data = resp.json()
        self.assertIsInstance(data["scanners"], list)

    def test_scanner_entry_has_expected_keys(self):
        resp = client.get("/api/config")
        scanner = resp.json()["scanners"][0]
        self.assertIn("device_id", scanner)
        self.assertIn("action", scanner)
        self.assertIn("target", scanner)

    def test_spoolman_url_present(self):
        resp = client.get("/api/config")
        self.assertEqual(resp.json()["spoolman_url"], "http://spoolman:7912")

    def test_toolheads_forwarded_to_mobile(self):
        resp = client.get("/api/config")
        self.assertEqual(resp.json()["mobile"]["toolheads"], ["T0", "T1"])


# ---------------------------------------------------------------------------
# POST /api/mobile-scan
# ---------------------------------------------------------------------------

class TestMobileScan(unittest.TestCase):

    def setUp(self):
        _reset_app_state(mobile_enabled=True, mobile_action="afc_stage")

    def _post(self, payload: dict):
        return client.post("/api/mobile-scan", json=payload)

    def test_returns_503_when_mobile_disabled(self):
        _reset_app_state(mobile_enabled=False)
        resp = self._post({"uid": "aabb", "present": True, "tag_data_valid": True})
        self.assertEqual(resp.status_code, 503)

    def test_valid_scan_returns_success(self):
        scan = _make_scan_event()
        with (
            patch("rest_api.detect_and_parse", return_value=scan),
            patch("rest_api._activate_from_scan"),
        ):
            resp = self._post({"uid": "aabbccdd", "present": True, "tag_data_valid": True})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["success"])

    def test_absent_tag_returns_failure(self):
        scan = _make_scan_event(present=False)
        with patch("rest_api.detect_and_parse", return_value=scan):
            resp = self._post({"uid": "aabbccdd", "present": False, "tag_data_valid": False})
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.json()["success"])

    def test_toolhead_stage_stores_pending_spool(self):
        _reset_app_state(mobile_enabled=True, mobile_action="toolhead_stage")
        scan = _make_scan_event()
        with patch("rest_api.detect_and_parse", return_value=scan):
            resp = self._post({"uid": "aabbccdd", "present": True, "tag_data_valid": True})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["success"])
        self.assertTrue(data["pending"])
        # Pending spool was stored in app_state
        self.assertIsNotNone(app_state.pending_spool)

    def test_toolhead_stage_replaced_flag_set_on_second_scan(self):
        _reset_app_state(mobile_enabled=True, mobile_action="toolhead_stage")
        # Pre-populate a pending spool to simulate scanning twice
        app_state.pending_spool = {"uid": "00000000", "spoolman_id": 1}
        scan = _make_scan_event()
        with patch("rest_api.detect_and_parse", return_value=scan):
            resp = self._post({"uid": "aabbccdd", "present": True, "tag_data_valid": True})
        self.assertTrue(resp.json()["replaced"])

    def test_parse_error_returns_failure_not_500(self):
        with patch("rest_api.detect_and_parse", side_effect=ValueError("bad format")):
            resp = self._post({"uid": "aabbccdd", "present": True, "tag_data_valid": True})
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.json()["success"])


# ---------------------------------------------------------------------------
# POST /api/assign-tool
# ---------------------------------------------------------------------------

class TestAssignTool(unittest.TestCase):

    def setUp(self):
        _reset_app_state(mobile_enabled=True, mobile_action="toolhead_stage")
        # Pre-populate a pending spool so assign-tool has something to work with
        app_state.pending_spool = {
            "uid": "aabbccdd",
            "spoolman_id": 42,
            "color_hex": "FF0000",
            "material": "PLA",
            "remaining_g": 200.0,
        }

    def _post(self, toolhead: str):
        return client.post("/api/assign-tool", json={"toolhead": toolhead})

    def test_returns_503_when_mobile_disabled(self):
        _reset_app_state(mobile_enabled=False, mobile_action="toolhead_stage")
        resp = self._post("T0")
        self.assertEqual(resp.status_code, 503)

    def test_returns_failure_for_wrong_action_mode(self):
        _reset_app_state(mobile_enabled=True, mobile_action="afc_stage")
        resp = self._post("T0")
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.json()["success"])

    def test_valid_assign_sends_gcode_and_returns_success(self):
        with patch("rest_api._send_gcode") as mock_gcode:
            resp = self._post("T0")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["success"])
        # ASSIGN_SPOOL TOOL= must be sent — this is the contract with Klipper macros
        mock_gcode.assert_called_once()
        call_args = mock_gcode.call_args[0]
        self.assertIn("ASSIGN_SPOOL", call_args[1])
        self.assertIn("T0", call_args[1])

    def test_toolhead_uppercased(self):
        # Mobile clients may send lowercase toolhead names
        with patch("rest_api._send_gcode") as mock_gcode:
            resp = self._post("t0")
        self.assertEqual(resp.status_code, 200)
        call_args = mock_gcode.call_args[0]
        self.assertIn("T0", call_args[1])

    def test_invalid_toolhead_returns_failure(self):
        with patch("rest_api._send_gcode"):
            resp = self._post("T9")
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.json()["success"])

    def test_no_pending_spool_returns_409(self):
        app_state.pending_spool = None
        resp = self._post("T0")
        self.assertEqual(resp.status_code, 409)

    def test_moonraker_error_returns_502(self):
        with patch("rest_api._send_gcode", side_effect=Exception("connection refused")):
            resp = self._post("T0")
        self.assertEqual(resp.status_code, 502)

    def test_response_includes_spool_id(self):
        with patch("rest_api._send_gcode"):
            resp = self._post("T0")
        self.assertEqual(resp.json()["spool_id"], 42)


if __name__ == "__main__":
    unittest.main()
