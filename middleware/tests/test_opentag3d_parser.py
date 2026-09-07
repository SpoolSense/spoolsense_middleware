"""Tests for opentag3d/parser.py — OpenTag3D v2 weight_source contract (#119)."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from opentag3d.parser import parse_opentag3d  # noqa: E402


def _payload(**extra) -> dict:
    base = {
        "uid": "53AB12CD34EF56",
        "opentag_version": 2,
        "manufacturer": "Prusament",
        "material_name": "PLA",
        "spool_weight_nominal": 1000,
        "spool_weight_measured": 1000,
    }
    base.update(extra)
    return base


class TestWeightSourceParsing(unittest.TestCase):

    def test_nominal_weight_source_parsed(self):
        scan = parse_opentag3d(_payload(weight_source="nominal"), "lane1")
        self.assertEqual(scan.weight_source, "nominal")
        self.assertEqual(scan.remaining_weight_g, 1000)

    def test_measured_weight_source_parsed(self):
        scan = parse_opentag3d(
            _payload(weight_source="measured", spool_weight_measured=812), "lane1")
        self.assertEqual(scan.weight_source, "measured")
        self.assertEqual(scan.remaining_weight_g, 812)

    def test_absent_weight_source_is_none(self):
        # Legacy firmware — field missing entirely
        scan = parse_opentag3d(_payload(), "lane1")
        self.assertIsNone(scan.weight_source)

    def test_pending_deduction_parsed(self):
        scan = parse_opentag3d(_payload(pending_deduction_g=12.5), "lane1")
        self.assertEqual(scan.pending_deduction_g, 12.5)

    def test_absent_pending_deduction_is_none(self):
        scan = parse_opentag3d(_payload(), "lane1")
        self.assertIsNone(scan.pending_deduction_g)


if __name__ == "__main__":
    unittest.main()
