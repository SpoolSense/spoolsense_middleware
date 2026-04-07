"""
Tests for adapters/dispatcher.py — tag format auto-detection and routing to
the correct parser. Covers spoolsense_scanner, opentag3d, the not-yet-active
openprinttag format, and unknown payloads.
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

sys.modules.setdefault("paho", MagicMock())
sys.modules.setdefault("paho.mqtt", MagicMock())
sys.modules.setdefault("paho.mqtt.client", MagicMock())
sys.modules.setdefault("watchdog", MagicMock())
sys.modules.setdefault("watchdog.observers", MagicMock())
sys.modules.setdefault("watchdog.events", MagicMock())

from adapters.dispatcher import detect_format, detect_and_parse  # noqa: E402
from state.models import ScanEvent  # noqa: E402


def _spoolsense_payload(**kwargs) -> dict:
    """Minimal spoolsense_scanner firmware payload."""
    base = {
        "uid": "aabbccdd",
        "present": True,
        "tag_data_valid": True,
        "manufacturer": "Bambu",
        "material_type": "PLA",
        "material_name": "PLA Basic",
        "color": "#FF0000",
        "remaining_g": 200.0,
        "initial_weight_g": 1000.0,
        "spoolman_id": 5,
        "blank": False,
    }
    base.update(kwargs)
    return base


def _opentag3d_payload(**kwargs) -> dict:
    """Minimal OpenTag3D Web API payload."""
    base = {
        "uid": "11223344",
        "opentag_version": 1,
        "manufacturer": "Prusament",
        "material_name": "PLA",
        "spool_weight_nominal": 1000,
        "spool_weight_remaining": 750,
        "color_hex": "FF8C00",
        "color_name": "Orange",
    }
    base.update(kwargs)
    return base


# ---------------------------------------------------------------------------
# detect_format()
# ---------------------------------------------------------------------------

class TestDetectFormat(unittest.TestCase):

    def test_spoolsense_scanner_detected_by_both_keys(self):
        payload = {"present": True, "tag_data_valid": False}
        self.assertEqual(detect_format(payload), "spoolsense_scanner")

    def test_opentag3d_detected_by_opentag_version(self):
        payload = {"opentag_version": 1}
        self.assertEqual(detect_format(payload), "opentag3d")

    def test_opentag3d_detected_by_spool_weight_nominal(self):
        payload = {"spool_weight_nominal": 1000}
        self.assertEqual(detect_format(payload), "opentag3d")

    def test_openprinttag_detected_by_brand_name(self):
        # Not yet supported, but must be identified so we give a clear error
        payload = {"brand_name": "Bambu", "primary_color": "FF0000"}
        self.assertEqual(detect_format(payload), "openprinttag")

    def test_openprinttag_detected_by_actual_netto_full_weight(self):
        payload = {"actual_netto_full_weight": 990}
        self.assertEqual(detect_format(payload), "openprinttag")

    def test_unknown_returns_unknown(self):
        payload = {"some_random_key": 42}
        self.assertEqual(detect_format(payload), "unknown")

    def test_empty_payload_returns_unknown(self):
        self.assertEqual(detect_format({}), "unknown")

    def test_spoolsense_requires_both_keys(self):
        # Having only 'present' is not sufficient — must not mis-detect
        payload = {"present": True}
        self.assertNotEqual(detect_format(payload), "spoolsense_scanner")


# ---------------------------------------------------------------------------
# detect_and_parse()
# ---------------------------------------------------------------------------

class TestDetectAndParse(unittest.TestCase):

    def test_routes_spoolsense_scanner_payload(self):
        payload = _spoolsense_payload()
        event = detect_and_parse(payload, target_id="lane1")
        self.assertIsInstance(event, ScanEvent)
        self.assertEqual(event.source, "spoolsense_scanner")

    def test_routes_opentag3d_payload(self):
        payload = _opentag3d_payload()
        event = detect_and_parse(payload, target_id="T0")
        self.assertIsInstance(event, ScanEvent)
        # OpenTag3D parser normalizes source to "opentag3d"
        self.assertEqual(event.source, "opentag3d")

    def test_spoolsense_scanner_preserves_target_id(self):
        payload = _spoolsense_payload()
        event = detect_and_parse(payload, target_id="lane3")
        self.assertEqual(event.target_id, "lane3")

    def test_opentag3d_preserves_target_id(self):
        payload = _opentag3d_payload()
        event = detect_and_parse(payload, target_id="T1")
        self.assertEqual(event.target_id, "T1")

    def test_openprinttag_raises_not_implemented(self):
        # openprinttag CBOR format is detected but not yet active — must tell the user clearly
        payload = {"brand_name": "Bambu", "primary_color": "FF0000"}
        with self.assertRaises(NotImplementedError):
            detect_and_parse(payload, target_id="default")

    def test_unknown_format_raises_value_error(self):
        payload = {"some_random_key": 42}
        with self.assertRaises(ValueError):
            detect_and_parse(payload, target_id="default")

    def test_explicit_format_key_overrides_detection(self):
        # If the payload carries a 'format' key, use it directly without key inspection
        payload = _spoolsense_payload()
        payload["format"] = "spoolsense_scanner"
        event = detect_and_parse(payload, target_id="lane1")
        self.assertEqual(event.source, "spoolsense_scanner")

    def test_topic_arg_is_optional(self):
        # topic is used for logging only — omitting it must not cause errors
        payload = _spoolsense_payload()
        event = detect_and_parse(payload, target_id="lane1")
        self.assertIsInstance(event, ScanEvent)

    def test_spoolsense_absent_tag_parsed_correctly(self):
        payload = _spoolsense_payload(present=False, tag_data_valid=False)
        event = detect_and_parse(payload, target_id="lane1")
        self.assertFalse(event.present)

    def test_spoolsense_uid_parsed_from_payload(self):
        payload = _spoolsense_payload(uid="deadbeef")
        event = detect_and_parse(payload, target_id="lane1")
        self.assertEqual(event.uid, "deadbeef")


if __name__ == "__main__":
    unittest.main()
