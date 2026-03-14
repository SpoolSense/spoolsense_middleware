"""
Phase 1 Tag Writeback — Dry-run Path Test
==========================================
Exercises build_write_plan() and device_id extraction logic without
any hardware, MQTT broker, or Spoolman instance.

HOW TO RUN:
    From the middleware/ directory:
        python test_phase1_writeback.py

WHAT TO CHECK:
    - Cases 1, 4, 5 → no write plan (None returned)
    - Cases 2, 3    → write plan returned with correct payload and reason
"""

import json
import logging
logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(message)s")

from tag_sync.policy import build_write_plan, should_write_remaining
from state.models import ScanEvent, SpoolInfo

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_scan(uid="04A2B31C5F2280", remaining_weight_g=None):
    return ScanEvent(
        source="openprinttag_scanner",
        target_id="T0",
        scanned_at="2026-03-14T00:00:00+00:00",
        uid=uid,
        present=True,
        tag_data_valid=True,
        brand_name="Prusament",
        material_type="PLA",
        material_name="Galaxy Black",
        color_hex="1A1A2E",
        full_weight_g=1000.0,
        remaining_weight_g=remaining_weight_g,
    )

def make_spool_info(remaining_weight_g=None):
    return SpoolInfo(
        spool_uid="04A2B31C5F2280",
        source="merged (tag preferred)",
        spoolman_id=42,
        remaining_weight_g=remaining_weight_g,
    )

def extract_device_id(topic, prefix="openprinttag"):
    """Mirror of the extraction logic in spoolsense.py _handle_rich_tag."""
    parts = topic.split("/") if topic else []
    if len(parts) >= 4 and parts[0] == prefix and parts[2] == "tag" and parts[3] == "state":
        return parts[1]
    return None

def run_case(label, scan, spool_info, device_id, expect_plan):
    print(f"\n--- {label} ---")
    plan = build_write_plan(scan, spool_info, device_id=device_id)
    if plan:
        print(f"  write plan: command={plan.command} payload={plan.payload} reason={plan.reason}")
    else:
        print(f"  write plan: None")

    ok = (plan is not None) == expect_plan
    print(f"  {'PASS' if ok else 'FAIL'} (expected {'plan' if expect_plan else 'None'})")
    return ok

# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------

results = []

# Case 1: Fresh tag — equal remaining → no write
results.append(run_case(
    label="Case 1: fresh tag, equal remaining → no write",
    scan=make_scan(remaining_weight_g=742.0),
    spool_info=make_spool_info(remaining_weight_g=742.0),
    device_id="esp32-t0",
    expect_plan=False,
))

# Case 2: Stale tag — Spoolman lower → would-write
results.append(run_case(
    label="Case 2: stale tag, Spoolman lower → would-write",
    scan=make_scan(remaining_weight_g=742.0),
    spool_info=make_spool_info(remaining_weight_g=701.0),
    device_id="esp32-t0",
    expect_plan=True,
))

# Case 3: Tag missing remaining — Spoolman has value → would-write
results.append(run_case(
    label="Case 3: tag missing remaining, Spoolman has value → would-write",
    scan=make_scan(remaining_weight_g=None),
    spool_info=make_spool_info(remaining_weight_g=701.0),
    device_id="esp32-t0",
    expect_plan=True,
))

# Case 4: PN532 / non-writer path — no device_id → no write
results.append(run_case(
    label="Case 4: PN532 path, no device_id → no write",
    scan=make_scan(remaining_weight_g=742.0),
    spool_info=make_spool_info(remaining_weight_g=701.0),
    device_id=None,
    expect_plan=False,
))

# Case 5: Malformed topic — device_id extraction fails → no write
print("\n--- Case 5: malformed topic → no device_id extracted ---")
malformed_topics = [
    "nfc/toolhead/T0",                        # PN532 topic
    "openprinttag/esp32-t0/tag",              # missing /state
    "openprinttag/esp32-t0",                  # too short
    "",                                        # empty
    "openprinttag/esp32-t0/attributes/state", # wrong structure
]
case5_pass = True
for t in malformed_topics:
    device_id = extract_device_id(t)
    ok = device_id is None
    if not ok:
        case5_pass = False
    print(f"  {'PASS' if ok else 'FAIL'} topic={repr(t)!s:<50} → device_id={device_id!r}")

# Also confirm the valid topic extracts correctly
valid_topic = "openprinttag/esp32-t0/tag/state"
device_id = extract_device_id(valid_topic)
ok = device_id == "esp32-t0"
if not ok:
    case5_pass = False
print(f"  {'PASS' if ok else 'FAIL'} topic={repr(valid_topic)!s:<50} → device_id={device_id!r} (expected 'esp32-t0')")
results.append(case5_pass)

# ---------------------------------------------------------------------------
# Spoolman offline path — spool_info=None, stale tag → would-write
# ---------------------------------------------------------------------------
results.append(run_case(
    label="Bonus: Spoolman offline (spool_info=None), tag has remaining → no write (no authoritative value)",
    scan=make_scan(remaining_weight_g=742.0),
    spool_info=None,
    device_id="esp32-t0",
    expect_plan=False,
))

results.append(run_case(
    label="Bonus: Spoolman offline (spool_info=None), tag missing remaining → no write",
    scan=make_scan(remaining_weight_g=None),
    spool_info=None,
    device_id="esp32-t0",
    expect_plan=False,
))

# Negative remaining_g from Spoolman — guard should reject
results.append(run_case(
    label="Bonus: Spoolman returns negative remaining → guard rejects, no write",
    scan=make_scan(remaining_weight_g=742.0),
    spool_info=make_spool_info(remaining_weight_g=-5.0),
    device_id="esp32-t0",
    expect_plan=False,
))

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
print(f"\n{'='*50}")
passed = sum(results)
total = len(results)
print(f"{'ALL PASSED' if passed == total else 'FAILURES DETECTED'} ({passed}/{total})")
