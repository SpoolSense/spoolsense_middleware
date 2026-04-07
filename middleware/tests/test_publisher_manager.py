"""
Tests for publisher_manager.py — fan-out dispatch, primary/secondary routing,
error isolation, and disabled-publisher filtering.
"""
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
from publishers.base import Action, SpoolEvent  # noqa: E402
from publisher_manager import PublisherManager  # noqa: E402


def _reset_app_state() -> None:
    app_state.cfg = {
        "moonraker_url": "http://moonraker:7125",
        "low_spool_threshold": 100,
    }
    app_state.lane_locks = {}
    app_state.active_spools = {}
    app_state.pending_spool = None
    app_state.state_lock = threading.Lock()


def _make_event(**kwargs) -> SpoolEvent:
    """Minimal valid SpoolEvent for routing tests — concrete values don't matter here."""
    defaults = dict(
        spool_id=1,
        action=Action.AFC_STAGE,
        target="lane1",
        color="FF0000",
        material="PLA",
        weight=250.0,
        nozzle_temp_min=200,
        nozzle_temp_max=230,
        bed_temp_min=60,
        bed_temp_max=70,
        scanner_id="ecb338",
        tag_only=False,
    )
    defaults.update(kwargs)
    return SpoolEvent(**defaults)


def _make_publisher(name: str, primary: bool, enabled: bool, result: bool) -> MagicMock:
    """Build a mock Publisher — enabled() and publish() return fixed values."""
    pub = MagicMock()
    pub.name = name
    pub.primary = primary
    pub.enabled = MagicMock(return_value=enabled)
    pub.publish = MagicMock(return_value=result)
    return pub


class TestPublisherManagerRegister(unittest.TestCase):

    def setUp(self):
        _reset_app_state()

    def test_enabled_publisher_is_added(self):
        mgr = PublisherManager()
        pub = _make_publisher("klipper", primary=True, enabled=True, result=True)
        mgr.register(pub)
        # Publisher appeared in the registry — publish() must reach it
        event = _make_event()
        mgr.publish(event)
        pub.publish.assert_called_once_with(event)

    def test_disabled_publisher_is_not_added(self):
        # Publishers that report disabled at registration time must never receive events
        mgr = PublisherManager()
        pub = _make_publisher("lane_data", primary=False, enabled=False, result=True)
        mgr.register(pub)
        mgr.publish(_make_event())
        pub.publish.assert_not_called()


class TestPublisherManagerPublish(unittest.TestCase):

    def setUp(self):
        _reset_app_state()

    def test_no_primary_returns_true(self):
        # No primary publisher registered — orchestrator should always proceed
        mgr = PublisherManager()
        pub = _make_publisher("mqtt", primary=False, enabled=True, result=False)
        mgr.register(pub)
        result = mgr.publish(_make_event())
        self.assertTrue(result)

    def test_primary_success_returns_true(self):
        mgr = PublisherManager()
        mgr.register(_make_publisher("klipper", primary=True, enabled=True, result=True))
        self.assertTrue(mgr.publish(_make_event()))

    def test_primary_failure_returns_false(self):
        mgr = PublisherManager()
        mgr.register(_make_publisher("klipper", primary=True, enabled=True, result=False))
        self.assertFalse(mgr.publish(_make_event()))

    def test_fanout_reaches_all_publishers(self):
        # All registered publishers must receive the same event
        mgr = PublisherManager()
        primary = _make_publisher("klipper", primary=True, enabled=True, result=True)
        secondary = _make_publisher("mqtt", primary=False, enabled=True, result=True)
        mgr.register(primary)
        mgr.register(secondary)
        event = _make_event()
        mgr.publish(event)
        primary.publish.assert_called_once_with(event)
        secondary.publish.assert_called_once_with(event)

    def test_secondary_failure_does_not_affect_return(self):
        # A failing secondary publisher must not flip the return value to False
        mgr = PublisherManager()
        mgr.register(_make_publisher("klipper", primary=True, enabled=True, result=True))
        mgr.register(_make_publisher("lane_data", primary=False, enabled=True, result=False))
        self.assertTrue(mgr.publish(_make_event()))

    def test_primary_exception_returns_false(self):
        # An exception from the primary counts as failure — activation must not proceed
        mgr = PublisherManager()
        pub = _make_publisher("klipper", primary=True, enabled=True, result=True)
        pub.publish.side_effect = RuntimeError("moonraker down")
        mgr.register(pub)
        self.assertFalse(mgr.publish(_make_event()))

    def test_secondary_exception_does_not_propagate(self):
        # Secondary publisher crashing must not raise out of publish()
        mgr = PublisherManager()
        mgr.register(_make_publisher("klipper", primary=True, enabled=True, result=True))
        bad = _make_publisher("mqtt", primary=False, enabled=True, result=True)
        bad.publish.side_effect = RuntimeError("connection lost")
        mgr.register(bad)
        # Must not raise and must still return True from the primary
        result = mgr.publish(_make_event())
        self.assertTrue(result)

    def test_secondary_exception_does_not_block_other_secondaries(self):
        # Error isolation: one bad secondary must not prevent the next one from running
        mgr = PublisherManager()
        mgr.register(_make_publisher("klipper", primary=True, enabled=True, result=True))
        bad = _make_publisher("bad_secondary", primary=False, enabled=True, result=True)
        bad.publish.side_effect = RuntimeError("explode")
        good = _make_publisher("lane_data", primary=False, enabled=True, result=True)
        mgr.register(bad)
        mgr.register(good)
        event = _make_event()
        mgr.publish(event)
        good.publish.assert_called_once_with(event)

    def test_empty_registry_returns_true(self):
        mgr = PublisherManager()
        self.assertTrue(mgr.publish(_make_event()))

    def test_multiple_primaries_all_must_succeed(self):
        # Edge case: two primaries — both must succeed for the call to return True
        mgr = PublisherManager()
        mgr.register(_make_publisher("klipper", primary=True, enabled=True, result=True))
        mgr.register(_make_publisher("klipper2", primary=True, enabled=True, result=False))
        self.assertFalse(mgr.publish(_make_event()))


class TestPublisherManagerShutdown(unittest.TestCase):

    def setUp(self):
        _reset_app_state()

    def test_shutdown_calls_teardown_on_all_publishers(self):
        mgr = PublisherManager()
        pub_a = _make_publisher("klipper", primary=True, enabled=True, result=True)
        pub_b = _make_publisher("mqtt", primary=False, enabled=True, result=True)
        mgr.register(pub_a)
        mgr.register(pub_b)
        mgr.shutdown()
        pub_a.teardown.assert_called_once()
        pub_b.teardown.assert_called_once()

    def test_shutdown_teardown_exception_does_not_propagate(self):
        # One publisher failing teardown must not block the rest
        mgr = PublisherManager()
        pub = _make_publisher("klipper", primary=True, enabled=True, result=True)
        pub.teardown.side_effect = RuntimeError("stuck")
        mgr.register(pub)
        # Must not raise
        mgr.shutdown()


if __name__ == "__main__":
    unittest.main()
