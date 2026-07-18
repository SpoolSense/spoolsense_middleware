"""Tests for activation.py — spool activation, lock management, publisher routing."""
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
from activation import activate_spool  # noqa: E402
from publishers.klipper import _validate_color_hex, _validate_material  # noqa: E402


def _setup_app_state(moonraker_url="http://moonraker:7125"):
    app_state.cfg = {
        "moonraker_url": moonraker_url,
        "low_spool_threshold": 100,
    }
    app_state.lane_locks = {}
    app_state.active_spools = {}
    app_state.pending_spool_afc = None
    app_state.pending_spool_toolhead = None
    app_state.state_lock = threading.Lock()


class TestActivateSpool(unittest.TestCase):

    def setUp(self):
        _setup_app_state()

    @patch("requests.post")
    def test_afc_stage_sends_correct_gcode(self, mock_post):
        mock_post.return_value = MagicMock(raise_for_status=lambda: None)
        result = activate_spool(42, "afc_stage")
        assert result is True
        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args
        assert "SET_NEXT_SPOOL_ID SPOOL_ID=42" in kwargs["json"]["script"]

    @patch("requests.post")
    def test_afc_lane_sends_correct_gcode(self, mock_post):
        mock_post.return_value = MagicMock(raise_for_status=lambda: None)
        result = activate_spool(7, "afc_lane", target="lane1")
        assert result is True
        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args
        assert "SET_SPOOL_ID LANE=lane1 SPOOL_ID=7" in kwargs["json"]["script"]

    @patch("requests.post")
    def test_toolhead_sends_correct_gcode(self, mock_post):
        mock_post.return_value = MagicMock(raise_for_status=lambda: None)
        result = activate_spool(99, "toolhead", target="T0")
        assert result is True
        assert mock_post.call_count == 2
        # First call sets active spool in Moonraker's spoolman endpoint
        first_call_url = mock_post.call_args_list[0][0][0]
        assert "/server/spoolman/spool_id" in first_call_url
        # Second call saves the variable
        second_script = mock_post.call_args_list[1][1]["json"]["script"]
        assert "SAVE_VARIABLE VARIABLE=t0_spool_id VALUE=99" in second_script

    @patch("requests.post")
    def test_toolhead_stage_logs_staging_returns_true(self, mock_post):
        result = activate_spool(55, "toolhead_stage")
        assert result is True
        # No HTTP calls for toolhead_stage
        mock_post.assert_not_called()

    def test_no_moonraker_url_returns_false(self):
        app_state.cfg["moonraker_url"] = ""
        result = activate_spool(1, "afc_lane", target="lane1")
        assert result is False

    def test_afc_lane_no_target_returns_false(self):
        result = activate_spool(1, "afc_lane", target=None)
        assert result is False

    def test_toolhead_no_target_returns_false(self):
        result = activate_spool(1, "toolhead", target=None)
        assert result is False

    @patch("requests.post")
    def test_moonraker_error_returns_false(self, mock_post):
        import requests as req
        mock_post.side_effect = req.ConnectionError("refused")
        result = activate_spool(1, "afc_lane", target="lane1")
        assert result is False

    @patch("requests.post")
    def test_moonraker_http_error_returns_false(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = Exception("500 Server Error")
        mock_post.return_value = mock_resp
        result = activate_spool(1, "afc_stage")
        assert result is False

    @patch("requests.post")
    def test_unknown_action_returns_false(self, mock_post):
        result = activate_spool(1, "not_an_action")
        assert result is False
        mock_post.assert_not_called()


class TestValidateColorHex(unittest.TestCase):

    def test_valid_lowercase_hex(self):
        assert _validate_color_hex("ff0000") == "FF0000"

    def test_valid_uppercase_hex(self):
        assert _validate_color_hex("ABCDEF") == "ABCDEF"

    def test_valid_with_hash_prefix(self):
        assert _validate_color_hex("#1a2b3c") == "1A2B3C"

    def test_valid_all_zeros(self):
        assert _validate_color_hex("000000") == "000000"

    def test_invalid_too_short(self):
        assert _validate_color_hex("ff00") is None

    def test_invalid_too_long(self):
        assert _validate_color_hex("ff0000ff") is None

    def test_invalid_non_hex_chars(self):
        assert _validate_color_hex("gggggg") is None

    def test_invalid_empty_string(self):
        assert _validate_color_hex("") is None

    def test_invalid_spaces(self):
        assert _validate_color_hex("ff 000") is None


class TestValidateMaterial(unittest.TestCase):

    def test_valid_simple_material(self):
        assert _validate_material("PLA") is True

    def test_valid_material_with_numbers(self):
        assert _validate_material("PLA95") is True

    def test_valid_material_with_space(self):
        assert _validate_material("PLA Pro") is True

    def test_valid_material_with_dash(self):
        assert _validate_material("PLA-Plus") is True

    def test_valid_material_with_underscore(self):
        assert _validate_material("PLA_HF") is True

    def test_valid_exactly_50_chars(self):
        assert _validate_material("A" * 50) is True

    def test_too_long_returns_false(self):
        assert _validate_material("A" * 51) is False

    def test_empty_string_returns_false(self):
        assert _validate_material("") is False

    def test_special_chars_returns_false(self):
        assert _validate_material("PLA!") is False

    def test_sql_injection_returns_false(self):
        assert _validate_material("PLA'; DROP TABLE") is False

    def test_newline_returns_false(self):
        assert _validate_material("PLA\n") is False


class TestStagedObserverEvents(unittest.TestCase):
    """Tag-only staged rich scans never reach the publisher chain — they must
    still hit the observer path so the MQTT event stream sees them (#93)."""

    def test_tag_only_staged_notifies_observers(self):
        from activation import _route_staged
        from publishers.base import Action
        event = MagicMock()
        with patch("activation.notify_observers") as mock_notify, \
             patch("activation._cache_pending_spool"):
            _route_staged(Action.AFC_STAGE, False, "FF0000", "PLA", 500.0,
                          None, event)
        mock_notify.assert_called_once_with(event)

    def test_spoolman_staged_does_not_double_notify(self):
        # With a spoolman_id the event already went through the manager —
        # secondaries saw it there; notifying again would duplicate
        from activation import _route_staged
        from publishers.base import Action
        with patch("activation.notify_observers") as mock_notify, \
             patch("activation._cache_pending_spool"):
            _route_staged(Action.AFC_STAGE, True, "FF0000", "PLA", 500.0,
                          42, MagicMock())
        mock_notify.assert_not_called()


class TestBuildSpoolEventTemps(unittest.TestCase):
    """SpoolEvent temps must come from ScanEvent's *_c fields — the old
    suffixless getattr silently produced None for every rich scan."""

    def test_temps_copied_from_scan_c_fields(self):
        from activation import _build_spool_event
        from publishers.base import Action
        from state.models import ScanEvent
        scan = ScanEvent(
            source="spoolsense_scanner", target_id="T0", scanned_at="now",
            uid="AA", present=True, tag_data_valid=True,
            nozzle_temp_min_c=240, nozzle_temp_max_c=260,
            bed_temp_min_c=90, bed_temp_max_c=110,
        )
        event = _build_spool_event({"action": "toolhead_stage"}, Action.TOOLHEAD_STAGE,
                                   None, 42, "FF0000", "ASA", 500.0, scan)
        self.assertEqual(event.nozzle_temp_min, 240)
        self.assertEqual(event.nozzle_temp_max, 260)
        self.assertEqual(event.bed_temp_min, 90)
        self.assertEqual(event.bed_temp_max, 110)


class TestPendingSlotIsolation(unittest.TestCase):
    """A scan staged for AFC must land in the AFC slot and never be visible
    to the toolchanger consumer, and vice versa — the shared-slot race this
    split exists to kill."""

    def setUp(self):
        _setup_app_state()

    def _stage(self, action):
        from activation import _route_staged
        event = MagicMock(nozzle_temp_min=None, nozzle_temp_max=None,
                          bed_temp_min=None, bed_temp_max=None)
        with patch("activation.notify_observers"):
            _route_staged(action, True, "FF0000", "PLA", 500.0, 42, event)

    def test_afc_scan_fills_only_afc_slot(self):
        from publishers.base import Action
        self._stage(Action.AFC_STAGE)
        self.assertIsNotNone(app_state.pending_spool_afc)
        self.assertIsNone(app_state.pending_spool_toolhead)

    def test_toolhead_scan_fills_only_toolhead_slot(self):
        from publishers.base import Action
        self._stage(Action.TOOLHEAD_STAGE)
        self.assertIsNone(app_state.pending_spool_afc)
        self.assertIsNotNone(app_state.pending_spool_toolhead)

    def test_mixed_scans_do_not_clobber_each_other(self):
        from publishers.base import Action
        self._stage(Action.AFC_STAGE)
        self._stage(Action.TOOLHEAD_STAGE)
        self.assertEqual(app_state.pending_spool_afc["spoolman_id"], 42)
        self.assertEqual(app_state.pending_spool_toolhead["spoolman_id"], 42)
        self.assertIsNot(app_state.pending_spool_afc,
                         app_state.pending_spool_toolhead)


class TestStagedCarriesTrackingFields(unittest.TestCase):
    """Staged scans carry uid + filament props into the pending slot so the
    consumer can record a deduction baseline on assignment (#109). The uid is
    stored as-sent — /api/status echoes this dict to the shipped mobile app."""

    def setUp(self):
        _setup_app_state()

    def test_rich_scan_fields_land_in_pending_slot(self):
        from activation import _route_staged
        from publishers.base import Action
        from state.models import ScanEvent
        scan = ScanEvent(
            source="spoolsense_scanner", target_id="T0", scanned_at="now",
            uid="AABB11", present=True, tag_data_valid=True,
            diameter_mm=1.75, density=1.24,
            raw={"tag_format": "openprinttag"},
        )
        event = MagicMock(nozzle_temp_min=None, nozzle_temp_max=None,
                          bed_temp_min=None, bed_temp_max=None)
        with patch("activation.notify_observers"):
            _route_staged(Action.TOOLHEAD_STAGE, True, "FF0000", "PLA", 500.0,
                          42, event, scan, "f3d360")
        pending = app_state.pending_spool_toolhead
        self.assertEqual(pending["uid"], "AABB11")
        self.assertEqual(pending["device_id"], "f3d360")
        self.assertEqual(pending["diameter_mm"], 1.75)
        self.assertEqual(pending["tag_format"], "openprinttag")

    def test_opentag3d_format_derived_from_source(self):
        # Direct OpenTag3D payloads have no tag_format key — the parser puts
        # the format in scan.source; storing "unknown" would make
        # _is_writable_tag silently skip every deduction after assignment
        from activation import _route_staged
        from publishers.base import Action
        from state.models import ScanEvent
        scan = ScanEvent(
            source="opentag3d", target_id="T0", scanned_at="now",
            uid="AABB11", present=True, tag_data_valid=True,
            raw={"opentag_version": 1},
        )
        event = MagicMock(nozzle_temp_min=None, nozzle_temp_max=None,
                          bed_temp_min=None, bed_temp_max=None)
        with patch("activation.notify_observers"):
            _route_staged(Action.TOOLHEAD_STAGE, True, "FF0000", "PLA", 500.0,
                          42, event, scan, "f3d360")
        self.assertEqual(app_state.pending_spool_toolhead["tag_format"],
                         "opentag3d")

    def test_no_scan_defaults_are_harmless(self):
        from activation import _route_staged
        from publishers.base import Action
        event = MagicMock(nozzle_temp_min=None, nozzle_temp_max=None,
                          bed_temp_min=None, bed_temp_max=None)
        with patch("activation.notify_observers"):
            _route_staged(Action.AFC_STAGE, True, "FF0000", "PLA", 500.0,
                          42, event)
        pending = app_state.pending_spool_afc
        self.assertIsNone(pending["uid"])
        self.assertEqual(pending["tag_format"], "unknown")


class TestBuildSpoolEventScannerId(unittest.TestCase):
    """scanner_id must carry the source scanner so event-stream (#93)
    consumers can tell scanners apart — regression for the live finding
    where rich staged scans published scanner_id="unknown"."""

    def _event(self, scanner_cfg, target, device_id):
        from activation import _build_spool_event
        from publishers.base import Action
        scan = MagicMock(nozzle_temp_min=None, nozzle_temp_max=None,
                         bed_temp_min=None, bed_temp_max=None)
        return _build_spool_event(scanner_cfg, Action.TOOLHEAD_STAGE, target,
                                  None, "FF0000", "PLA", 500.0, scan,
                                  device_id=device_id)

    def test_topic_device_id_wins(self):
        # Stage scanner (no target) — device_id from the topic must show up
        ev = self._event({"action": "toolhead_stage"}, None, "f3d360")
        self.assertEqual(ev.scanner_id, "f3d360")

    def test_falls_back_to_target_when_no_device_id(self):
        ev = self._event({"action": "toolhead"}, "T0", None)
        self.assertEqual(ev.scanner_id, "T0")

    def test_unknown_only_when_nothing_available(self):
        ev = self._event({"action": "toolhead_stage"}, None, None)
        self.assertEqual(ev.scanner_id, "unknown")


if __name__ == "__main__":
    unittest.main()
