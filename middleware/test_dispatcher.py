"""
Dispatcher Isolation Test
=========================
Tests the auto-format detection and routing logic in adapters/dispatcher.py
completely in isolation. No NFC scanner, MQTT broker, or Klipper required.

HOW TO RUN:
    From the middleware/ directory:

        python test_dispatcher.py

WHAT THIS TESTS:
    1. Auto-detection of OpenTag3D payload (no "format" key present)
    2. Explicit "format" key override — dispatcher trusts it without auto-detecting
    3. Partial tag — only one detection key present, should still route correctly
    4. Blank/unknown tag — no recognizable keys, should raise ValueError gracefully
    5. OpenPrintTag payload — should raise NotImplementedError with a clear message

HOW TO FEED YOUR OWN DATA:
    Replace or add payloads below. The envelope keys "uid" and "toolhead" are
    stripped before the dispatcher sees the data — they are added by the ESP32,
    not the tag. Everything else in the dict is treated as tag data.

NOTE — OpenPrintTag:
    OpenPrintTag detection is kept in the dispatcher so users get a clear
    "not yet supported" message. It will raise NotImplementedError, not route
    to the parser. See Test 5 below.
"""

import json
import logging
from adapters.dispatcher import detect_and_parse

logging.basicConfig(level=logging.DEBUG)


def run(label: str, uid: str, tag_data: dict):
    """Helper to run a single test case and print the result."""
    print(f"\n--- {label} ---")
    try:
        spool = detect_and_parse(uid, tag_data)
        print(json.dumps(spool.to_dict(), indent=2))
    except NotImplementedError as e:
        print(f"NotImplementedError (expected for OpenPrintTag): {e}")
    except ValueError as e:
        print(f"ValueError (expected for unknown tags): {e}")
    except Exception as e:
        print(f"Unexpected error: {e}")


# ── Test 1: OpenTag3D — auto-detect, no "format" key ─────────────────────────
mqtt_payload_1 = {
    "uid": "UID-1111",
    "toolhead": "lane1",
    "manufacturer": "Polymaker",
    "material_name": "PLA",
    "color_hex": "#00FF00",
    "spool_weight_nominal": 1000.0,
    "spool_weight_measured": 450.0,
}
uid1 = mqtt_payload_1["uid"]
tag_data_1 = {k: v for k, v in mqtt_payload_1.items() if k not in ("uid", "toolhead")}
run("Test 1: OpenTag3D (auto-detect)", uid1, tag_data_1)


# ── Test 2: Explicit "format" key override ────────────────────────────────────
# Simulates future ESP32 firmware that explicitly declares the format.
# Dispatcher should trust it without running auto-detect.
mqtt_payload_2 = {
    "uid": "UID-2222",
    "toolhead": "T0",
    "format": "opentag3d",              # explicit override
    "manufacturer": "eSun",
    "material_name": "PETG",
    "color_hex": "#FF6600",
    "spool_weight_nominal": 1000.0,
    "spool_weight_measured": 750.0,
}
uid2 = mqtt_payload_2["uid"]
tag_data_2 = {k: v for k, v in mqtt_payload_2.items() if k not in ("uid", "toolhead")}
run("Test 2: Explicit format override (opentag3d)", uid2, tag_data_2)


# ── Test 3: Partial tag — only one detection key present ─────────────────────
# Someone wrote an OpenTag3D tag but only filled in manufacturer.
# auto-detect should still route it correctly via any().
mqtt_payload_3 = {
    "uid": "UID-3333",
    "toolhead": "lane2",
    "manufacturer": "eSun",             # only one OT3D key — should still detect
}
uid3 = mqtt_payload_3["uid"]
tag_data_3 = {k: v for k, v in mqtt_payload_3.items() if k not in ("uid", "toolhead")}
run("Test 3: Partial OpenTag3D tag (one detection key)", uid3, tag_data_3)


# ── Test 4: Blank/unknown tag ─────────────────────────────────────────────────
# No recognizable format keys. Should raise ValueError — not silently return None.
mqtt_payload_4 = {
    "uid": "UID-4444",
    "toolhead": "lane3",
}
uid4 = mqtt_payload_4["uid"]
tag_data_4 = {k: v for k, v in mqtt_payload_4.items() if k not in ("uid", "toolhead")}
run("Test 4: Blank/unknown tag (expect ValueError)", uid4, tag_data_4)


# ── Test 5: OpenPrintTag — should raise NotImplementedError ──────────────────
# OpenPrintTag is detected but not yet supported. Users should get a clear
# message explaining why, not a confusing "unknown format" error.
mqtt_payload_5 = {
    "uid": "UID-5555",
    "toolhead": "lane4",
    "brand_name": "Prusament",
    "material_type": "PETG",
    "actual_netto_full_weight": 1000.0,
}
uid5 = mqtt_payload_5["uid"]
tag_data_5 = {k: v for k, v in mqtt_payload_5.items() if k not in ("uid", "toolhead")}
run("Test 5: OpenPrintTag (expect NotImplementedError)", uid5, tag_data_5)
