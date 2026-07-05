"""
health.py — middleware service health published to MQTT (#41).

Tracks per-service connectivity (mqtt, moonraker, spoolman, klipper) and
publishes a retained JSON snapshot to `spoolsense/middleware/status` on
state transitions only — same edge-triggered philosophy as the low-spool
LED (#61). Retained + QoS 1 means scanners and dashboards get the current
state immediately on subscribe without polling.

Callers report state from wherever they already learn it (poll loops,
websocket callbacks, cache refreshes):

    from health import set_health
    set_health("moonraker", "unreachable")

Values per #41: "connected" / "unreachable" for services,
"ready" / "disconnected" for klipper. "unknown" before the first check.
"""
from __future__ import annotations

import json
import logging

import app_state

logger = logging.getLogger(__name__)

STATUS_TOPIC = "spoolsense/middleware/status"

_SERVICES = ("mqtt", "moonraker", "spoolman", "klipper")

# Last snapshot actually published — compared under state_lock so identical
# transitions from concurrent reporters publish once.
_last_published: dict | None = None


def set_health(service: str, status: str) -> None:
    """
    Record a service's health and publish the full snapshot if anything
    changed since the last publish. Safe from any thread; never raises.
    """
    global _last_published

    if service not in _SERVICES:
        logger.warning("health: unknown service %r ignored", service)
        return

    with app_state.state_lock:
        current = app_state.service_health.get(service)
        if current == status:
            return
        app_state.service_health[service] = status
        snapshot = dict(app_state.service_health)
        if snapshot == _last_published:
            return
        _last_published = snapshot

    _publish(snapshot)


def publish_current() -> None:
    """
    Publish the current snapshot unconditionally (used after MQTT reconnect
    so the retained topic reflects this session's state).
    """
    global _last_published
    with app_state.state_lock:
        snapshot = dict(app_state.service_health)
        _last_published = snapshot
    _publish(snapshot)


def _publish(snapshot: dict) -> None:
    """Publish the snapshot to the retained status topic. Never raises."""
    client = app_state.mqtt_client
    if client is None:
        return
    try:
        client.publish(STATUS_TOPIC, json.dumps(snapshot), qos=1, retain=True)
        logger.info("health: %s", snapshot)
    except Exception:
        logger.exception("health: failed to publish status")


def _reset_for_testing() -> None:
    """Test helper — clears the last-published snapshot."""
    global _last_published
    _last_published = None
