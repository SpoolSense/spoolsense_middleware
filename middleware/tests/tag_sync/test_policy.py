"""
Unit tests for tag_sync/policy.py

HOW TO RUN:
    From the middleware/ directory:
        python -m pytest tests/
    Or to run this file specifically:
        python -m pytest tests/tag_sync/test_policy.py -v
"""

from types import SimpleNamespace

from tag_sync.policy import (
    TagWritePlan,
    build_write_plan,
    should_write_remaining,
)


def make_scan(uid="04A2B31C5F2280", remaining_weight_g=742.0):
    return SimpleNamespace(
        uid=uid,
        remaining_weight_g=remaining_weight_g,
    )


def make_spool(remaining_weight_g=742.0):
    return SimpleNamespace(
        remaining_weight_g=remaining_weight_g,
    )


# ---------------------------------------------------------------------------
# should_write_remaining
# ---------------------------------------------------------------------------

def test_should_write_remaining_false_when_spoolman_missing():
    assert should_write_remaining(tag_remaining=742.0, spoolman_remaining=None) is False


def test_should_write_remaining_true_when_tag_missing():
    assert should_write_remaining(tag_remaining=None, spoolman_remaining=701.0) is True


def test_should_write_remaining_true_when_tag_is_higher_than_spoolman():
    assert should_write_remaining(tag_remaining=742.0, spoolman_remaining=701.0) is True


def test_should_write_remaining_false_when_equal():
    assert should_write_remaining(tag_remaining=701.0, spoolman_remaining=701.0) is False


def test_should_write_remaining_false_when_spoolman_is_higher():
    assert should_write_remaining(tag_remaining=701.0, spoolman_remaining=742.0) is False


# ---------------------------------------------------------------------------
# build_write_plan
# ---------------------------------------------------------------------------

def test_build_write_plan_returns_none_when_device_id_missing():
    scan = make_scan(uid="04A2B31C5F2280", remaining_weight_g=742.0)
    spool = make_spool(remaining_weight_g=701.0)
    plan = build_write_plan(scan, spool, device_id=None)
    assert plan is None


def test_build_write_plan_returns_none_when_uid_missing():
    scan = make_scan(uid=None, remaining_weight_g=742.0)
    spool = make_spool(remaining_weight_g=701.0)
    plan = build_write_plan(scan, spool, device_id="ab12cd")
    assert plan is None


def test_build_write_plan_returns_none_when_spool_info_missing():
    scan = make_scan(uid="04A2B31C5F2280", remaining_weight_g=742.0)
    plan = build_write_plan(scan, None, device_id="ab12cd")
    assert plan is None


def test_build_write_plan_returns_none_when_tag_is_fresh():
    scan = make_scan(uid="04A2B31C5F2280", remaining_weight_g=701.0)
    spool = make_spool(remaining_weight_g=701.0)
    plan = build_write_plan(scan, spool, device_id="ab12cd")
    assert plan is None


def test_build_write_plan_returns_plan_when_tag_is_stale():
    scan = make_scan(uid="04A2B31C5F2280", remaining_weight_g=742.0)
    spool = make_spool(remaining_weight_g=701.0)
    plan = build_write_plan(scan, spool, device_id="ab12cd")
    assert isinstance(plan, TagWritePlan)
    assert plan.device_id == "ab12cd"
    assert plan.uid == "04A2B31C5F2280"
    assert plan.command == "update_remaining"
    assert plan.payload == {"remaining_g": 701.0}
    assert plan.reason == "tag remaining=742.0g, spoolman remaining=701.0g"


def test_build_write_plan_returns_plan_when_tag_remaining_missing():
    scan = make_scan(uid="04A2B31C5F2280", remaining_weight_g=None)
    spool = make_spool(remaining_weight_g=701.0)
    plan = build_write_plan(scan, spool, device_id="ab12cd")
    assert isinstance(plan, TagWritePlan)
    assert plan.device_id == "ab12cd"
    assert plan.uid == "04A2B31C5F2280"
    assert plan.command == "update_remaining"
    assert plan.payload == {"remaining_g": 701.0}
    assert plan.reason == "tag missing remaining, spoolman remaining=701.0g"


def test_build_write_plan_returns_none_when_spoolman_remaining_is_negative():
    scan = make_scan(uid="04A2B31C5F2280", remaining_weight_g=742.0)
    spool = make_spool(remaining_weight_g=-5.0)
    plan = build_write_plan(scan, spool, device_id="ab12cd")
    assert plan is None
