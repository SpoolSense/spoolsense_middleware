"""
Tests for spoolman/client.py — SpoolmanClient spool sync, lookup, and write logic.

Covers the public sync surface (sync_spool_from_scan, sync_spool), the internal
filament deduplication logic (_get_filament), weight calculation
(_update_spoolman_weight), and NFC writeback (_write_nfc_id).

HTTP calls are patched at the requests level. No real Spoolman server is needed.
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
from spoolman.client import SpoolmanClient  # noqa: E402
from state.models import ScanEvent, SpoolInfo  # noqa: E402


BASE_URL = "http://spoolman:7912"


def _reset_app_state() -> None:
    app_state.cfg = {"moonraker_url": "http://moonraker:7125"}
    app_state.lane_locks = {}
    app_state.active_spools = {}
    app_state.state_lock = threading.Lock()


def _make_scan_event(**kwargs) -> ScanEvent:
    """Minimal ScanEvent that represents a scanned PLA spool."""
    defaults = dict(
        source="spoolsense_scanner",
        target_id="lane1",
        scanned_at="2026-01-01T00:00:00Z",
        uid="aabbccdd",
        brand_name="PolyMaker",
        material_type="PLA",
        material_name="PolyLite PLA",
        color_name="Red",
        color_hex="FF0000",
        diameter_mm=1.75,
        full_weight_g=1000.0,
        remaining_weight_g=750.0,
    )
    defaults.update(kwargs)
    return ScanEvent(**defaults)


def _ok_response(data: object) -> MagicMock:
    """Build a mock requests.Response that succeeds and returns data."""
    resp = MagicMock()
    resp.json.return_value = data
    resp.raise_for_status = lambda: None
    return resp


class TestSyncSpoolFromScan(unittest.TestCase):
    """Tests for sync_spool_from_scan — the ScanEvent → SpoolInfo bridge."""

    def setUp(self) -> None:
        _reset_app_state()

    def test_returns_none_when_uid_missing(self) -> None:
        # A scan with no UID cannot be linked to a Spoolman entry
        client = SpoolmanClient(BASE_URL)
        scan = _make_scan_event(uid=None)

        result = client.sync_spool_from_scan(scan)

        self.assertIsNone(result)

    @patch("requests.get")
    def test_finds_existing_spool_by_nfc_uid(self, mock_get: MagicMock) -> None:
        # Cache returns a hit — sync_spool must use the existing Spoolman record
        existing_spool = {
            "id": 7,
            "filament": {
                "color_hex": "FF0000",
                "material": "PLA",
                "name": "PolyLite PLA",
                "weight": 1000.0,
            },
            "extra": {"nfc_id": '"aabbccdd"'},
        }
        mock_get.return_value = _ok_response([existing_spool])

        client = SpoolmanClient(BASE_URL)
        scan = _make_scan_event()

        result = client.sync_spool_from_scan(scan)

        self.assertIsNotNone(result)
        self.assertEqual(result.spoolman_id, 7)

    @patch("requests.patch")
    @patch("requests.post")
    @patch("requests.get")
    def test_creates_new_spool_when_not_found(
        self, mock_get: MagicMock, mock_post: MagicMock, mock_patch: MagicMock
    ) -> None:
        # Cache is empty — full create path: vendor → filament → spool → NFC writeback
        mock_get.side_effect = [
            _ok_response([]),           # _fetch_all_spools (cache miss)
            _ok_response([]),           # _fetch_all_spools (forced refresh)
            _ok_response([]),           # _get_vendor_by_name
            _ok_response([]),           # _get_filament
        ]
        mock_post.side_effect = [
            _ok_response({"id": 1, "name": "PolyMaker"}),   # _create_vendor
            _ok_response({"id": 10, "name": "PolyLite PLA"}),  # _create_filament
            _ok_response({"id": 50}),                        # _create_spool
        ]
        mock_patch.return_value = _ok_response({"id": 50})  # _write_nfc_id

        client = SpoolmanClient(BASE_URL)
        scan = _make_scan_event()

        result = client.sync_spool_from_scan(scan)

        self.assertIsNotNone(result)
        self.assertEqual(result.spoolman_id, 50)
        self.assertEqual(result.source, "created")

    @patch("requests.patch")
    @patch("requests.post")
    @patch("requests.get")
    def test_spoolman_color_overrides_tag_color(
        self, mock_get: MagicMock, mock_post: MagicMock, mock_patch: MagicMock
    ) -> None:
        # When Spoolman has a color set, it wins over the tag — a human set it deliberately
        existing_spool = {
            "id": 3,
            "filament": {
                "color_hex": "0000FF",  # blue in Spoolman, not red from tag
                "material": "PLA",
                "name": "PolyLite PLA",
                "weight": 1000.0,
            },
            "extra": {"nfc_id": '"aabbccdd"'},
        }
        mock_get.return_value = _ok_response([existing_spool])
        mock_patch.return_value = _ok_response({})

        client = SpoolmanClient(BASE_URL)
        scan = _make_scan_event(color_hex="FF0000")  # tag says red

        result = client.sync_spool_from_scan(scan)

        self.assertEqual(result.color_hex, "0000FF")  # Spoolman won


class TestGetFilament(unittest.TestCase):
    """Tests for _get_filament — deduplication logic for filament lookup."""

    def setUp(self) -> None:
        _reset_app_state()

    @patch("requests.get")
    def test_finds_matching_filament(self, mock_get: MagicMock) -> None:
        filament = {
            "id": 10,
            "vendor": {"id": 1},
            "material": "PLA",
            "color_hex": "FF0000",
            "name": "PolyLite PLA",
        }
        mock_get.return_value = _ok_response([filament])

        client = SpoolmanClient(BASE_URL)
        result = client._get_filament(
            vendor_id=1, material="PLA", color_hex="FF0000", name="PolyLite PLA"
        )

        self.assertEqual(result["id"], 10)

    @patch("requests.get")
    def test_color_hex_matching_is_case_insensitive(self, mock_get: MagicMock) -> None:
        # Tag may emit lowercase hex; Spoolman stores mixed case — must match
        filament = {
            "id": 11,
            "vendor": {"id": 1},
            "material": "PETG",
            "color_hex": "1A1A2E",
            "name": None,
        }
        mock_get.return_value = _ok_response([filament])

        client = SpoolmanClient(BASE_URL)
        result = client._get_filament(
            vendor_id=1, material="PETG", color_hex="#1a1a2e", name=None
        )

        self.assertIsNotNone(result)
        self.assertEqual(result["id"], 11)

    @patch("requests.get")
    def test_returns_none_when_no_match(self, mock_get: MagicMock) -> None:
        # Different material — should not match, return None so a new filament is created
        filament = {
            "id": 12,
            "vendor": {"id": 1},
            "material": "ABS",
            "color_hex": "FF0000",
            "name": "ABS Pro",
        }
        mock_get.return_value = _ok_response([filament])

        client = SpoolmanClient(BASE_URL)
        result = client._get_filament(
            vendor_id=1, material="PLA", color_hex="FF0000", name="ABS Pro"
        )

        self.assertIsNone(result)

    @patch("requests.get")
    def test_returns_none_when_name_differs(self, mock_get: MagicMock) -> None:
        # Same vendor/material/color but different name — treat as a distinct filament
        # to avoid silently merging different product lines
        filament = {
            "id": 13,
            "vendor": {"id": 2},
            "material": "PLA",
            "color_hex": "FFFFFF",
            "name": "PolyLite PLA",
        }
        mock_get.return_value = _ok_response([filament])

        client = SpoolmanClient(BASE_URL)
        result = client._get_filament(
            vendor_id=2, material="PLA", color_hex="FFFFFF", name="PolyMax PLA"
        )

        self.assertIsNone(result)


class TestUpdateSpoolmanWeight(unittest.TestCase):
    """Tests for _update_spoolman_weight — used_weight calculation."""

    def setUp(self) -> None:
        _reset_app_state()

    @patch("requests.patch")
    def test_calculates_used_weight_correctly(self, mock_patch: MagicMock) -> None:
        # used_weight = nominal_g - remaining_g
        mock_patch.return_value = _ok_response({})
        client = SpoolmanClient(BASE_URL)

        client._update_spoolman_weight(spoolman_id=5, remaining_g=650.0, nominal_g=1000.0)

        call_kwargs = mock_patch.call_args[1]
        self.assertEqual(call_kwargs["json"]["used_weight"], 350.0)

    @patch("requests.patch")
    def test_used_weight_clamped_to_zero(self, mock_patch: MagicMock) -> None:
        # Tag weight can exceed nominal (scale drift, spool weight included) — clamp at 0
        mock_patch.return_value = _ok_response({})
        client = SpoolmanClient(BASE_URL)

        client._update_spoolman_weight(spoolman_id=5, remaining_g=1200.0, nominal_g=1000.0)

        call_kwargs = mock_patch.call_args[1]
        self.assertEqual(call_kwargs["json"]["used_weight"], 0.0)

    @patch("requests.patch")
    def test_skips_update_when_remaining_is_none(self, mock_patch: MagicMock) -> None:
        # Both weights required — if tag had no weight field, do not write garbage to Spoolman
        client = SpoolmanClient(BASE_URL)

        client._update_spoolman_weight(spoolman_id=5, remaining_g=None, nominal_g=1000.0)

        mock_patch.assert_not_called()

    @patch("requests.patch")
    def test_skips_update_when_nominal_is_none(self, mock_patch: MagicMock) -> None:
        client = SpoolmanClient(BASE_URL)

        client._update_spoolman_weight(spoolman_id=5, remaining_g=500.0, nominal_g=None)

        mock_patch.assert_not_called()


class TestWriteNfcId(unittest.TestCase):
    """Tests for _write_nfc_id — NFC UID writeback to Spoolman extras."""

    def setUp(self) -> None:
        _reset_app_state()

    @patch("requests.patch")
    def test_writes_nfc_uid_to_spool_extras(self, mock_patch: MagicMock) -> None:
        mock_patch.return_value = _ok_response({})
        client = SpoolmanClient(BASE_URL)

        client._write_nfc_id(spoolman_id=99, nfc_uid="AABBCCDD")

        call_kwargs = mock_patch.call_args[1]
        self.assertEqual(call_kwargs["json"]["extra"]["nfc_id"], "aabbccdd")

    @patch("requests.patch")
    def test_nfc_uid_lowercased_before_write(self, mock_patch: MagicMock) -> None:
        # UIDs are always stored lowercase — inconsistent casing from the scanner
        # would cause cache misses on the next scan
        mock_patch.return_value = _ok_response({})
        client = SpoolmanClient(BASE_URL)

        client._write_nfc_id(spoolman_id=1, nfc_uid="FF112233")

        call_kwargs = mock_patch.call_args[1]
        self.assertEqual(call_kwargs["json"]["extra"]["nfc_id"], "ff112233")

    @patch("requests.patch")
    def test_updates_local_cache_after_write(self, mock_patch: MagicMock) -> None:
        # Cache must be updated immediately so the next scan can find the spool
        # without waiting for a full TTL refresh
        mock_patch.return_value = _ok_response({})
        client = SpoolmanClient(BASE_URL)

        client._write_nfc_id(spoolman_id=42, nfc_uid="deadbeef")

        self.assertIn("deadbeef", client.cache)
        self.assertEqual(client.cache["deadbeef"]["id"], 42)

    @patch("requests.patch")
    def test_raises_on_http_failure(self, mock_patch: MagicMock) -> None:
        import requests as req
        mock_patch.side_effect = req.HTTPError("403")

        client = SpoolmanClient(BASE_URL)

        with self.assertRaises(Exception):
            client._write_nfc_id(spoolman_id=1, nfc_uid="aabbccdd")


if __name__ == "__main__":
    unittest.main()
