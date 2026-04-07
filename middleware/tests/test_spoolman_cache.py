"""
Tests for spoolman_cache.py — in-memory UID cache: hit, miss, auto-refresh,
TTL expiry, and Spoolman API failure handling.
"""
from __future__ import annotations

import os
import sys
import threading
import time
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
import spoolman_cache  # noqa: E402


def _reset_app_state() -> None:
    app_state.cfg = {
        "moonraker_url": "http://moonraker:7125",
        "spoolman_url": "http://spoolman:7912",
        "low_spool_threshold": 100,
    }
    app_state.spool_cache: dict = {}
    # Push last_cache_refresh into the future so TTL checks don't auto-trigger
    app_state.last_cache_refresh = time.time()
    app_state.CACHE_TTL = 300
    app_state.lane_locks = {}
    app_state.active_spools = {}
    app_state.pending_spool = None
    app_state.state_lock = threading.Lock()


def _make_spool(nfc_id: str, spool_id: int = 1) -> dict:
    """Minimal Spoolman spool dict with an nfc_id extra field."""
    return {
        "id": spool_id,
        "extra": {"nfc_id": f'"{nfc_id}"'},
        "filament": {"name": "PLA", "color_hex": "FF0000"},
    }


def _mock_spoolman_response(spools: list) -> MagicMock:
    """Builds a requests.Response mock that returns the given spool list."""
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = spools
    return mock_resp


class TestFindSpoolByNfc(unittest.TestCase):

    def setUp(self):
        _reset_app_state()

    def test_returns_spool_from_warm_cache(self):
        # Cache hit — no network call should occur
        spool = _make_spool("aabbccdd", spool_id=5)
        app_state.spool_cache = {"aabbccdd": spool}
        result = spoolman_cache.find_spool_by_nfc("aabbccdd")
        self.assertEqual(result, spool)

    def test_lookup_is_case_insensitive(self):
        # UIDs arrive from firmware in mixed case; cache keys are always lower
        spool = _make_spool("aabbccdd", spool_id=7)
        app_state.spool_cache = {"aabbccdd": spool}
        result = spoolman_cache.find_spool_by_nfc("AABBCCDD")
        self.assertEqual(result, spool)

    def test_returns_none_for_unknown_uid_with_empty_cache(self):
        # UID not in cache and Spoolman has no matching spool
        app_state.spool_cache = {}
        with patch("requests.get") as mock_get:
            mock_get.return_value = _mock_spoolman_response([])
            result = spoolman_cache.find_spool_by_nfc("deadbeef")
        self.assertIsNone(result)

    def test_cache_miss_triggers_forced_refresh(self):
        # When the UID is absent, one refresh attempt must be made before returning None
        app_state.spool_cache = {}
        with patch("requests.get") as mock_get:
            mock_get.return_value = _mock_spoolman_response([])
            spoolman_cache.find_spool_by_nfc("unknown11")
            mock_get.assert_called()

    def test_cache_miss_returns_spool_after_refresh(self):
        # Simulates a spool that was added to Spoolman after the last cache build
        spool = _make_spool("fresh1122", spool_id=99)
        app_state.spool_cache = {}

        with patch("requests.get") as mock_get:
            mock_get.return_value = _mock_spoolman_response([spool])
            result = spoolman_cache.find_spool_by_nfc("fresh1122")

        self.assertIsNotNone(result)
        self.assertEqual(result["id"], 99)

    def test_ttl_expired_triggers_refresh_before_lookup(self):
        # Expired TTL must cause a refresh even on a cache hit that would otherwise work
        spool = _make_spool("olduid11", spool_id=3)
        app_state.spool_cache = {"olduid11": spool}
        app_state.last_cache_refresh = 0.0  # force TTL expiry

        with patch("requests.get") as mock_get:
            mock_get.return_value = _mock_spoolman_response([spool])
            result = spoolman_cache.find_spool_by_nfc("olduid11")

        mock_get.assert_called()
        self.assertEqual(result["id"], 3)


class TestRefreshSpoolCache(unittest.TestCase):

    def setUp(self):
        _reset_app_state()

    def test_populates_cache_from_api(self):
        spools = [
            _make_spool("uid00001", spool_id=1),
            _make_spool("uid00002", spool_id=2),
        ]
        with patch("requests.get") as mock_get:
            mock_get.return_value = _mock_spoolman_response(spools)
            result = spoolman_cache.refresh_spool_cache()

        self.assertTrue(result)
        self.assertIn("uid00001", app_state.spool_cache)
        self.assertIn("uid00002", app_state.spool_cache)

    def test_cache_keys_are_lowercase(self):
        # Spoolman extra.nfc_id may arrive with mixed case — normalize on ingest
        spool = {"id": 10, "extra": {"nfc_id": '"AABBCCDD"'}}
        with patch("requests.get") as mock_get:
            mock_get.return_value = _mock_spoolman_response([spool])
            spoolman_cache.refresh_spool_cache()

        self.assertIn("aabbccdd", app_state.spool_cache)

    def test_spools_without_nfc_id_are_excluded(self):
        # Only spools with a populated nfc_id belong in the NFC lookup index
        spools = [{"id": 5, "extra": {}}, {"id": 6, "extra": {"nfc_id": '""'}}]
        with patch("requests.get") as mock_get:
            mock_get.return_value = _mock_spoolman_response(spools)
            spoolman_cache.refresh_spool_cache()

        self.assertEqual(len(app_state.spool_cache), 0)

    def test_returns_false_when_no_spoolman_url(self):
        app_state.cfg["spoolman_url"] = ""
        result = spoolman_cache.refresh_spool_cache()
        self.assertFalse(result)

    def test_returns_false_on_network_error(self):
        import requests as req
        with patch("requests.get", side_effect=req.ConnectionError("refused")):
            result = spoolman_cache.refresh_spool_cache()
        self.assertFalse(result)

    def test_returns_false_on_http_error(self):
        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = Exception("404 Not Found")
        with patch("requests.get", return_value=mock_resp):
            result = spoolman_cache.refresh_spool_cache()
        self.assertFalse(result)

    def test_updates_last_cache_refresh_timestamp(self):
        before = time.time()
        with patch("requests.get") as mock_get:
            mock_get.return_value = _mock_spoolman_response([])
            spoolman_cache.refresh_spool_cache()
        self.assertGreaterEqual(app_state.last_cache_refresh, before)


if __name__ == "__main__":
    unittest.main()
