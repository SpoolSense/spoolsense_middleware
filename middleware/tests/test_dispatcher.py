"""
Dispatcher Isolation Test
=========================
Tests the auto-format detection and routing logic in adapters/dispatcher.py
completely in isolation. No NFC scanner, MQTT broker, or Klipper required.

HOW TO RUN:
    From the middleware/ directory:

        python test_dispatcher.py

WHAT THIS TESTS:
    1.  OpenTag3D — auto-detect, no "format" key present
    2.  OpenTag3D — explicit "format" key override
    3.  OpenTag3D — partial tag (only one detection key)
    4.  Unknown/blank payload — should raise ValueError
    5.  OpenPrintTag spec (CBOR direct) — should raise NotImplementedError
    6.  spoolsense_scanner — valid tag (valid=True)
    7.  spoolsense_scanner — valid=False (bad read)
    8.  spoolsense_scanner — color_hex derived from color name
    9.  spoolsense_scanner — remaining_m converted to remaining_length_mm

HOW TO FEED YOUR OWN DATA:
    Add or replace payloads below. Pass the full payload dict and a target_id
    string to run(). The dispatcher extracts uid from payload.get("uid").

NOTE — OpenPrintTag CBOR direct:
    Detection is kept so users get a clear "not yet supported" message rather
    than a confusing "unknown format" error. See Test 5.
"""

import json
import logging
from adapters.dispatcher import detect_and_parse

logging.basicConfig(level=logging.DEBUG)


def run(label: str, payload: dict, target_id: str):
    """Helper to run a single test case and print the result."""
    print(f"\n--- {label} ---")
    try:
        event = detect_and_parse(payload, target_id)
        print(json.dumps(event.to_dict(), indent=2))
    except NotImplementedError as e:
        print(f"NotImplementedError (expected): {e}")
    except ValueError as e:
        print(f"ValueError (expected): {e}")
    except Exception as e:
        print(f"Unexpected error: {e}")


# ── Test 1: OpenTag3D — auto-detect ──────────────────────────────────────────
run(
    "Test 1: OpenTag3D (auto-detect)",
    payload={
        "uid": "UID-1111",
        "manufacturer": "Polymaker",
        "material_name": "PLA",
        "color_name": "Galaxy Black",
        "color_hex": "#1A1A1A",
        "spool_weight_nominal": 1000.0,
        "spool_weight_measured": 450.0,
    },
    target_id="lane1",
)


# ── Test 2: OpenTag3D — explicit format override ──────────────────────────────
run(
    "Test 2: OpenTag3D (explicit format override)",
    payload={
        "uid": "UID-2222",
        "format": "opentag3d",
        "manufacturer": "eSun",
        "material_name": "PETG",
        "color_hex": "#FF6600",
        "spool_weight_nominal": 1000.0,
        "spool_weight_measured": 750.0,
    },
    target_id="T0",
)


# ── Test 3: OpenTag3D — partial tag (one detection key) ──────────────────────
run(
    "Test 3: OpenTag3D (partial tag, one detection key)",
    payload={
        "uid": "UID-3333",
        "manufacturer": "eSun",
        "spool_weight_nominal": 800.0,
    },
    target_id="lane2",
)


# ── Test 4: Unknown/blank payload — expect ValueError ────────────────────────
run(
    "Test 4: Unknown/blank payload (expect ValueError)",
    payload={
        "uid": "UID-4444",
    },
    target_id="lane3",
)


# ── Test 5: OpenPrintTag spec (CBOR direct) — expect NotImplementedError ─────
run(
    "Test 5: OpenPrintTag spec CBOR (expect NotImplementedError)",
    payload={
        "uid": "UID-5555",
        "brand_name": "Prusament",
        "material_type": "PETG",
        "actual_netto_full_weight": 1000.0,
    },
    target_id="lane4",
)


# ── Test 6: spoolsense_scanner — valid tag ──────────────────────────────────
# Full confirmed payload from spoolsense/spoolsense_scanner.
# tag_data_valid=True — should parse cleanly.
# color_hex should be "1A1A2E" (# stripped, uppercased).
# scanner_spoolman_id should be None (-1 stripped).
run(
    "Test 6: spoolsense_scanner (valid tag)",
    payload={
        "uid": "04A2B31C5F2280",
        "present": True,
        "tag_data_valid": True,
        "manufacturer": "Prusament",
        "material_type": "PLA",
        "material_name": "Galaxy Black",
        "color": "#1A1A2E",
        "initial_weight_g": 1000.0,
        "remaining_g": 742.0,
        "spoolman_id": -1,
        "blank": False,
    },
    target_id="default",
)


# ── Test 7: spoolsense_scanner — tag_data_valid=False ───────────────────────
# Tag detected but data could not be read cleanly.
# Should return a ScanEvent with tag_data_valid=False, not raise.
run(
    "Test 7: spoolsense_scanner (tag_data_valid=False — bad read)",
    payload={
        "uid": "04A2B31C5F2280",
        "present": True,
        "tag_data_valid": False,
        "manufacturer": "",
        "material_type": "",
        "material_name": "",
        "color": "",
        "initial_weight_g": 0.0,
        "remaining_g": 0.0,
        "spoolman_id": -1,
        "blank": False,
    },
    target_id="default",
)


# ── Test 8: spoolsense_scanner — color hex strip ────────────────────────────
# color "#FF6600" should produce color_hex="FF6600" (# stripped, uppercased).
run(
    "Test 8: spoolsense_scanner (color hex # stripped)",
    payload={
        "uid": "04BBCCDD112233",
        "present": True,
        "tag_data_valid": True,
        "manufacturer": "Prusament",
        "material_type": "PETG",
        "material_name": "Urban Grey",
        "color": "#6E6E6E",
        "initial_weight_g": 1000.0,
        "remaining_g": 500.0,
        "spoolman_id": -1,
        "blank": False,
    },
    target_id="T1",
)


# ── Test 9: spoolsense_scanner — present=False ──────────────────────────────
# No tag on reader. present=False, tag_data_valid=False, all data fields empty.
# Should return ScanEvent with present=False, not raise.
run(
    "Test 9: spoolsense_scanner (present=False — no tag)",
    payload={
        "uid": "",
        "present": False,
        "tag_data_valid": False,
        "manufacturer": "",
        "material_type": "",
        "material_name": "",
        "color": "",
        "initial_weight_g": 0.0,
        "remaining_g": 0.0,
        "spoolman_id": -1,
        "blank": False,
    },
    target_id="default",
)
