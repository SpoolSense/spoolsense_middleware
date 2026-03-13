"""
Parser Isolation Test
=====================
Tests the OpenPrintTag and OpenTag3D parsers completely in isolation.
No NFC scanner, MQTT broker, or Klipper instance required.

HOW TO RUN:
    From the middleware/ directory:

        python test_parsers.py

HOW TO FEED IT YOUR OWN DATA:
    Edit the fake payload dicts below (fake_opt_payload and fake_ot3d_payload)
    to match whatever tag data you want to test. The field names must match the
    format's spec — see the comments next to each field.

    For OpenPrintTag, the ESP32 decodes the CBOR off the tag and sends the
    result as JSON over MQTT. The keys in fake_opt_payload mirror what that
    decoded JSON looks like. Swap in values from a real tag scan to verify your
    parser handles them correctly.

    For OpenTag3D, raw_data mirrors the Web API JSON response. If you have a
    real tag, you can query the API and paste the response directly in place of
    fake_ot3d_payload.

    To test an edge case (e.g. missing color, no consumed_weight), just remove
    that key from the dict. The parser should leave that SpoolInfo field as None.

WHAT TO CHECK IN THE OUTPUT:
    - OpenPrintTag: remaining_weight_g should equal full - consumed (800.0g here)
    - OpenPrintTag: color_hex should be #FF0000 (converted from packed RGBA 0xFF0000FF)
    - OpenTag3D:    source should be "opentag3d", brand should be "Polymaker"
    - Both:         any field not in the payload should appear as null in the output
"""

import json
from openprinttag.parser import parse_openprinttag
from opentag3d.parser import parse_opentag3d

# --- OpenPrintTag ---
# These field names match the OpenPrintTag spec after CBOR decoding.
# primary_color is a packed RGBA integer — the parser converts it to hex.
# remaining_weight_g is not on the tag; it is calculated: full - consumed.
fake_opt_payload = {
    "brand_name": "Prusament",
    "material_type": "PETG",
    "material_name": "Prusament PETG Urban Grey",
    "primary_color": 0xFF0000FF,        # packed RGBA → parser outputs #FF0000
    "min_print_temperature": 230,
    "max_print_temperature": 250,
    "min_bed_temperature": 70,
    "max_bed_temperature": 90,
    "filament_diameter": 1.75,
    "actual_netto_full_weight": 1000.0,
    "empty_container_weight": 188.0,
    "consumed_weight": 200.0,           # remaining = 1000 - 200 = 800g
}

# --- OpenTag3D ---
# These field names match the OpenTag3D Web API JSON response.
# Temperatures are already in °C and diameter is already in mm at this layer.
fake_ot3d_payload = {
    "opentag_version": 1,
    "manufacturer": "Polymaker",
    "material_name": "PLA",
    "color_name": "Lime Green",
    "color_hex": "#00FF00",
    "extruder_temp_min": 190,
    "extruder_temp_max": 220,
    "bed_temp_min": 25,
    "bed_temp_max": 60,
    "diameter": 1.75,
    "spool_weight_nominal": 1000.0,
    "spool_weight_measured": 450.0,
}

print("--- Parsing OpenPrintTag ---")
opt_spool = parse_openprinttag("UID-1111", fake_opt_payload)
print(json.dumps(opt_spool.to_dict(), indent=2))

print("\n--- Parsing OpenTag3D ---")
ot3d_spool = parse_opentag3d("UID-2222", fake_ot3d_payload)
print(json.dumps(ot3d_spool.to_dict(), indent=2))
