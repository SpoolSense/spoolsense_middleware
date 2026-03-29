"""Tests for tag writeback cooldown (issue #21 — write loop prevention)."""

from __future__ import annotations

import os
import sys
import time
import threading
import unittest
from unittest.mock import MagicMock, patch
from dataclasses import dataclass

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

# Stub heavy dependencies, but preserve real tag_sync modules
sys.modules.setdefault("paho", MagicMock())
sys.modules.setdefault("paho.mqtt", MagicMock())
sys.modules.setdefault("paho.mqtt.client", MagicMock())
sys.modules.setdefault("watchdog", MagicMock())
sys.modules.setdefault("watchdog.observers", MagicMock())

# Force-import the real modules (undo any earlier MagicMock stubs)
import importlib
for mod_name in ("app_state", "tag_sync", "tag_sync.policy"):
    if mod_name in sys.modules and isinstance(sys.modules[mod_name], MagicMock):
        del sys.modules[mod_name]

import app_state
from tag_sync.policy import build_write_plan, should_write_remaining, TagWritePlan


@dataclass
class FakeScanEvent:
    uid: str = "04ECA4AB8F6180"
    remaining_weight_g: float | None = 800.0


@dataclass
class FakeSpoolInfo:
    remaining_weight_g: float | None = 500.0


class TestShouldWriteRemaining(unittest.TestCase):
    """Existing logic — ensure cooldown doesn't break base behavior."""

    def test_spoolman_lower_than_tag_writes(self):
        self.assertTrue(should_write_remaining(800.0, 500.0))

    def test_spoolman_equal_to_tag_no_write(self):
        self.assertFalse(should_write_remaining(500.0, 500.0))

    def test_spoolman_higher_than_tag_no_write(self):
        self.assertFalse(should_write_remaining(500.0, 800.0))

    def test_spoolman_none_no_write(self):
        self.assertFalse(should_write_remaining(800.0, None))

    def test_tag_none_writes(self):
        self.assertTrue(should_write_remaining(None, 500.0))


class TestWriteCooldown(unittest.TestCase):
    """Cooldown prevents write loops from our own tag state republishes."""

    def setUp(self):
        # Reset cooldown state before each test
        app_state.tag_write_timestamps.clear()
        app_state.state_lock = threading.Lock()

    def test_no_cooldown_allows_write(self):
        """First write to a UID should always be allowed."""
        scan = FakeScanEvent(uid="AABBCCDD", remaining_weight_g=800.0)
        spool = FakeSpoolInfo(remaining_weight_g=500.0)
        plan = build_write_plan(scan, spool, device_id="abc123")
        self.assertIsNotNone(plan)
        self.assertEqual(plan.uid, "AABBCCDD")

    def test_cooldown_blocks_immediate_rewrite(self):
        """Write within cooldown window should be suppressed."""
        uid = "AABBCCDD"
        # Simulate a recent write
        app_state.tag_write_timestamps[uid] = time.monotonic()

        scan = FakeScanEvent(uid=uid, remaining_weight_g=800.0)
        spool = FakeSpoolInfo(remaining_weight_g=500.0)
        plan = build_write_plan(scan, spool, device_id="abc123")
        self.assertIsNone(plan)

    def test_cooldown_expires_allows_write(self):
        """Write after cooldown expires should be allowed."""
        uid = "AABBCCDD"
        # Simulate a write that happened long ago
        app_state.tag_write_timestamps[uid] = time.monotonic() - (app_state.WRITE_COOLDOWN_SECONDS + 1)

        scan = FakeScanEvent(uid=uid, remaining_weight_g=800.0)
        spool = FakeSpoolInfo(remaining_weight_g=500.0)
        plan = build_write_plan(scan, spool, device_id="abc123")
        self.assertIsNotNone(plan)

    def test_cooldown_per_uid(self):
        """Cooldown on one UID does not block writes to a different UID."""
        # UID A is in cooldown
        app_state.tag_write_timestamps["AAAA"] = time.monotonic()

        # UID B should still work
        scan = FakeScanEvent(uid="BBBB", remaining_weight_g=800.0)
        spool = FakeSpoolInfo(remaining_weight_g=500.0)
        plan = build_write_plan(scan, spool, device_id="abc123")
        self.assertIsNotNone(plan)
        self.assertEqual(plan.uid, "BBBB")

    def test_no_device_id_returns_none(self):
        """No device_id means no writeback (unchanged behavior)."""
        scan = FakeScanEvent(uid="AABBCCDD", remaining_weight_g=800.0)
        spool = FakeSpoolInfo(remaining_weight_g=500.0)
        plan = build_write_plan(scan, spool, device_id=None)
        self.assertIsNone(plan)

    def test_no_uid_returns_none(self):
        """No UID means no writeback (unchanged behavior)."""
        scan = FakeScanEvent(uid=None, remaining_weight_g=800.0)
        spool = FakeSpoolInfo(remaining_weight_g=500.0)
        plan = build_write_plan(scan, spool, device_id="abc123")
        self.assertIsNone(plan)

    def test_build_write_plan_claims_slot_on_write(self):
        """build_write_plan records timestamp when a write plan is produced."""
        uid = "CLAIM_TEST"
        scan = FakeScanEvent(uid=uid, remaining_weight_g=800.0)
        spool = FakeSpoolInfo(remaining_weight_g=500.0)
        plan = build_write_plan(scan, spool, device_id="abc123")
        self.assertIsNotNone(plan)
        self.assertIn(uid, app_state.tag_write_timestamps)

    def test_no_claim_when_write_not_needed(self):
        """build_write_plan does NOT burn cooldown when no write is needed."""
        uid = "NO_WRITE"
        scan = FakeScanEvent(uid=uid, remaining_weight_g=500.0)
        spool = FakeSpoolInfo(remaining_weight_g=500.0)  # equal — no write
        plan = build_write_plan(scan, spool, device_id="abc123")
        self.assertIsNone(plan)
        self.assertNotIn(uid, app_state.tag_write_timestamps)

    def test_pruning_removes_expired_entries(self):
        """Lazy pruning clears expired UIDs when dict exceeds threshold."""
        now = time.monotonic()
        expired_time = now - (app_state.WRITE_COOLDOWN_SECONDS + 5)
        # Fill with 60 expired entries to trigger pruning (threshold > 50)
        for i in range(60):
            app_state.tag_write_timestamps[f"OLD_{i}"] = expired_time

        # New write should trigger pruning
        scan = FakeScanEvent(uid="NEW_UID", remaining_weight_g=800.0)
        spool = FakeSpoolInfo(remaining_weight_g=500.0)
        build_write_plan(scan, spool, device_id="abc123")

        # Expired entries should be pruned, only NEW_UID remains
        self.assertIn("NEW_UID", app_state.tag_write_timestamps)
        self.assertNotIn("OLD_0", app_state.tag_write_timestamps)
        self.assertLessEqual(len(app_state.tag_write_timestamps), 2)


class TestScannerWriterTimestamp(unittest.TestCase):
    """Verify scanner_writer.execute records timestamps correctly."""

    def setUp(self):
        app_state.tag_write_timestamps.clear()
        app_state.state_lock = threading.Lock()

    def _get_execute_and_mqtt(self):
        """Import scanner_writer with paho.mqtt.client.MQTT_ERR_SUCCESS patched."""
        # Set the constant on the mock before (re)importing
        paho_mock = sys.modules.get("paho.mqtt.client")
        if paho_mock:
            paho_mock.MQTT_ERR_SUCCESS = 0
        if "tag_sync.scanner_writer" in sys.modules:
            del sys.modules["tag_sync.scanner_writer"]
        import tag_sync.scanner_writer as sw
        # Patch the module-level mqtt reference so the comparison works
        sw.mqtt.MQTT_ERR_SUCCESS = 0
        return sw.execute

    def test_successful_publish_refreshes_timestamp(self):
        """execute() refreshes the cooldown timestamp on successful publish."""
        execute = self._get_execute_and_mqtt()

        uid = "WRITER_TEST"
        plan = TagWritePlan(device_id="dev1", uid=uid, command="update_remaining",
                            payload={"remaining_g": 500.0})

        mock_client = MagicMock()
        mock_result = MagicMock()
        mock_result.rc = 0
        mock_client.publish.return_value = mock_result

        execute(plan, mock_client)
        self.assertIn(uid, app_state.tag_write_timestamps)

    def test_failed_publish_releases_cooldown_claim(self):
        """execute() clears cooldown on publish failure so retry isn't blocked."""
        execute = self._get_execute_and_mqtt()

        uid = "FAIL_TEST"
        # Simulate optimistic claim from build_write_plan
        app_state.tag_write_timestamps[uid] = time.monotonic()

        plan = TagWritePlan(device_id="dev1", uid=uid, command="update_remaining",
                            payload={"remaining_g": 500.0})

        mock_client = MagicMock()
        mock_result = MagicMock()
        mock_result.rc = 4  # Non-zero = failure
        mock_client.publish.return_value = mock_result

        execute(plan, mock_client)
        # Claim should be released so a retry can proceed
        self.assertNotIn(uid, app_state.tag_write_timestamps)

    def test_exception_releases_cooldown_claim(self):
        """execute() clears cooldown on exception so retry isn't blocked."""
        execute = self._get_execute_and_mqtt()

        uid = "EXCEPTION_TEST"
        # Simulate optimistic claim from build_write_plan
        app_state.tag_write_timestamps[uid] = time.monotonic()

        plan = TagWritePlan(device_id="dev1", uid=uid, command="update_remaining",
                            payload={"remaining_g": 500.0})

        mock_client = MagicMock()
        mock_client.publish.side_effect = OSError("connection lost")

        execute(plan, mock_client)
        # Claim should be released so a retry can proceed
        self.assertNotIn(uid, app_state.tag_write_timestamps)


if __name__ == "__main__":
    unittest.main()
