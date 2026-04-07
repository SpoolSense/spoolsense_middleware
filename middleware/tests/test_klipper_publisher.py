"""
Tests for publishers/klipper.py — KlipperPublisher routing, gcode safety, and
Moonraker REST calls.

Covers input validation helpers (_validate_color_hex, _validate_material,
display_spoolcolor), the gcode transport (_send_gcode), and the publish()
dispatch path for each action type. HTTP calls are patched at the requests level.
"""
from __future__ import annotations

import os
import sys
import threading
import unittest
from unittest.mock import MagicMock, patch, call

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

sys.modules.setdefault("paho", MagicMock())
sys.modules.setdefault("paho.mqtt", MagicMock())
sys.modules.setdefault("paho.mqtt.client", MagicMock())
sys.modules.setdefault("watchdog", MagicMock())
sys.modules.setdefault("watchdog.observers", MagicMock())
sys.modules.setdefault("watchdog.events", MagicMock())

import app_state  # noqa: E402
from publishers.base import Action, SpoolEvent  # noqa: E402
from publishers.klipper import (  # noqa: E402
    KlipperPublisher,
    _validate_color_hex,
    _validate_material,
    _send_gcode,
    display_spoolcolor,
)

MOONRAKER = "http://moonraker:7125"


def _reset_app_state(moonraker_url: str = MOONRAKER) -> None:
    app_state.cfg = {
        "moonraker_url": moonraker_url,
        "publish_lane_data": False,
    }
    app_state.lane_locks = {}
    app_state.active_spools = {}
    app_state.state_lock = threading.Lock()


def _make_event(**kwargs) -> SpoolEvent:
    """Baseline SpoolEvent — callers override only the fields they care about."""
    defaults = dict(
        spool_id=5,
        action=Action.AFC_LANE,
        target="lane1",
        color="FF0000",
        material="PLA",
        weight=500.0,
        nozzle_temp_min=200,
        nozzle_temp_max=230,
        bed_temp_min=60,
        bed_temp_max=70,
        scanner_id="ecb338",
        tag_only=False,
    )
    defaults.update(kwargs)
    return SpoolEvent(**defaults)


class TestValidateColorHex(unittest.TestCase):
    """Tests for _validate_color_hex — gcode injection guard for colors."""

    def test_accepts_valid_6_digit_hex(self) -> None:
        self.assertEqual(_validate_color_hex("FF0000"), "FF0000")

    def test_strips_leading_hash(self) -> None:
        self.assertEqual(_validate_color_hex("#1A2B3C"), "1A2B3C")

    def test_normalizes_to_uppercase(self) -> None:
        self.assertEqual(_validate_color_hex("ff0000"), "FF0000")

    def test_rejects_5_digit_hex(self) -> None:
        self.assertIsNone(_validate_color_hex("FF000"))

    def test_rejects_7_digit_hex(self) -> None:
        self.assertIsNone(_validate_color_hex("FF00000"))

    def test_rejects_non_hex_characters(self) -> None:
        # Semicolons and spaces could be used for gcode injection
        self.assertIsNone(_validate_color_hex("GG0000"))

    def test_rejects_empty_string(self) -> None:
        self.assertIsNone(_validate_color_hex(""))

    def test_rejects_gcode_injection_attempt(self) -> None:
        self.assertIsNone(_validate_color_hex("FF0000; M112"))


class TestValidateMaterial(unittest.TestCase):
    """Tests for _validate_material — gcode injection guard for material names."""

    def test_accepts_simple_material(self) -> None:
        self.assertTrue(_validate_material("PLA"))

    def test_accepts_material_with_plus_style_hyphen(self) -> None:
        # Common filament names like "PLA-CF" use hyphens
        self.assertTrue(_validate_material("PLA-CF"))

    def test_accepts_material_with_space(self) -> None:
        self.assertTrue(_validate_material("ABS Plus"))

    def test_rejects_empty_string(self) -> None:
        self.assertFalse(_validate_material(""))

    def test_rejects_semicolon(self) -> None:
        # Semicolons separate gcode commands — cannot be allowed through
        self.assertFalse(_validate_material("PLA; M112"))

    def test_rejects_overly_long_material(self) -> None:
        self.assertFalse(_validate_material("A" * 51))

    def test_rejects_newline(self) -> None:
        self.assertFalse(_validate_material("PLA\nM112"))


class TestDisplaySpoolcolor(unittest.TestCase):
    """Tests for display_spoolcolor — LED color normalization."""

    def test_returns_uppercase_hex(self) -> None:
        self.assertEqual(display_spoolcolor("ff0000"), "FF0000")

    def test_returns_none_for_empty_string(self) -> None:
        self.assertIsNone(display_spoolcolor(""))

    def test_returns_none_for_invalid_hex(self) -> None:
        self.assertIsNone(display_spoolcolor("not-a-color"))

    def test_substitutes_black_with_dim_white(self) -> None:
        # Pure black produces no LED output — substitute dim white so the user
        # can tell a spool is loaded vs the LED being off entirely
        result = display_spoolcolor("000000")
        self.assertEqual(result, "333333")

    def test_white_passthrough(self) -> None:
        # White is a valid color and must not be substituted
        self.assertEqual(display_spoolcolor("FFFFFF"), "FFFFFF")


class TestSendGcode(unittest.TestCase):
    """Tests for _send_gcode — the Moonraker POST transport."""

    def setUp(self) -> None:
        _reset_app_state()

    @patch("requests.post")
    def test_posts_to_gcode_script_endpoint(self, mock_post: MagicMock) -> None:
        mock_post.return_value = MagicMock(raise_for_status=lambda: None)

        _send_gcode(MOONRAKER, "G28")

        url = mock_post.call_args[0][0]
        self.assertIn("/printer/gcode/script", url)

    @patch("requests.post")
    def test_sends_script_in_json_body(self, mock_post: MagicMock) -> None:
        mock_post.return_value = MagicMock(raise_for_status=lambda: None)

        _send_gcode(MOONRAKER, "SET_COLOR LANE=lane1 COLOR=FF0000")

        json_body = mock_post.call_args[1]["json"]
        self.assertEqual(json_body["script"], "SET_COLOR LANE=lane1 COLOR=FF0000")

    @patch("requests.post")
    def test_raises_on_http_error(self, mock_post: MagicMock) -> None:
        import requests as req
        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = req.HTTPError("500")
        mock_post.return_value = mock_resp

        with self.assertRaises(req.HTTPError):
            _send_gcode(MOONRAKER, "G28")


class TestKlipperPublisherAfcLane(unittest.TestCase):
    """Tests for publish() routing of Action.AFC_LANE events."""

    def setUp(self) -> None:
        _reset_app_state()

    @patch("requests.post")
    def test_afc_lane_sends_set_spool_id(self, mock_post: MagicMock) -> None:
        mock_post.return_value = MagicMock(raise_for_status=lambda: None)
        publisher = KlipperPublisher(app_state.cfg)

        result = publisher.publish(_make_event(action=Action.AFC_LANE, target="lane2", spool_id=8))

        self.assertTrue(result)
        scripts = [
            c[1]["json"].get("script", "")
            for c in mock_post.call_args_list
            if "json" in c[1]
        ]
        self.assertTrue(any("SET_SPOOL_ID LANE=lane2 SPOOL_ID=8" in s for s in scripts))

    @patch("requests.post")
    def test_afc_lane_tag_only_sends_color_not_spool_id(self, mock_post: MagicMock) -> None:
        # tag_only=True means no Spoolman backing — send color/material/weight directly
        mock_post.return_value = MagicMock(raise_for_status=lambda: None)
        publisher = KlipperPublisher(app_state.cfg)

        result = publisher.publish(
            _make_event(action=Action.AFC_LANE, tag_only=True, spool_id=None, color="00FF00")
        )

        self.assertTrue(result)
        scripts = [
            c[1]["json"].get("script", "")
            for c in mock_post.call_args_list
            if "json" in c[1]
        ]
        # SET_SPOOL_ID must not be issued — there is no Spoolman ID to set
        self.assertFalse(any("SET_SPOOL_ID" in s for s in scripts))
        # Color must still reach AFC
        self.assertTrue(any("SET_COLOR" in s for s in scripts))

    @patch("requests.post")
    def test_afc_lane_missing_target_returns_false(self, mock_post: MagicMock) -> None:
        publisher = KlipperPublisher(app_state.cfg)

        result = publisher.publish(_make_event(action=Action.AFC_LANE, target=""))

        self.assertFalse(result)
        mock_post.assert_not_called()


class TestKlipperPublisherToolhead(unittest.TestCase):
    """Tests for publish() routing of Action.TOOLHEAD events."""

    def setUp(self) -> None:
        _reset_app_state()

    @patch("requests.post")
    def test_toolhead_posts_spool_id_to_moonraker(self, mock_post: MagicMock) -> None:
        mock_post.return_value = MagicMock(raise_for_status=lambda: None)
        publisher = KlipperPublisher(app_state.cfg)

        result = publisher.publish(
            _make_event(action=Action.TOOLHEAD, target="T0", spool_id=15)
        )

        self.assertTrue(result)
        urls = [c[0][0] for c in mock_post.call_args_list]
        self.assertTrue(any("/server/spoolman/spool_id" in u for u in urls))

    @patch("requests.post")
    def test_toolhead_sends_save_variable(self, mock_post: MagicMock) -> None:
        mock_post.return_value = MagicMock(raise_for_status=lambda: None)
        publisher = KlipperPublisher(app_state.cfg)

        publisher.publish(_make_event(action=Action.TOOLHEAD, target="T0", spool_id=15))

        scripts = [
            c[1]["json"].get("script", "")
            for c in mock_post.call_args_list
            if "json" in c[1]
        ]
        # SAVE_VARIABLE persists the spool_id across printer restarts
        self.assertTrue(any("SAVE_VARIABLE VARIABLE=t0_spool_id VALUE=15" in s for s in scripts))

    @patch("requests.post")
    def test_toolhead_tag_only_sends_color_variable(self, mock_post: MagicMock) -> None:
        # No Spoolman — color from tag is sent via SET_GCODE_VARIABLE
        mock_post.return_value = MagicMock(raise_for_status=lambda: None)
        publisher = KlipperPublisher(app_state.cfg)

        result = publisher.publish(
            _make_event(
                action=Action.TOOLHEAD, target="T1", tag_only=True, spool_id=None, color="0000FF"
            )
        )

        self.assertTrue(result)
        scripts = [
            c[1]["json"].get("script", "")
            for c in mock_post.call_args_list
            if "json" in c[1]
        ]
        self.assertTrue(any("SET_GCODE_VARIABLE MACRO=T1 VARIABLE=color" in s for s in scripts))

    @patch("requests.post")
    def test_toolhead_missing_target_returns_false(self, mock_post: MagicMock) -> None:
        publisher = KlipperPublisher(app_state.cfg)

        result = publisher.publish(_make_event(action=Action.TOOLHEAD, target=""))

        self.assertFalse(result)

    @patch("requests.post")
    def test_toolhead_stage_returns_true_without_gcode(self, mock_post: MagicMock) -> None:
        # toolhead_stage is handled by toolchanger_status.py on tool pickup —
        # KlipperPublisher must be a no-op at scan time
        publisher = KlipperPublisher(app_state.cfg)

        result = publisher.publish(_make_event(action=Action.TOOLHEAD_STAGE, target="T2"))

        self.assertTrue(result)
        mock_post.assert_not_called()


class TestKlipperPublisherEdgeCases(unittest.TestCase):
    """Edge case and no-op behaviour tests for KlipperPublisher."""

    def setUp(self) -> None:
        _reset_app_state()

    @patch("requests.post")
    def test_unknown_action_returns_true_without_call(self, mock_post: MagicMock) -> None:
        # Unknown action types must be a no-op — forward compatibility for new PRs
        publisher = KlipperPublisher(app_state.cfg)
        event = _make_event(action="some_future_action")  # type: ignore[arg-type]

        result = publisher.publish(event)

        self.assertTrue(result)

    @patch("requests.post")
    def test_no_moonraker_url_returns_false(self, mock_post: MagicMock) -> None:
        app_state.cfg["moonraker_url"] = ""
        publisher = KlipperPublisher(app_state.cfg)

        result = publisher.publish(_make_event(action=Action.AFC_LANE))

        self.assertFalse(result)
        mock_post.assert_not_called()

    @patch("requests.post")
    def test_afc_stage_sends_set_next_spool_id(self, mock_post: MagicMock) -> None:
        mock_post.return_value = MagicMock(raise_for_status=lambda: None)
        publisher = KlipperPublisher(app_state.cfg)

        result = publisher.publish(_make_event(action=Action.AFC_STAGE, spool_id=99, target=""))

        self.assertTrue(result)
        scripts = [
            c[1]["json"].get("script", "")
            for c in mock_post.call_args_list
            if "json" in c[1]
        ]
        self.assertTrue(any("SET_NEXT_SPOOL_ID SPOOL_ID=99" in s for s in scripts))

    @patch("requests.post")
    def test_afc_stage_tag_only_is_noop(self, mock_post: MagicMock) -> None:
        # No Spoolman ID to stage — AFC cannot be told which spool to expect
        publisher = KlipperPublisher(app_state.cfg)

        result = publisher.publish(
            _make_event(action=Action.AFC_STAGE, spool_id=None, tag_only=True)
        )

        self.assertTrue(result)
        mock_post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
