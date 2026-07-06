"""
publishers/mqtt_events.py — SpoolEvents republished to MQTT (#93).

Every SpoolEvent that flows through PublisherManager is published as JSON
to `spoolsense/events/<action>` (QoS 1, not retained — these are events,
not state; the health topic covers state). Home Assistant and anything
else on the broker get automation hooks for scans, activations, and
staging with no HA-specific code here.

Wildcard-subscribe `spoolsense/events/#` to see everything.

Secondary publisher: failures are logged by PublisherManager and never
block activation. Enabled by default; set `publish_events: false` in
config.yaml to turn it off.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict
from datetime import datetime, timezone

import app_state
from publishers.base import Publisher, SpoolEvent

logger = logging.getLogger(__name__)

EVENT_TOPIC_PREFIX = "spoolsense/events"


class MqttEventPublisher(Publisher):
    """Republishes SpoolEvents to the MQTT broker for external consumers."""

    def __init__(self, config: dict) -> None:
        self._config = config

    @property
    def name(self) -> str:
        return "mqtt_events"

    @property
    def primary(self) -> bool:
        return False

    def enabled(self, config: dict) -> bool:
        """On by default — emitting an event is inert. `publish_events: false` disables."""
        return bool(config.get("publish_events", True))

    def publish(self, event: SpoolEvent) -> bool:
        """Publish the event as JSON. Never raises; returns False on failure."""
        client = app_state.mqtt_client
        if client is None:
            logger.debug("mqtt_events: no MQTT client yet — dropping event")
            return False

        payload = asdict(event)
        # Action is a str-mixin Enum; be explicit so the JSON value and topic
        # segment are the plain string on every Python version.
        payload["action"] = event.action.value
        payload["ts"] = datetime.now(timezone.utc).isoformat()

        topic = f"{EVENT_TOPIC_PREFIX}/{event.action.value}"
        try:
            result = client.publish(topic, json.dumps(payload), qos=1)
            if result.rc != 0:
                logger.warning("mqtt_events: publish failed (rc=%d) topic=%s", result.rc, topic)
                return False
            return True
        except Exception:
            logger.exception("mqtt_events: failed to publish to %s", topic)
            return False

    def teardown(self) -> None:
        """Stateless — nothing to tear down."""
