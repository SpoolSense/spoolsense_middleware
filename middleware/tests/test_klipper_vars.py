"""Tests for klipper_vars.py — save_variables websocket callback sync."""
from __future__ import annotations

import os
import sys
import threading
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

sys.modules.setdefault("paho", MagicMock())
sys.modules.setdefault("paho.mqtt", MagicMock())
sys.modules.setdefault("paho.mqtt.client", MagicMock())

import app_state  # noqa: E402
from klipper_vars import on_ws_save_variables  # noqa: E402


def _reset(toolheads=None):
    app_state.cfg = {"toolheads": toolheads if toolheads is not None else ["T0", "T1"]}
    app_state.active_spools = {}
    app_state.state_lock = threading.Lock()


class TestOnWsSaveVariables(unittest.TestCase):

    def setUp(self):
        _reset()

    def test_reads_spool_ids_into_active_spools(self):
        on_ws_save_variables({"t0_spool_id": 131, "t1_spool_id": 132})
        self.assertEqual(app_state.active_spools["T0"], 131)
        self.assertEqual(app_state.active_spools["T1"], 132)

    def test_string_values_tolerated(self):
        # Values normally arrive as native ints, but strings shouldn't break sync
        on_ws_save_variables({"t0_spool_id": "42"})
        self.assertEqual(app_state.active_spools["T0"], 42)

    def test_zero_means_ejected(self):
        # Same 0-means-ejected convention as toolhead_status.py
        app_state.active_spools["T0"] = 131
        on_ws_save_variables({"t0_spool_id": 0})
        self.assertIsNone(app_state.active_spools["T0"])

    def test_missing_var_clears_active_spool(self):
        # variables arrives complete — an absent key genuinely means unset
        app_state.active_spools["T0"] = 131
        on_ws_save_variables({"some_other_var": 1})
        self.assertIsNone(app_state.active_spools["T0"])

    def test_non_integer_value_skipped_without_clearing(self):
        app_state.active_spools["T0"] = 131
        on_ws_save_variables({"t0_spool_id": "not-a-number"})
        self.assertEqual(app_state.active_spools["T0"], 131)

    def test_unchanged_value_does_not_rewrite(self):
        # Sync is idempotent — repeated deltas with the same value are no-ops
        app_state.active_spools["T0"] = 131
        on_ws_save_variables({"t0_spool_id": 131})
        self.assertEqual(app_state.active_spools["T0"], 131)

    def test_non_dict_payload_ignored(self):
        on_ws_save_variables("garbage")  # type: ignore[arg-type]
        self.assertEqual(app_state.active_spools, {})

    def test_no_toolheads_configured_is_noop(self):
        _reset(toolheads=[])
        on_ws_save_variables({"t0_spool_id": 131})
        self.assertEqual(app_state.active_spools, {})

    def test_multi_digit_toolhead_maps_to_full_number(self):
        # T10 must read t10_spool_id, not t0_spool_id (last-char bug in the
        # old file watcher, fixed in the websocket port)
        _reset(toolheads=["T0", "T10"])
        on_ws_save_variables({"t0_spool_id": 1, "t10_spool_id": 2})
        self.assertEqual(app_state.active_spools["T0"], 1)
        self.assertEqual(app_state.active_spools["T10"], 2)

    def test_afc_lane_targets_skipped_entirely(self):
        # Mixed AFC + toolhead configs put lane names in cfg["toolheads"].
        # Lanes have no t<N>_spool_id var — they must be skipped, not cleared,
        # or every unrelated SAVE_VARIABLE would wipe AFC's active_spools slot.
        _reset(toolheads=["T0", "lane1"])
        app_state.active_spools["lane1"] = 42   # set by AFC sync
        on_ws_save_variables({"t0_spool_id": 7})
        self.assertEqual(app_state.active_spools["T0"], 7)
        self.assertEqual(app_state.active_spools["lane1"], 42)

    def test_unrelated_variables_ignored(self):
        # Real save_variables dicts carry offsets, colors, etc — only the
        # t<N>_spool_id keys matter
        on_ws_save_variables({
            "t0_offset_x": 0.42,
            "t0_color": "FF0000",
            "t0_spool_id": 7,
            "current_tool": 0,
        })
        self.assertEqual(app_state.active_spools["T0"], 7)
        # T1 was never set and its var is absent — no key gets created
        self.assertNotIn("T1", app_state.active_spools)


if __name__ == "__main__":
    unittest.main()
