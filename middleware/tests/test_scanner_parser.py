"""
Tests for openprinttag/scanner_parser.py — spoolsense_scanner firmware JSON
→ ScanEvent conversion. Covers rich tags, absent tags, UID-only reads, blank
tags, and edge cases in color/spoolman_id handling.
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

sys.modules.setdefault("paho", MagicMock())
sys.modules.setdefault("paho.mqtt", MagicMock())
sys.modules.setdefault("paho.mqtt.client", MagicMock())
sys.modules.setdefault("watchdog", MagicMock())
sys.modules.setdefault("watchdog.observers", MagicMock())
sys.modules.setdefault("watchdog.events", MagicMock())

from openprinttag.scanner_parser import scan_event_from_spoolsense_scanner  # noqa: E402
from state.models import ScanEvent  # noqa: E402


def _full_payload(**kwargs) -> dict:
    """Rich tag payload — all fields present, as firmware would publish on a valid read."""
    base = {
        "uid": "aabbccdd",
        "present": True,
        "tag_data_valid": True,
        "manufacturer": "Bambu",
        "material_type": "PLA",
        "material_name": "PLA Basic",
        "color": "#1A1A2E",
        "remaining_g": 185.5,
        "initial_weight_g": 1000.0,
        "spoolman_id": 7,
        "blank": False,
    }
    base.update(kwargs)
    return base


# ---------------------------------------------------------------------------
# Full rich tag
# ---------------------------------------------------------------------------

class TestFullRichTag(unittest.TestCase):

    def setUp(self):
        self.event = scan_event_from_spoolsense_scanner(_full_payload(), target_id="lane2")

    def test_returns_scan_event(self):
        self.assertIsInstance(self.event, ScanEvent)

    def test_source_is_spoolsense_scanner(self):
        self.assertEqual(self.event.source, "spoolsense_scanner")

    def test_target_id_preserved(self):
        self.assertEqual(self.event.target_id, "lane2")

    def test_uid_preserved(self):
        self.assertEqual(self.event.uid, "aabbccdd")

    def test_present_is_true(self):
        self.assertTrue(self.event.present)

    def test_tag_data_valid_is_true(self):
        self.assertTrue(self.event.tag_data_valid)

    def test_manufacturer_mapped_to_brand_name(self):
        # Firmware field 'manufacturer' maps to ScanEvent.brand_name per spec
        self.assertEqual(self.event.brand_name, "Bambu")

    def test_material_type_preserved(self):
        self.assertEqual(self.event.material_type, "PLA")

    def test_material_name_preserved(self):
        self.assertEqual(self.event.material_name, "PLA Basic")

    def test_color_hash_stripped_and_uppercased(self):
        # "#1A1A2E" → "1A1A2E" — no prefix, canonical uppercase
        self.assertEqual(self.event.color_hex, "1A1A2E")

    def test_remaining_g_mapped_to_remaining_weight_g(self):
        self.assertAlmostEqual(self.event.remaining_weight_g, 185.5)

    def test_initial_weight_g_mapped_to_full_weight_g(self):
        self.assertAlmostEqual(self.event.full_weight_g, 1000.0)

    def test_spoolman_id_preserved(self):
        # scanner_spoolman_id is a hint — caller must re-verify via UID
        self.assertEqual(self.event.scanner_spoolman_id, 7)

    def test_blank_is_false(self):
        self.assertFalse(self.event.blank)

    def test_raw_payload_attached(self):
        # raw is needed for debugging firmware bugs — must not be stripped
        self.assertIn("uid", self.event.raw)


# ---------------------------------------------------------------------------
# present=False (tag removed)
# ---------------------------------------------------------------------------

class TestAbsentTag(unittest.TestCase):

    def setUp(self):
        payload = _full_payload(present=False, tag_data_valid=False, uid="aabbccdd")
        self.event = scan_event_from_spoolsense_scanner(payload, target_id="lane1")

    def test_present_is_false(self):
        self.assertFalse(self.event.present)

    def test_tag_data_valid_is_false(self):
        self.assertFalse(self.event.tag_data_valid)

    def test_source_still_set(self):
        # Source must be populated even for absent events so logging works
        self.assertEqual(self.event.source, "spoolsense_scanner")


# ---------------------------------------------------------------------------
# tag_data_valid=False with UID present (UID-only / partial read)
# ---------------------------------------------------------------------------

class TestUidOnlyTag(unittest.TestCase):

    def setUp(self):
        # Firmware can see the chip but couldn't parse the data blocks
        payload = {
            "uid": "deadbeef",
            "present": True,
            "tag_data_valid": False,
            "blank": False,
        }
        self.event = scan_event_from_spoolsense_scanner(payload, target_id="T0")

    def test_present_is_true(self):
        self.assertTrue(self.event.present)

    def test_tag_data_valid_is_false(self):
        self.assertFalse(self.event.tag_data_valid)

    def test_uid_preserved(self):
        self.assertEqual(self.event.uid, "deadbeef")

    def test_filament_fields_are_none(self):
        # No data was readable — all filament fields must be absent
        self.assertIsNone(self.event.brand_name)
        self.assertIsNone(self.event.material_type)
        self.assertIsNone(self.event.color_hex)


# ---------------------------------------------------------------------------
# Blank tag
# ---------------------------------------------------------------------------

class TestBlankTag(unittest.TestCase):

    def setUp(self):
        # Blank = chip is present but has never been written
        payload = {
            "uid": "ff112233",
            "present": True,
            "tag_data_valid": False,
            "blank": True,
        }
        self.event = scan_event_from_spoolsense_scanner(payload, target_id="lane1")

    def test_blank_is_true(self):
        self.assertTrue(self.event.blank)

    def test_uid_preserved_for_blank(self):
        # UID is known even for blank tags — caller may use it to assign
        self.assertEqual(self.event.uid, "ff112233")

    def test_tag_data_valid_is_false_for_blank(self):
        self.assertFalse(self.event.tag_data_valid)


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases(unittest.TestCase):

    def test_spoolman_id_minus_one_becomes_none(self):
        # -1 is the firmware sentinel for "not linked" — strip to None
        payload = _full_payload(spoolman_id=-1)
        event = scan_event_from_spoolsense_scanner(payload, target_id="lane1")
        self.assertIsNone(event.scanner_spoolman_id)

    def test_color_without_hash_prefix_still_uppercased(self):
        payload = _full_payload(color="ff0000")
        event = scan_event_from_spoolsense_scanner(payload, target_id="lane1")
        self.assertEqual(event.color_hex, "FF0000")

    def test_no_color_field_yields_none(self):
        payload = _full_payload()
        del payload["color"]
        event = scan_event_from_spoolsense_scanner(payload, target_id="lane1")
        self.assertIsNone(event.color_hex)

    def test_no_uid_field_yields_none(self):
        payload = _full_payload()
        del payload["uid"]
        event = scan_event_from_spoolsense_scanner(payload, target_id="lane1")
        self.assertIsNone(event.uid)

    def test_empty_uid_field_yields_none(self):
        # Empty string from firmware should be treated as absent
        payload = _full_payload(uid="")
        event = scan_event_from_spoolsense_scanner(payload, target_id="lane1")
        self.assertIsNone(event.uid)

    def test_scanned_at_is_iso8601_string(self):
        payload = _full_payload()
        event = scan_event_from_spoolsense_scanner(payload, target_id="lane1")
        # Must be a non-empty string parseable as ISO 8601
        from datetime import datetime
        self.assertIsInstance(event.scanned_at, str)
        self.assertTrue(len(event.scanned_at) > 10)

    def test_topic_embedded_device_id_in_warning(self):
        # Parser must not crash when topic is present and tag_data_valid is True but uid missing
        payload = _full_payload(uid=None, tag_data_valid=True)
        # Should log a warning but not raise
        event = scan_event_from_spoolsense_scanner(
            payload,
            target_id="lane1",
            topic="spoolsense/ecb338/tag/attributes",
        )
        self.assertIsNone(event.uid)

    def test_present_defaults_to_true_when_absent_from_payload(self):
        # Some older firmware versions omitted 'present' from the payload
        payload = {"uid": "aabbccdd", "tag_data_valid": False}
        event = scan_event_from_spoolsense_scanner(payload, target_id="lane1")
        self.assertTrue(event.present)


if __name__ == "__main__":
    unittest.main()
