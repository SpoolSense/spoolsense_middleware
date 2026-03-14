"""
Tests for scanner topic routing in spoolsense.py:
  - _extract_scanner_device_id()
  - _resolve_lane_from_topic()

HOW TO RUN:
    From the middleware/ directory:
        python -m pytest tests/test_spoolsense_scanner_routing.py -v
"""

import spoolsense


def test_extract_scanner_device_id_returns_id_for_valid_topic(monkeypatch):
    monkeypatch.setattr(
        spoolsense,
        "cfg",
        {"scanner_topic_prefix": "openprinttag"},
        raising=False,
    )
    topic = "openprinttag/ab12cd/tag/state"
    assert spoolsense._extract_scanner_device_id(topic) == "ab12cd"


def test_extract_scanner_device_id_returns_none_for_wrong_prefix(monkeypatch):
    monkeypatch.setattr(
        spoolsense,
        "cfg",
        {"scanner_topic_prefix": "openprinttag"},
        raising=False,
    )
    topic = "otherprefix/ab12cd/tag/state"
    assert spoolsense._extract_scanner_device_id(topic) is None


def test_extract_scanner_device_id_returns_empty_string_for_missing_device_id_current_behavior(monkeypatch):
    monkeypatch.setattr(
        spoolsense,
        "cfg",
        {"scanner_topic_prefix": "openprinttag"},
        raising=False,
    )
    # openprinttag//tag/state → parts[1] is "" — documents current behavior
    topic = "openprinttag//tag/state"
    assert spoolsense._extract_scanner_device_id(topic) == ""


def test_extract_scanner_device_id_returns_none_for_wrong_suffix(monkeypatch):
    monkeypatch.setattr(
        spoolsense,
        "cfg",
        {"scanner_topic_prefix": "openprinttag"},
        raising=False,
    )
    topic = "openprinttag/ab12cd/tag/attributes"
    assert spoolsense._extract_scanner_device_id(topic) is None


def test_extract_scanner_device_id_returns_none_for_extra_segments(monkeypatch):
    monkeypatch.setattr(
        spoolsense,
        "cfg",
        {"scanner_topic_prefix": "openprinttag"},
        raising=False,
    )
    # Topics with extra segments after /tag/state are rejected — exact 4-part match required
    topic = "openprinttag/ab12cd/tag/state/extra"
    assert spoolsense._extract_scanner_device_id(topic) is None


def test_extract_scanner_device_id_uses_configured_prefix(monkeypatch):
    monkeypatch.setattr(
        spoolsense,
        "cfg",
        {"scanner_topic_prefix": "customprefix"},
        raising=False,
    )
    topic = "customprefix/ab12cd/tag/state"
    assert spoolsense._extract_scanner_device_id(topic) == "ab12cd"


def test_resolve_lane_from_topic_returns_mapped_lane(monkeypatch):
    monkeypatch.setattr(
        spoolsense,
        "cfg",
        {
            "scanner_topic_prefix": "openprinttag",
            "scanner_lane_map": {"ab12cd": "lane1"},
        },
        raising=False,
    )
    topic = "openprinttag/ab12cd/tag/state"
    assert spoolsense._resolve_lane_from_topic(topic) == "lane1"


def test_resolve_lane_from_topic_returns_none_for_unmapped_scanner(monkeypatch):
    monkeypatch.setattr(
        spoolsense,
        "cfg",
        {
            "scanner_topic_prefix": "openprinttag",
            "scanner_lane_map": {"ef34gh": "lane2"},
        },
        raising=False,
    )
    topic = "openprinttag/ab12cd/tag/state"
    assert spoolsense._resolve_lane_from_topic(topic) is None


def test_resolve_lane_from_topic_returns_none_for_non_scanner_topic(monkeypatch):
    monkeypatch.setattr(
        spoolsense,
        "cfg",
        {
            "scanner_topic_prefix": "openprinttag",
            "scanner_lane_map": {"ab12cd": "lane1"},
        },
        raising=False,
    )
    topic = "nfc/toolhead/lane1/color"
    assert spoolsense._resolve_lane_from_topic(topic) is None


def test_resolve_lane_from_topic_returns_lane_for_pn532_topic(monkeypatch):
    monkeypatch.setattr(
        spoolsense,
        "cfg",
        {
            "scanner_topic_prefix": "openprinttag",
            "scanner_lane_map": {},
        },
        raising=False,
    )
    topic = "nfc/toolhead/lane1"
    assert spoolsense._resolve_lane_from_topic(topic) == "lane1"
