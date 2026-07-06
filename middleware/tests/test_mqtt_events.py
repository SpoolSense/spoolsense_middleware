"""Tests for publishers/mqtt_events.py — SpoolEvents republished to MQTT (#93)."""
from __future__ import annotations

import json
import os
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

sys.modules.setdefault("paho", MagicMock())
sys.modules.setdefault("paho.mqtt", MagicMock())
sys.modules.setdefault("paho.mqtt.client", MagicMock())

import app_state  # noqa: E402
from publishers.base import Action, SpoolEvent  # noqa: E402
from publishers.mqtt_events import MqttEventPublisher  # noqa: E402


def _event(**overrides) -> SpoolEvent:
    base = dict(
        spool_id=131,
        action=Action.TOOLHEAD,
        target="T0",
        color="1E00FF",
        material="PLA",
        weight=666.0,
        nozzle_temp_min=190,
        nozzle_temp_max=220,
        bed_temp_min=50,
        bed_temp_max=60,
        scanner_id="4d9620",
        tag_only=False,
    )
    base.update(overrides)
    return SpoolEvent(**base)


def _reset(mqtt_client=True):
    app_state.mqtt_client = MagicMock() if mqtt_client else None
    if mqtt_client:
        result = MagicMock()
        result.rc = 0
        app_state.mqtt_client.publish.return_value = result


class TestEnabled(unittest.TestCase):

    def test_enabled_by_default(self):
        self.assertTrue(MqttEventPublisher({}).enabled({}))

    def test_disabled_via_config(self):
        self.assertFalse(MqttEventPublisher({}).enabled({"publish_events": False}))

    def test_is_secondary(self):
        pub = MqttEventPublisher({})
        self.assertFalse(pub.primary)
        self.assertEqual(pub.name, "mqtt_events")


class TestPublish(unittest.TestCase):

    def setUp(self):
        _reset()
        self.pub = MqttEventPublisher({})

    def _published(self):
        args, kwargs = app_state.mqtt_client.publish.call_args
        return args[0], json.loads(args[1]), kwargs

    def test_topic_is_per_action(self):
        self.assertTrue(self.pub.publish(_event(action=Action.TOOLHEAD)))
        topic, _, _ = self._published()
        self.assertEqual(topic, "spoolsense/events/toolhead")

        self.pub.publish(_event(action=Action.AFC_STAGE, target=""))
        topic, _, _ = self._published()
        self.assertEqual(topic, "spoolsense/events/afc_stage")

        self.pub.publish(_event(action=Action.HAPPY_HARE_STAGE, target=""))
        topic, _, _ = self._published()
        self.assertEqual(topic, "spoolsense/events/happy_hare_stage")

    def test_payload_carries_full_event_plus_ts(self):
        self.pub.publish(_event())
        _, payload, _ = self._published()
        self.assertEqual(payload["spool_id"], 131)
        self.assertEqual(payload["action"], "toolhead")   # plain string, not enum repr
        self.assertEqual(payload["target"], "T0")
        self.assertEqual(payload["color"], "1E00FF")
        self.assertEqual(payload["material"], "PLA")
        self.assertEqual(payload["weight"], 666.0)
        self.assertEqual(payload["scanner_id"], "4d9620")
        self.assertFalse(payload["tag_only"])
        # ISO-8601 UTC timestamp
        self.assertIn("ts", payload)
        self.assertIn("T", payload["ts"])
        self.assertTrue(payload["ts"].endswith("+00:00") or payload["ts"].endswith("Z"))

    def test_qos1_not_retained(self):
        self.pub.publish(_event())
        _, _, kwargs = self._published()
        self.assertEqual(kwargs.get("qos"), 1)
        self.assertFalse(kwargs.get("retain", False))

    def test_tag_only_event_publishes(self):
        self.assertTrue(self.pub.publish(_event(spool_id=None, tag_only=True)))
        _, payload, _ = self._published()
        self.assertIsNone(payload["spool_id"])
        self.assertTrue(payload["tag_only"])

    def test_no_mqtt_client_returns_false_without_raising(self):
        _reset(mqtt_client=False)
        self.assertFalse(self.pub.publish(_event()))

    def test_publish_exception_returns_false(self):
        app_state.mqtt_client.publish.side_effect = Exception("broker gone")
        self.assertFalse(self.pub.publish(_event()))

    def test_nonzero_rc_returns_false(self):
        result = MagicMock()
        result.rc = 4
        app_state.mqtt_client.publish.return_value = result
        self.assertFalse(self.pub.publish(_event()))


if __name__ == "__main__":
    unittest.main()
