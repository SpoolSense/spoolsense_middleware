"""Tests for tracking_store.py — deduction baselines survive restarts (#91)."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

sys.modules.setdefault("paho", MagicMock())
sys.modules.setdefault("paho.mqtt", MagicMock())
sys.modules.setdefault("paho.mqtt.client", MagicMock())

import app_state  # noqa: E402
from tracking_store import load_tracking, save_tracking  # noqa: E402


class TestTrackingStore(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self.tmp.close()
        os.unlink(self.tmp.name)
        app_state.TRACKING_FILE = self.tmp.name
        app_state.state_lock = threading.Lock()
        app_state.active_spool_tracking = {}

    def tearDown(self):
        if os.path.exists(self.tmp.name):
            os.unlink(self.tmp.name)

    def test_round_trip(self):
        app_state.active_spool_tracking["T0"] = app_state.ActiveSpool(
            uid="aabbcc", device_id="f3d360", weight_g=750.0,
            diameter_mm=2.85, density=1.27, tag_format="openprinttag",
        )
        save_tracking()
        app_state.active_spool_tracking = {}
        load_tracking()
        rec = app_state.active_spool_tracking["T0"]
        self.assertEqual(rec.uid, "aabbcc")
        self.assertEqual(rec.device_id, "f3d360")
        self.assertEqual(rec.weight_g, 750.0)
        self.assertEqual(rec.diameter_mm, 2.85)
        self.assertEqual(rec.density, 1.27)
        self.assertEqual(rec.tag_format, "openprinttag")

    def test_missing_file_is_noop(self):
        load_tracking()
        self.assertEqual(app_state.active_spool_tracking, {})

    def test_corrupt_file_tolerated(self):
        with open(self.tmp.name, "w") as f:
            f.write("{not json")
        load_tracking()  # must not raise
        self.assertEqual(app_state.active_spool_tracking, {})

    def test_malformed_record_skipped_others_load(self):
        with open(self.tmp.name, "w") as f:
            json.dump({
                "T0": {"uid": "good", "weight_g": 100.0},
                "T1": {"uid": "bad", "unexpected_field": True},
                "T2": {"no_uid_at_all": 1},
            }, f)
        load_tracking()
        self.assertIn("T0", app_state.active_spool_tracking)
        self.assertNotIn("T1", app_state.active_spool_tracking)
        self.assertNotIn("T2", app_state.active_spool_tracking)

    def test_save_never_raises(self):
        app_state.TRACKING_FILE = "/nonexistent-root-dir/tracking.json"
        save_tracking()  # must not raise


class TestClearTracking(unittest.TestCase):
    """Eject/clear paths must drop the persisted baseline — otherwise a
    restart resurrects it and UPDATE_TAG deducts from an unmounted spool."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self.tmp.close()
        os.unlink(self.tmp.name)
        app_state.TRACKING_FILE = self.tmp.name
        app_state.state_lock = threading.Lock()
        app_state.active_spool_tracking = {
            "T0": app_state.ActiveSpool(uid="aaa", weight_g=500.0),
            "lane1": app_state.ActiveSpool(uid="bbb", weight_g=750.0),
        }

    def tearDown(self):
        if os.path.exists(self.tmp.name):
            os.unlink(self.tmp.name)

    def test_clear_removes_record_and_persists(self):
        from tracking_store import clear_tracking, load_tracking
        clear_tracking("T0")
        self.assertNotIn("T0", app_state.active_spool_tracking)
        # The FILE must not resurrect it either
        app_state.active_spool_tracking = {}
        load_tracking()
        self.assertNotIn("T0", app_state.active_spool_tracking)
        self.assertIn("lane1", app_state.active_spool_tracking)

    def test_clear_missing_target_is_noop_without_file_write(self):
        from tracking_store import clear_tracking
        app_state.active_spool_tracking = {}
        clear_tracking("lane1")   # nothing tracked → nothing removed
        self.assertFalse(os.path.exists(self.tmp.name))

    def test_clear_multiple_targets(self):
        from tracking_store import clear_tracking
        clear_tracking("T0", "lane1", "not-tracked")
        self.assertEqual(app_state.active_spool_tracking, {})


if __name__ == "__main__":
    unittest.main()
