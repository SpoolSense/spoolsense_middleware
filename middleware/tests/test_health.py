"""Tests for health.py — edge-triggered service health on a retained MQTT topic (#41)."""
from __future__ import annotations

import json
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
import health  # noqa: E402
from health import STATUS_TOPIC, publish_current, set_health  # noqa: E402


def _reset(mqtt_client=True):
    app_state.state_lock = threading.Lock()
    app_state.service_health = {
        "mqtt": "unknown",
        "moonraker": "unknown",
        "spoolman": "unknown",
        "klipper": "unknown",
    }
    app_state.mqtt_client = MagicMock() if mqtt_client else None
    health._reset_for_testing()


def _published_payloads():
    return [
        json.loads(call.args[1])
        for call in app_state.mqtt_client.publish.call_args_list
        if call.args[0] == STATUS_TOPIC
    ]


class TestSetHealth(unittest.TestCase):

    def setUp(self):
        _reset()

    def test_transition_publishes_full_snapshot(self):
        set_health("moonraker", "connected")
        payloads = _published_payloads()
        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0], {
            "mqtt": "unknown",
            "moonraker": "connected",
            "spoolman": "unknown",
            "klipper": "unknown",
        })

    def test_publishes_retained_qos1(self):
        set_health("moonraker", "connected")
        kwargs = app_state.mqtt_client.publish.call_args.kwargs
        self.assertTrue(kwargs["retain"])
        self.assertEqual(kwargs["qos"], 1)

    def test_no_change_no_publish(self):
        # Poll loops report the same state every cycle — only the first
        # transition may publish
        set_health("moonraker", "connected")
        set_health("moonraker", "connected")
        set_health("moonraker", "connected")
        self.assertEqual(len(_published_payloads()), 1)

    def test_flap_publishes_each_transition(self):
        set_health("moonraker", "connected")
        set_health("moonraker", "unreachable")
        set_health("moonraker", "connected")
        self.assertEqual(len(_published_payloads()), 3)

    def test_multiple_services_each_publish(self):
        set_health("moonraker", "connected")
        set_health("klipper", "ready")
        set_health("spoolman", "unreachable")
        payloads = _published_payloads()
        self.assertEqual(len(payloads), 3)
        self.assertEqual(payloads[-1], {
            "mqtt": "unknown",
            "moonraker": "connected",
            "spoolman": "unreachable",
            "klipper": "ready",
        })

    def test_unknown_service_ignored(self):
        set_health("nonsense", "connected")
        self.assertEqual(len(_published_payloads()), 0)
        self.assertNotIn("nonsense", app_state.service_health)

    def test_no_mqtt_client_no_crash(self):
        _reset(mqtt_client=False)
        set_health("moonraker", "connected")  # must not raise
        self.assertEqual(app_state.service_health["moonraker"], "connected")

    def test_publish_failure_does_not_raise(self):
        app_state.mqtt_client.publish.side_effect = Exception("broker gone")
        set_health("moonraker", "connected")  # must not raise
        self.assertEqual(app_state.service_health["moonraker"], "connected")


class TestPublishCurrent(unittest.TestCase):

    def setUp(self):
        _reset()

    def test_publishes_unconditionally(self):
        publish_current()
        publish_current()
        self.assertEqual(len(_published_payloads()), 2)

    def test_set_health_after_publish_current_dedups(self):
        # publish_current records the snapshot; a redundant set_health
        # must not double-publish
        set_health("mqtt", "connected")
        publish_current()
        set_health("mqtt", "connected")
        self.assertEqual(len(_published_payloads()), 2)


class TestMoonrakerWsWiring(unittest.TestCase):
    """Klippy lifecycle events must drive the klipper health state."""

    def setUp(self):
        _reset(mqtt_client=False)
        from moonraker_ws import MoonrakerWebsocket
        self.ws = MoonrakerWebsocket("ws://localhost:7125/websocket")

    def test_klippy_ready_sets_health(self):
        self.ws._on_message(MagicMock(), json.dumps({"method": "notify_klippy_ready"}))
        self.assertEqual(app_state.service_health["klipper"], "ready")

    def test_klippy_disconnected_sets_health(self):
        self.ws._on_message(MagicMock(), json.dumps({"method": "notify_klippy_disconnected"}))
        self.assertEqual(app_state.service_health["klipper"], "disconnected")

    def test_subscribe_response_sets_klipper_ready(self):
        # Normal startup: notify_klippy_ready never fires, but a successful
        # subscription is positive evidence Klipper is up.
        self.ws._subscribe_id = 5
        self.ws._on_message(MagicMock(), json.dumps({"id": 5, "result": {"status": {}}}))
        self.assertEqual(app_state.service_health["klipper"], "ready")


if __name__ == "__main__":
    unittest.main()
