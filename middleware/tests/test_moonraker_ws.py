"""Tests for MoonrakerWebsocket — callback dispatch and state normalization."""
from __future__ import annotations

import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


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

        # Simulate the subscribe response arriving with the correct ID
        self.ws._subscribe_id = 5
        msg = json.dumps({
            "id": 5,
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

    def test_objects_list_response_discovers_lanes_and_subscribes(self):
        """printer.objects.list response updates lane names and triggers subscription."""
        sent = []
        mock_ws = MagicMock()
        mock_ws.send = lambda msg: sent.append(json.loads(msg))

        self.ws._next_id = 1
        self.ws._list_id = 2
        self.ws._next_id = 2  # simulate ID counter at the list call

        msg = json.dumps({
            "id": 2,
            "result": {
                "objects": [
                    "AFC_stepper lane1",
                    "AFC_stepper lane2",
                    "AFC_stepper lane3",
                    "gcode_macro AFC_RESUME",
                    "toolhead",
                ]
            }
        })
        self.ws._on_message(mock_ws, msg)

        # Lane names should be updated
        self.assertEqual(self.ws._lane_names, ["lane1", "lane2", "lane3"])
        # A subscribe message should have been sent
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["method"], "printer.objects.subscribe")
        self.assertIn("AFC_stepper lane1", sent[0]["params"]["objects"])
        self.assertIn("AFC_stepper lane3", sent[0]["params"]["objects"])

    def test_objects_list_response_no_afc_lanes(self):
        """printer.objects.list with no AFC lanes still triggers subscribe."""
        sent = []
        mock_ws = MagicMock()
        mock_ws.send = lambda msg: sent.append(json.loads(msg))

        self.ws._next_id = 1
        self.ws._list_id = 1

        msg = json.dumps({
            "id": 1,
            "result": {"objects": ["toolhead", "gcode_macro ASSIGN_SPOOL"]}
        })
        self.ws._on_message(mock_ws, msg)

        # Lane names unchanged (empty)
        self.assertEqual(self.ws._lane_names, [])
        # Subscribe still sent (for non-AFC objects)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["method"], "printer.objects.subscribe")

    def test_klippy_ready_triggers_rediscovery(self):
        """notify_klippy_ready sends a new printer.objects.list request."""
        sent = []
        mock_ws = MagicMock()
        mock_ws.send = lambda msg: sent.append(json.loads(msg))

        msg = json.dumps({"method": "notify_klippy_ready"})
        self.ws._on_message(mock_ws, msg)

        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["method"], "printer.objects.list")

    def test_on_open_sends_objects_list(self):
        """_on_open sends printer.objects.list for lane discovery."""
        sent = []
        mock_ws = MagicMock()
        mock_ws.send = lambda msg: sent.append(json.loads(msg))

        self.ws._consecutive_failures = 3
        self.ws._on_open(mock_ws)

        self.assertEqual(self.ws._consecutive_failures, 0)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["method"], "printer.objects.list")

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
        self.assertIn("gcode_macro UPDATE_TAG", objects)
        self.assertEqual(len(objects), 5)
        for v in objects.values():
            self.assertIsNone(v)


if __name__ == "__main__":
    unittest.main()
