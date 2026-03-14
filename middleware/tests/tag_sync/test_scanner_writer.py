"""
Unit tests for tag_sync/scanner_writer.py

HOW TO RUN:
    From the middleware/ directory:
        python -m pytest tests/tag_sync/test_scanner_writer.py -v
"""

import json
from unittest.mock import Mock

import paho.mqtt.client as mqtt

from tag_sync.policy import TagWritePlan
from tag_sync import scanner_writer


def make_plan(
    device_id="ab12cd",
    uid="04A2B31C5F2280",
    command="update_remaining",
    payload=None,
    reason="tag remaining=742.0g, spoolman remaining=701.0g",
):
    return TagWritePlan(
        device_id=device_id,
        uid=uid,
        command=command,
        payload=payload if payload is not None else {"remaining_g": 701.0},
        reason=reason,
    )


def make_publish_result(rc=mqtt.MQTT_ERR_SUCCESS):
    result = Mock()
    result.rc = rc
    return result


# ---------------------------------------------------------------------------
# Input validation guards
# ---------------------------------------------------------------------------

def test_execute_skips_when_device_id_missing():
    plan = make_plan(device_id="")
    client = Mock()
    scanner_writer.execute(plan, client)
    client.publish.assert_not_called()


def test_execute_skips_when_uid_missing():
    plan = make_plan(uid="")
    client = Mock()
    scanner_writer.execute(plan, client)
    client.publish.assert_not_called()


def test_execute_skips_when_command_missing():
    plan = make_plan(command="")
    client = Mock()
    scanner_writer.execute(plan, client)
    client.publish.assert_not_called()


# ---------------------------------------------------------------------------
# Publish behavior
# ---------------------------------------------------------------------------

def test_execute_publishes_expected_topic_and_payload():
    plan = make_plan()
    client = Mock()
    client.publish.return_value = make_publish_result()
    scanner_writer.execute(plan, client)
    client.publish.assert_called_once()
    topic, payload = client.publish.call_args.args[:2]
    kwargs = client.publish.call_args.kwargs
    assert topic == "openprinttag/ab12cd/cmd/update_remaining/04A2B31C5F2280"
    assert json.loads(payload) == {"remaining_g": 701.0}
    assert kwargs["qos"] == 1


def test_execute_logs_info_when_publish_succeeds(caplog):
    plan = make_plan()
    client = Mock()
    client.publish.return_value = make_publish_result()
    with caplog.at_level("INFO"):
        scanner_writer.execute(plan, client)
    assert "Tag write published:" in caplog.text
    assert "reason=tag remaining=742.0g, spoolman remaining=701.0g" in caplog.text


def test_execute_logs_warning_when_publish_rc_is_not_success(caplog):
    plan = make_plan()
    client = Mock()
    client.publish.return_value = make_publish_result(rc=mqtt.MQTT_ERR_NO_CONN)
    with caplog.at_level("WARNING"):
        scanner_writer.execute(plan, client)
    client.publish.assert_called_once()
    assert "Tag write publish failed" in caplog.text


def test_execute_handles_publish_exception_without_raising(caplog):
    plan = make_plan()
    client = Mock()
    client.publish.side_effect = RuntimeError("boom")
    with caplog.at_level("ERROR"):
        scanner_writer.execute(plan, client)
    client.publish.assert_called_once()
    assert "Tag write failed (non-blocking)" in caplog.text


def test_execute_does_not_include_reason_in_payload():
    plan = make_plan(reason="custom reason")
    client = Mock()
    client.publish.return_value = make_publish_result()
    scanner_writer.execute(plan, client)
    topic, payload = client.publish.call_args.args[:2]
    assert topic == "openprinttag/ab12cd/cmd/update_remaining/04A2B31C5F2280"
    assert json.loads(payload) == {"remaining_g": 701.0}
    assert "custom reason" not in payload
