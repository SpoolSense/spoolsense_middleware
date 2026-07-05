"""Tests for moonraker_client.py — shared Moonraker HTTP transport helpers."""
from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import moonraker_client  # noqa: E402
from moonraker_client import (  # noqa: E402
    FETCH_ERROR,
    get_active_spool_id,
    is_printer_idle,
    list_objects,
    query_objects,
    send_gcode,
    set_active_spool_id,
    set_database_item,
)

BASE = "http://moonraker:7125"


def _ok_json(payload):
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value=payload)
    return resp


class TestSendGcode(unittest.TestCase):

    @patch("moonraker_client.requests.post")
    def test_posts_script_to_gcode_endpoint(self, mock_post):
        mock_post.return_value = _ok_json({})
        send_gcode(BASE, "M117 hello")
        args, kwargs = mock_post.call_args
        self.assertEqual(args[0], f"{BASE}/printer/gcode/script")
        self.assertEqual(kwargs["json"], {"script": "M117 hello"})

    @patch("moonraker_client.requests.post")
    def test_raises_on_http_error(self, mock_post):
        resp = MagicMock()
        resp.raise_for_status = MagicMock(side_effect=requests.HTTPError("500"))
        mock_post.return_value = resp
        with self.assertRaises(requests.HTTPError):
            send_gcode(BASE, "M117 boom")


class TestQueryObjects(unittest.TestCase):

    @patch("moonraker_client.requests.get")
    def test_unwraps_result_status(self, mock_get):
        mock_get.return_value = _ok_json(
            {"result": {"status": {"print_stats": {"state": "standby"}}}}
        )
        status = query_objects(BASE, "print_stats")
        self.assertEqual(status, {"print_stats": {"state": "standby"}})
        self.assertIn("/printer/objects/query?print_stats", mock_get.call_args[0][0])

    @patch("moonraker_client.requests.get")
    def test_returns_none_on_connection_error(self, mock_get):
        mock_get.side_effect = requests.ConnectionError("refused")
        self.assertIsNone(query_objects(BASE, "mmu"))

    @patch("moonraker_client.requests.get")
    def test_returns_none_on_timeout(self, mock_get):
        mock_get.side_effect = requests.Timeout()
        self.assertIsNone(query_objects(BASE, "mmu"))

    @patch("moonraker_client.requests.get")
    def test_returns_none_on_non_dict_status(self, mock_get):
        mock_get.return_value = _ok_json({"result": {"status": "garbage"}})
        self.assertIsNone(query_objects(BASE, "mmu"))

    def test_returns_none_when_no_url(self):
        self.assertIsNone(query_objects("", "mmu"))


class TestListObjects(unittest.TestCase):

    @patch("moonraker_client.requests.get")
    def test_returns_object_list(self, mock_get):
        mock_get.return_value = _ok_json(
            {"result": {"objects": ["toolhead", "save_variables"]}}
        )
        self.assertEqual(list_objects(BASE), ["toolhead", "save_variables"])

    @patch("moonraker_client.requests.get")
    def test_returns_none_on_failure(self, mock_get):
        mock_get.side_effect = requests.ConnectionError("refused")
        self.assertIsNone(list_objects(BASE))

    def test_returns_none_when_no_url(self):
        self.assertIsNone(list_objects(""))


class TestGetActiveSpoolId(unittest.TestCase):

    @patch("moonraker_client.requests.get")
    def test_returns_spool_id(self, mock_get):
        mock_get.return_value = _ok_json({"result": {"spool_id": 42}})
        self.assertEqual(get_active_spool_id(BASE), 42)

    @patch("moonraker_client.requests.get")
    def test_zero_means_no_spool(self, mock_get):
        mock_get.return_value = _ok_json({"result": {"spool_id": 0}})
        self.assertIsNone(get_active_spool_id(BASE))

    @patch("moonraker_client.requests.get")
    def test_null_means_no_spool(self, mock_get):
        mock_get.return_value = _ok_json({"result": {"spool_id": None}})
        self.assertIsNone(get_active_spool_id(BASE))

    @patch("moonraker_client.requests.get")
    def test_connection_error_is_fetch_error_not_none(self, mock_get):
        # The tri-state matters: a failed fetch must never read as "ejected"
        mock_get.side_effect = requests.ConnectionError("refused")
        self.assertIs(get_active_spool_id(BASE), FETCH_ERROR)

    @patch("moonraker_client.requests.get")
    def test_404_is_fetch_error(self, mock_get):
        resp = MagicMock()
        resp.status_code = 404
        mock_get.side_effect = requests.HTTPError(response=resp)
        self.assertIs(get_active_spool_id(BASE), FETCH_ERROR)

    def test_no_url_is_fetch_error(self):
        self.assertIs(get_active_spool_id(""), FETCH_ERROR)


class TestSetActiveSpoolId(unittest.TestCase):

    @patch("moonraker_client.requests.post")
    def test_posts_spool_id(self, mock_post):
        mock_post.return_value = _ok_json({})
        set_active_spool_id(BASE, 42)
        args, kwargs = mock_post.call_args
        self.assertEqual(args[0], f"{BASE}/server/spoolman/spool_id")
        self.assertEqual(kwargs["json"], {"spool_id": 42})

    @patch("moonraker_client.requests.post")
    def test_raises_on_failure(self, mock_post):
        mock_post.side_effect = requests.ConnectionError("refused")
        with self.assertRaises(requests.ConnectionError):
            set_active_spool_id(BASE, 42)


class TestSetDatabaseItem(unittest.TestCase):

    @patch("moonraker_client.requests.post")
    def test_posts_namespaced_item(self, mock_post):
        mock_post.return_value = _ok_json({})
        set_database_item(BASE, "lane_data", "T0", {"color": "#FF0000"})
        args, kwargs = mock_post.call_args
        self.assertEqual(args[0], f"{BASE}/server/database/item")
        self.assertEqual(kwargs["json"], {
            "namespace": "lane_data", "key": "T0", "value": {"color": "#FF0000"},
        })


class TestIsPrinterIdle(unittest.TestCase):

    @patch("moonraker_client.requests.get")
    def test_standby_is_idle(self, mock_get):
        mock_get.return_value = _ok_json(
            {"result": {"status": {"print_stats": {"state": "standby"}}}}
        )
        self.assertTrue(is_printer_idle(BASE))

    @patch("moonraker_client.requests.get")
    def test_printing_is_busy(self, mock_get):
        mock_get.return_value = _ok_json(
            {"result": {"status": {"print_stats": {"state": "printing"}}}}
        )
        self.assertFalse(is_printer_idle(BASE))

    @patch("moonraker_client.requests.get")
    def test_fetch_failure_is_busy(self, mock_get):
        # Unknown state must never allow print-unsafe actions
        mock_get.side_effect = requests.ConnectionError("refused")
        self.assertFalse(is_printer_idle(BASE))

    def test_no_url_is_busy(self):
        self.assertFalse(is_printer_idle(""))


if __name__ == "__main__":
    unittest.main()
