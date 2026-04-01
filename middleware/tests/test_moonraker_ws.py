"""Tests for MoonrakerWebsocket — callback dispatch and state normalization."""
from __future__ import annotations

import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "middleware"))


class TestMoonrakerWebsocket(unittest.TestCase):

    def setUp(self):
        from moonraker_ws import MoonrakerWebsocket
        self.ws = MoonrakerWebsocket("ws://localhost:7125/websocket")

    def test_lane_callback_dispatched(self):
        """Websocket update for AFC_stepper dispatches to lane callback."""
        received = []
        self.ws.on_lane_update = lambda lane, data: received.append((lane, data))

        msg = json.dumps({
            "method": "notify_status_update",
            "params": [{
                "AFC_stepper lane1": {"spool_id": 42, "load": True}
            }, 12345.0]
        })
        self.ws._on_message(None, msg)

        self.assertEqual(len(received), 1)
        self.assertEqual(received[0][0], "lane1")
        self.assertEqual(received[0][1]["spool_id"], 42)
        self.assertEqual(received[0][1]["load"], True)

    def test_assign_spool_callback_dispatched(self):
        """Websocket update for ASSIGN_SPOOL macro dispatches to assign callback."""
        received = []
        self.ws.on_assign_spool = lambda tool: received.append(tool)

        msg = json.dumps({
            "method": "notify_status_update",
            "params": [{
                "gcode_macro ASSIGN_SPOOL": {"pending_tool": "T5"}
            }, 12345.0]
        })
        self.ws._on_message(None, msg)

        self.assertEqual(len(received), 1)
        self.assertEqual(received[0], "T5")

    def test_no_callback_when_not_registered(self):
        """No crash when callbacks are not registered."""
        msg = json.dumps({
            "method": "notify_status_update",
            "params": [{
                "AFC_stepper lane2": {"spool_id": None}
            }, 12345.0]
        })
        self.ws._on_message(None, msg)

    def test_initial_state_dispatched(self):
        """Initial subscription response dispatches full state."""
        received_lanes = []
        received_assigns = []
        self.ws.on_lane_update = lambda lane, data: received_lanes.append((lane, data))
        self.ws.on_assign_spool = lambda tool: received_assigns.append(tool)

        self.ws._subscribe_id = 1
        msg = json.dumps({
            "id": 1,
            "result": {
                "status": {
                    "AFC_stepper lane1": {"spool_id": 10, "load": False},
                    "AFC_stepper lane2": {"spool_id": None, "load": False},
                    "gcode_macro ASSIGN_SPOOL": {"pending_tool": ""}
                }
            }
        })
        self.ws._on_message(None, msg)

        self.assertEqual(len(received_lanes), 2)
        self.assertEqual(received_assigns, [""])

    def test_non_status_messages_ignored(self):
        """Messages without notify_status_update are silently ignored."""
        received = []
        self.ws.on_lane_update = lambda lane, data: received.append(lane)

        msg = json.dumps({"method": "notify_gcode_response", "params": ["ok"]})
        self.ws._on_message(None, msg)
        self.assertEqual(len(received), 0)

    def test_build_subscribe_objects(self):
        """Subscribe objects built from lane names."""
        self.ws.set_lane_names(["lane1", "lane2", "lane3"])
        objects = self.ws._build_subscribe_objects()

        self.assertIn("AFC_stepper lane1", objects)
        self.assertIn("AFC_stepper lane2", objects)
        self.assertIn("AFC_stepper lane3", objects)
        self.assertIn("gcode_macro ASSIGN_SPOOL", objects)
        self.assertEqual(len(objects), 4)
        for v in objects.values():
            self.assertIsNone(v)


if __name__ == "__main__":
    unittest.main()
