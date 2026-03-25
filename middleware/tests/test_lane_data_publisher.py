"""Tests for the lane_data publisher."""
from __future__ import annotations

from unittest.mock import patch, MagicMock
import pytest

from publishers.base import SpoolEvent, Action
from publishers.lane_data import LaneDataPublisher, _extract_lane_number, _avg_temp


class TestLaneDataPublisherEnabled:
    def test_enabled_when_config_set(self) -> None:
        pub = LaneDataPublisher({"moonraker_url": "http://localhost", "publish_lane_data": True})
        assert pub.enabled({"moonraker_url": "http://localhost", "publish_lane_data": True})

    def test_disabled_by_default(self) -> None:
        pub = LaneDataPublisher({"moonraker_url": "http://localhost"})
        assert not pub.enabled({"moonraker_url": "http://localhost"})

    def test_disabled_without_moonraker(self) -> None:
        pub = LaneDataPublisher({"publish_lane_data": True})
        assert not pub.enabled({"publish_lane_data": True})

    def test_name(self) -> None:
        pub = LaneDataPublisher({})
        assert pub.name == "lane_data"

    def test_not_primary(self) -> None:
        pub = LaneDataPublisher({})
        assert pub.primary is False


class TestLaneDataPublish:
    def _make_event(self, **overrides) -> SpoolEvent:
        defaults = {
            "spool_id": 42,
            "action": Action.TOOLHEAD,
            "target": "T0",
            "color": "FF0000",
            "material": "PLA",
            "weight": 1000.0,
            "nozzle_temp_min": 200,
            "nozzle_temp_max": 220,
            "bed_temp_min": 55,
            "bed_temp_max": 65,
            "scanner_id": "test",
            "tag_only": False,
        }
        defaults.update(overrides)
        return SpoolEvent(**defaults)

    @patch("publishers.lane_data.requests.post")
    def test_publishes_to_moonraker_db(self, mock_post: MagicMock) -> None:
        mock_post.return_value.raise_for_status = MagicMock()
        pub = LaneDataPublisher({"moonraker_url": "http://localhost"})
        event = self._make_event()
        result = pub.publish(event)
        assert result is True
        mock_post.assert_called_once()
        payload = mock_post.call_args[1]["json"]
        assert payload["namespace"] == "lane_data"
        assert payload["key"] == "T0"
        assert payload["value"]["color"] == "#FF0000"
        assert payload["value"]["material"] == "PLA"
        assert payload["value"]["spool_id"] == 42
        assert payload["value"]["weight"] == 1000.0
        assert payload["value"]["lane"] == "0"

    @patch("publishers.lane_data.requests.post")
    def test_temps_averaged(self, mock_post: MagicMock) -> None:
        mock_post.return_value.raise_for_status = MagicMock()
        pub = LaneDataPublisher({"moonraker_url": "http://localhost"})
        event = self._make_event(nozzle_temp_min=200, nozzle_temp_max=220, bed_temp_min=55, bed_temp_max=65)
        pub.publish(event)
        payload = mock_post.call_args[1]["json"]
        assert payload["value"]["nozzle_temp"] == 210
        assert payload["value"]["bed_temp"] == 60

    @patch("publishers.lane_data.requests.post")
    def test_no_target_returns_true(self, mock_post: MagicMock) -> None:
        """afc_stage with no target yet — nothing to publish."""
        pub = LaneDataPublisher({"moonraker_url": "http://localhost"})
        event = self._make_event(target="", action=Action.AFC_STAGE)
        result = pub.publish(event)
        assert result is True
        mock_post.assert_not_called()

    @patch("publishers.lane_data.requests.post")
    def test_no_moonraker_returns_false(self, mock_post: MagicMock) -> None:
        pub = LaneDataPublisher({"moonraker_url": ""})
        event = self._make_event()
        result = pub.publish(event)
        assert result is False

    @patch("publishers.lane_data.requests.post")
    def test_http_error_returns_false(self, mock_post: MagicMock) -> None:
        mock_post.return_value.raise_for_status.side_effect = Exception("500 Server Error")
        pub = LaneDataPublisher({"moonraker_url": "http://localhost"})
        event = self._make_event()
        result = pub.publish(event)
        assert result is False

    @patch("publishers.lane_data.requests.post")
    def test_tag_only_publishes(self, mock_post: MagicMock) -> None:
        """Tag-only mode should still publish — spool_id will be None."""
        mock_post.return_value.raise_for_status = MagicMock()
        pub = LaneDataPublisher({"moonraker_url": "http://localhost"})
        event = self._make_event(spool_id=None, tag_only=True)
        result = pub.publish(event)
        assert result is True
        payload = mock_post.call_args[1]["json"]
        assert payload["value"]["spool_id"] is None

    @patch("publishers.lane_data.requests.post")
    def test_lane_name_target(self, mock_post: MagicMock) -> None:
        """AFC lane targets should extract lane number correctly."""
        mock_post.return_value.raise_for_status = MagicMock()
        pub = LaneDataPublisher({"moonraker_url": "http://localhost"})
        event = self._make_event(target="lane3", action=Action.AFC_LANE)
        pub.publish(event)
        payload = mock_post.call_args[1]["json"]
        assert payload["value"]["lane"] == "3"
        assert payload["key"] == "lane3"

    @patch("publishers.lane_data.requests.post")
    def test_no_color_sends_empty_string(self, mock_post: MagicMock) -> None:
        mock_post.return_value.raise_for_status = MagicMock()
        pub = LaneDataPublisher({"moonraker_url": "http://localhost"})
        event = self._make_event(color=None)
        pub.publish(event)
        payload = mock_post.call_args[1]["json"]
        assert payload["value"]["color"] == ""

    @patch("publishers.lane_data.requests.post")
    def test_no_temps_sends_empty_string(self, mock_post: MagicMock) -> None:
        mock_post.return_value.raise_for_status = MagicMock()
        pub = LaneDataPublisher({"moonraker_url": "http://localhost"})
        event = self._make_event(nozzle_temp_min=None, nozzle_temp_max=None, bed_temp_min=None, bed_temp_max=None)
        pub.publish(event)
        payload = mock_post.call_args[1]["json"]
        assert payload["value"]["nozzle_temp"] == ""
        assert payload["value"]["bed_temp"] == ""


class TestExtractLaneNumber:
    def test_toolhead(self) -> None:
        assert _extract_lane_number("T0") == "0"

    def test_multi_digit(self) -> None:
        assert _extract_lane_number("T12") == "12"

    def test_lane_name(self) -> None:
        assert _extract_lane_number("lane3") == "3"

    def test_no_number(self) -> None:
        assert _extract_lane_number("extruder") == "0"


class TestAvgTemp:
    def test_both(self) -> None:
        assert _avg_temp(200, 220) == 210

    def test_min_only(self) -> None:
        assert _avg_temp(200, None) == 200

    def test_max_only(self) -> None:
        assert _avg_temp(None, 220) == 220

    def test_neither(self) -> None:
        assert _avg_temp(None, None) == ""
