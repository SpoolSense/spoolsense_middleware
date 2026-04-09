"""Tests for spoolman/client.py — SpoolmanClient spool lookup and enrichment (read-only)."""
from __future__ import annotations

import os
import sys
import threading
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

sys.modules.setdefault("paho", MagicMock())
sys.modules.setdefault("paho.mqtt", MagicMock())
sys.modules.setdefault("paho.mqtt.client", MagicMock())
sys.modules.setdefault("watchdog", MagicMock())
sys.modules.setdefault("watchdog.observers", MagicMock())
sys.modules.setdefault("watchdog.events", MagicMock())

import app_state  # noqa: E402
from spoolman.client import SpoolmanClient  # noqa: E402
from state.models import ScanEvent  # noqa: E402

BASE_URL = "http://spoolman:7912"


def _reset_app_state():
    app_state.cfg = {"spoolman_url": BASE_URL}
    app_state.state_lock = threading.Lock()


def _ok_response(data):
    mock = MagicMock()
    mock.json.return_value = data
    mock.raise_for_status = lambda: None
    mock.status_code = 200
    return mock


def _make_scan_event(**kwargs) -> ScanEvent:
    defaults = {
        "source": "spoolsense_scanner",
        "target_id": "T0",
        "scanned_at": "2026-04-09T00:00:00Z",
        "uid": "aabbccdd",
        "present": True,
        "tag_data_valid": True,
        "brand_name": "PolyMaker",
        "material_type": "PLA",
        "material_name": "PolyLite PLA",
        "color_hex": "FF0000",
        "full_weight_g": 1000.0,
        "remaining_weight_g": 800.0,
    }
    defaults.update(kwargs)
    return ScanEvent(**defaults)


# ── Cache and lookup ─────────────────────────────────────────────────────────

class TestFetchAllSpools(unittest.TestCase):

    def setUp(self):
        _reset_app_state()

    @patch("requests.get")
    def test_indexes_spools_by_nfc_id(self, mock_get):
        spools = [
            {"id": 1, "extra": {"nfc_id": '"AABBCCDD"'}},
            {"id": 2, "extra": {"nfc_id": '"11223344"'}},
        ]
        mock_get.return_value = _ok_response(spools)
        client = SpoolmanClient(BASE_URL)

        client._fetch_all_spools()

        self.assertEqual(len(client.cache), 2)
        self.assertEqual(client.cache["aabbccdd"]["id"], 1)

    @patch("requests.get")
    def test_filters_archived_spools(self, mock_get):
        # The URL should include ?archived=false to exclude archived spools (#49)
        mock_get.return_value = _ok_response([])
        client = SpoolmanClient(BASE_URL)

        client._fetch_all_spools()

        call_url = mock_get.call_args[0][0]
        self.assertIn("archived=false", call_url)

    @patch("requests.get")
    def test_skips_spools_without_nfc_id(self, mock_get):
        spools = [{"id": 5, "extra": {}}, {"id": 6, "extra": {"nfc_id": '""'}}]
        mock_get.return_value = _ok_response(spools)
        client = SpoolmanClient(BASE_URL)

        client._fetch_all_spools()

        self.assertEqual(len(client.cache), 0)


class TestFindByNfc(unittest.TestCase):

    def setUp(self):
        _reset_app_state()

    @patch("requests.get")
    def test_returns_cached_spool(self, mock_get):
        spool = {"id": 10, "extra": {"nfc_id": '"aabbccdd"'}}
        mock_get.return_value = _ok_response([spool])
        client = SpoolmanClient(BASE_URL)

        result = client.find_by_nfc("AABBCCDD")

        self.assertIsNotNone(result)
        self.assertEqual(result["id"], 10)

    @patch("requests.get")
    def test_returns_none_for_unknown_uid(self, mock_get):
        mock_get.return_value = _ok_response([])
        client = SpoolmanClient(BASE_URL)

        result = client.find_by_nfc("deadbeef")

        self.assertIsNone(result)

    @patch("requests.get")
    def test_forces_refresh_on_cache_miss(self, mock_get):
        # First call returns empty, second returns the spool (scanner just created it)
        mock_get.side_effect = [
            _ok_response([]),
            _ok_response([{"id": 99, "extra": {"nfc_id": '"aabbccdd"'}}]),
        ]
        client = SpoolmanClient(BASE_URL)

        result = client.find_by_nfc("aabbccdd")

        self.assertIsNotNone(result)
        self.assertEqual(result["id"], 99)
        self.assertEqual(mock_get.call_count, 2)


# ── Sync from scan ──────────────────────────────────────────────────────────

class TestSyncSpoolFromScan(unittest.TestCase):

    def setUp(self):
        _reset_app_state()

    @patch("requests.get")
    def test_returns_enriched_spool_info_when_found(self, mock_get):
        existing = {
            "id": 3,
            "filament": {
                "color_hex": "0000FF",
                "material": "PLA",
                "name": "PolyLite PLA",
                "weight": 1000.0,
            },
            "extra": {"nfc_id": '"aabbccdd"'},
        }
        mock_get.return_value = _ok_response([existing])
        client = SpoolmanClient(BASE_URL)
        scan = _make_scan_event(color_hex="FF0000")

        result = client.sync_spool_from_scan(scan)

        self.assertIsNotNone(result)
        self.assertEqual(result.spoolman_id, 3)
        # Spoolman color wins over tag color
        self.assertEqual(result.color_hex, "0000FF")

    @patch("requests.get")
    def test_returns_none_when_spool_not_found(self, mock_get):
        # Spool not in Spoolman — scanner will create it, middleware runs tag-only
        mock_get.return_value = _ok_response([])
        client = SpoolmanClient(BASE_URL)
        scan = _make_scan_event()

        result = client.sync_spool_from_scan(scan)

        self.assertIsNone(result)

    def test_returns_none_when_no_uid(self):
        client = SpoolmanClient(BASE_URL)
        scan = _make_scan_event(uid=None)

        result = client.sync_spool_from_scan(scan)

        self.assertIsNone(result)

    @patch("requests.get")
    def test_tag_weight_preserved_when_prefer_tag(self, mock_get):
        existing = {
            "id": 5,
            "remaining_weight": 500.0,
            "filament": {"material": "PLA", "name": "PLA"},
            "extra": {"nfc_id": '"aabbccdd"'},
        }
        mock_get.return_value = _ok_response([existing])
        client = SpoolmanClient(BASE_URL)
        scan = _make_scan_event(remaining_weight_g=800.0)

        result = client.sync_spool_from_scan(scan, prefer_tag=True)

        # Tag weight (800g) preserved — Moonraker handles Spoolman weight sync
        self.assertEqual(result.remaining_weight_g, 800.0)

    @patch("requests.get")
    def test_spoolman_weight_used_when_not_prefer_tag(self, mock_get):
        existing = {
            "id": 5,
            "remaining_weight": 500.0,
            "filament": {"material": "PETG", "name": "PETG", "vendor": {"name": "Sunlu"}},
            "extra": {"nfc_id": '"aabbccdd"'},
        }
        mock_get.return_value = _ok_response([existing])
        client = SpoolmanClient(BASE_URL)
        scan = _make_scan_event(remaining_weight_g=800.0)

        result = client.sync_spool_from_scan(scan, prefer_tag=False)

        self.assertEqual(result.remaining_weight_g, 500.0)
        self.assertEqual(result.material_type, "PETG")


# ── No writes ────────────────────────────────────────────────────────────────

class TestNoWrites(unittest.TestCase):
    """Verify the client never writes to Spoolman — scanner and Moonraker handle that."""

    def setUp(self):
        _reset_app_state()

    @patch("requests.patch")
    @patch("requests.post")
    @patch("requests.get")
    def test_no_post_or_patch_on_existing_spool(self, mock_get, mock_post, mock_patch):
        existing = {
            "id": 1,
            "filament": {"material": "PLA", "name": "PLA"},
            "extra": {"nfc_id": '"aabbccdd"'},
        }
        mock_get.return_value = _ok_response([existing])
        client = SpoolmanClient(BASE_URL)

        client.sync_spool_from_scan(_make_scan_event())

        mock_post.assert_not_called()
        mock_patch.assert_not_called()

    @patch("requests.patch")
    @patch("requests.post")
    @patch("requests.get")
    def test_no_post_or_patch_on_missing_spool(self, mock_get, mock_post, mock_patch):
        mock_get.return_value = _ok_response([])
        client = SpoolmanClient(BASE_URL)

        result = client.sync_spool_from_scan(_make_scan_event())

        self.assertIsNone(result)
        mock_post.assert_not_called()
        mock_patch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
