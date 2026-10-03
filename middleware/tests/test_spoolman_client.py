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


def _tag(uid: str) -> dict:
    """A native Spoolman tag as v0.27+ returns it on a spool."""
    return {"uid": uid, "format": "ntag", "added": "2026-10-03T00:00:00Z"}


class TestNativeTags(unittest.TestCase):
    """Spoolman v0.27+ native tags (#123). Older servers send no `tags` key."""

    def setUp(self) -> None:
        _reset_app_state()

    @patch("requests.get")
    def test_finds_spool_by_native_tag(self, mock_get: MagicMock) -> None:
        # #121: the UID lived only in a native tag, cache said "0 spools indexed"
        mock_get.return_value = _ok_response([{"id": 7, "extra": {}, "tags": [_tag("53B5674D740001")]}])
        client = SpoolmanClient(BASE_URL)

        result = client.find_by_nfc("53B5674D740001")

        self.assertIsNotNone(result)
        self.assertEqual(result["id"], 7)

    @patch("requests.get")
    def test_indexes_every_tag_on_a_spool(self, mock_get: MagicMock) -> None:
        mock_get.return_value = _ok_response([{"id": 7, "tags": [_tag("AABB0001"), _tag("AABB0002")]}])
        client = SpoolmanClient(BASE_URL)

        client._fetch_all_spools()

        self.assertEqual(client.cache["aabb0001"]["id"], 7)
        self.assertEqual(client.cache["aabb0002"]["id"], 7)

    @patch("requests.get")
    def test_indexes_native_tags_and_nfc_id_side_by_side(self, mock_get: MagicMock) -> None:
        spools = [
            {"id": 1, "extra": {"nfc_id": '"AABBCCDD"'}},
            {"id": 2, "extra": {}, "tags": [_tag("11223344")]},
        ]
        mock_get.return_value = _ok_response(spools)
        client = SpoolmanClient(BASE_URL)

        client._fetch_all_spools()

        self.assertEqual(client.cache["aabbccdd"]["id"], 1)
        self.assertEqual(client.cache["11223344"]["id"], 2)

    @patch("requests.get")
    def test_same_uid_in_both_places_on_one_spool_is_not_a_conflict(self, mock_get: MagicMock) -> None:
        # Dual-write (scanner/app) puts the UID in both places on the same spool
        spool = {"id": 3, "extra": {"nfc_id": '"AABBCCDD"'}, "tags": [_tag("AABBCCDD")]}
        mock_get.return_value = _ok_response([spool])
        client = SpoolmanClient(BASE_URL)

        with self.assertLogs("spoolman.client", level="INFO") as logs:
            client._fetch_all_spools()

        self.assertEqual(client.cache["aabbccdd"]["id"], 3)
        self.assertFalse(any(r.levelname == "WARNING" for r in logs.records))

    @patch("requests.get")
    def test_native_tag_wins_over_another_spools_nfc_id(self, mock_get: MagicMock) -> None:
        legacy = {"id": 1, "extra": {"nfc_id": '"AABBCCDD"'}}
        native = {"id": 2, "extra": {}, "tags": [_tag("AABBCCDD")]}
        for order in ([legacy, native], [native, legacy]):
            with self.subTest(order=[s["id"] for s in order]):
                mock_get.return_value = _ok_response(order)
                client = SpoolmanClient(BASE_URL)

                with self.assertLogs("spoolman.client", level="WARNING") as logs:
                    client._fetch_all_spools()

                self.assertEqual(client.cache["aabbccdd"]["id"], 2)
                message = logs.records[0].getMessage()
                self.assertIn("spool 2", message)
                self.assertIn("spool 1", message)

    @patch("requests.get")
    def test_conflict_warning_names_legacy_claimant_hidden_by_dual_write(self, mock_get: MagicMock) -> None:
        # Spool 2 is dual-written (nfc_id + native tag); spool 1 still carries the
        # same nfc_id. Spool 2 comes last, so it also owns the legacy slot — the
        # warning must still name spool 1.
        stale = {"id": 1, "extra": {"nfc_id": '"AABBCCDD"'}}
        dual = {"id": 2, "extra": {"nfc_id": '"AABBCCDD"'}, "tags": [_tag("AABBCCDD")]}
        mock_get.return_value = _ok_response([stale, dual])
        client = SpoolmanClient(BASE_URL)

        with self.assertLogs("spoolman.client", level="WARNING") as logs:
            client._fetch_all_spools()

        self.assertEqual(client.cache["aabbccdd"]["id"], 2)
        self.assertIn("spool 1", logs.records[0].getMessage())

    @patch("requests.get")
    def test_matches_hex_prefix_like_spoolman(self, mock_get: MagicMock) -> None:
        # Spoolman's normalize_uid strips an optional 0x; stored tags never carry it
        mock_get.return_value = _ok_response([{"id": 7, "tags": [_tag("04A2B3C4")]}])
        client = SpoolmanClient(BASE_URL)

        self.assertEqual(client.find_by_nfc("0x04A2B3C4")["id"], 7)
        self.assertEqual(client.find_by_nfc("0X04a2b3c4")["id"], 7)

    @patch("requests.get")
    def test_matches_across_case_separators_and_quotes(self, mock_get: MagicMock) -> None:
        spools = [
            {"id": 1, "extra": {"nfc_id": '"04:A2:B3:C4"'}},
            {"id": 2, "extra": {}, "tags": [_tag("04A2B3C5")]},
        ]
        mock_get.return_value = _ok_response(spools)
        client = SpoolmanClient(BASE_URL)

        self.assertEqual(client.find_by_nfc("04a2b3c4")["id"], 1)
        self.assertEqual(client.find_by_nfc("04-a2-b3-c5")["id"], 2)
        self.assertEqual(client.find_by_nfc('"04A2B3C5"')["id"], 2)

    @patch("requests.get")
    def test_ignores_malformed_tags(self, mock_get: MagicMock) -> None:
        spools = [
            {"id": 1, "tags": None},
            {"id": 2, "tags": "AABBCCDD"},
            {"id": 3, "tags": ["AABBCCDD", {"format": "ntag"}, {"uid": ""}, {"uid": 1234}]},
            {"id": 4, "tags": [_tag("11223344")]},
        ]
        mock_get.return_value = _ok_response(spools)
        client = SpoolmanClient(BASE_URL)

        client._fetch_all_spools()

        self.assertEqual(list(client.cache), ["11223344"])
        self.assertEqual(client.cache["11223344"]["id"], 4)

    @patch("requests.get")
    def test_refresh_log_counts_both_sources(self, mock_get: MagicMock) -> None:
        spools = [
            {"id": 1, "extra": {"nfc_id": '"AABBCCDD"'}},
            {"id": 2, "tags": [_tag("11223344"), _tag("55667788")]},
        ]
        mock_get.return_value = _ok_response(spools)
        client = SpoolmanClient(BASE_URL)

        with self.assertLogs("spoolman.client", level="INFO") as logs:
            client._fetch_all_spools()

        self.assertTrue(any(
            "3 UIDs indexed (2 native tags, 1 extra.nfc_id)" in r.getMessage() for r in logs.records
        ))


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


# ── get_spool_by_id ──────────────────────────────────────────────────────────

class TestGetSpoolById(unittest.TestCase):

    def setUp(self):
        _reset_app_state()

    @patch("requests.get")
    def test_returns_spool_dict_on_success(self, mock_get):
        spool = {"id": 42, "filament": {"name": "PLA"}, "extra": {}}
        mock_get.return_value = _ok_response(spool)
        client = SpoolmanClient(BASE_URL)

        result = client.get_spool_by_id(42)

        self.assertEqual(result, spool)
        call_url = mock_get.call_args[0][0]
        self.assertIn("/api/v1/spool/42", call_url)

    @patch("requests.get")
    def test_returns_none_on_http_error(self, mock_get):
        import requests as req
        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = req.HTTPError("404 Not Found")
        mock_get.return_value = mock_resp
        client = SpoolmanClient(BASE_URL)

        result = client.get_spool_by_id(99)

        self.assertIsNone(result)

    @patch("requests.get")
    def test_returns_none_on_network_error(self, mock_get):
        import requests as req
        mock_get.side_effect = req.ConnectionError("refused")
        client = SpoolmanClient(BASE_URL)

        result = client.get_spool_by_id(7)

        self.assertIsNone(result)


# ── refresh ──────────────────────────────────────────────────────────────────

class TestRefresh(unittest.TestCase):

    def setUp(self):
        _reset_app_state()

    @patch("requests.get")
    def test_refresh_primes_cache(self, mock_get):
        spools = [{"id": 1, "extra": {"nfc_id": '"aabbccdd"'}}]
        mock_get.return_value = _ok_response(spools)
        client = SpoolmanClient(BASE_URL)

        client.refresh()

        self.assertEqual(len(client.cache), 1)
        mock_get.assert_called_once()


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


class TestSpoolmanRemainingExposed(unittest.TestCase):
    """#119 — the tag-preferred merge must still expose Spoolman's own
    remaining weight; deduction baselines need it."""

    def setUp(self):
        _reset_app_state()

    def _client_with_spool(self, spool: dict | None) -> SpoolmanClient:
        with patch.object(SpoolmanClient, "_fetch_all_spools"):
            client = SpoolmanClient(BASE_URL)
        client.find_by_nfc = MagicMock(return_value=spool)
        return client

    def test_prefer_tag_still_exposes_spoolman_remaining(self):
        client = self._client_with_spool(
            {"id": 5, "remaining_weight": 812.5, "filament": {}})
        scan = _make_scan_event(remaining_weight_g=1000.0)
        info = client.sync_spool_from_scan(scan, prefer_tag=True)
        self.assertEqual(info.spoolman_remaining_g, 812.5)
        # merge semantics unchanged — tag value still wins the display field
        self.assertEqual(info.remaining_weight_g, 1000.0)

    def test_spoolman_without_weight_data_gives_none(self):
        client = self._client_with_spool({"id": 5, "filament": {}})
        scan = _make_scan_event(remaining_weight_g=1000.0)
        info = client.sync_spool_from_scan(scan, prefer_tag=True)
        self.assertIsNone(info.spoolman_remaining_g)

    def test_no_spoolman_match_returns_none_info(self):
        client = self._client_with_spool(None)
        scan = _make_scan_event(remaining_weight_g=1000.0)
        self.assertIsNone(client.sync_spool_from_scan(scan, prefer_tag=True))


class TestUpdateSpoolExtras(unittest.TestCase):
    """PATCH /api/v1/spool/<id> with `extra` updates, JSON-encoded per value."""

    def setUp(self):
        _reset_app_state()

    @patch("requests.patch")
    def test_extras_json_encoded_per_value(self, mock_patch):
        # Spoolman stores extras as JSON-encoded strings, so we must encode
        # each value before sending: int 4 → "4", string "muffin" → '"muffin"'.
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        mock_patch.return_value = resp

        client = SpoolmanClient(BASE_URL)
        result = client.update_spool_extras(42, {"mmu_gate": 4, "printer_name": "muffin"})

        assert result is True
        args, kwargs = mock_patch.call_args
        assert args[0] == f"{BASE_URL}/api/v1/spool/42"
        assert kwargs["json"] == {"extra": {"mmu_gate": "4", "printer_name": '"muffin"'}}

    @patch("requests.patch")
    def test_returns_false_on_http_error(self, mock_patch):
        import requests as req
        mock_patch.side_effect = req.HTTPError("404")
        client = SpoolmanClient(BASE_URL)
        assert client.update_spool_extras(42, {"x": 1}) is False

    @patch("requests.patch")
    def test_logs_spoolman_response_body_on_400(self, mock_patch):
        # Unknown extra fields return 400 with a body like
        # `{"message": "Unknown extra field mmu_gate."}` — the log must
        # include that body so users know which field to declare.
        import requests as req
        err_response = MagicMock()
        err_response.text = '{"message": "Unknown extra field mmu_gate."}'
        http_error = req.HTTPError("400 Client Error")
        http_error.response = err_response

        resp = MagicMock()
        resp.raise_for_status = MagicMock(side_effect=http_error)
        mock_patch.return_value = resp

        client = SpoolmanClient(BASE_URL)
        with self.assertLogs("spoolman.client", level="ERROR") as captured:
            result = client.update_spool_extras(42, {"mmu_gate": 4})
        assert result is False
        combined = "\n".join(r.getMessage() for r in captured.records)
        assert "Unknown extra field mmu_gate" in combined

    @patch("requests.patch")
    def test_returns_false_on_connection_error(self, mock_patch):
        import requests as req
        mock_patch.side_effect = req.ConnectionError("refused")
        client = SpoolmanClient(BASE_URL)
        assert client.update_spool_extras(42, {"x": 1}) is False


if __name__ == "__main__":
    unittest.main()
