"""
Parser Isolation Test
=====================
Tests the OpenTag3D parser completely in isolation.
No NFC scanner, MQTT broker, or Klipper instance required.

HOW TO RUN:
    From the middleware/ directory:

        python test_parsers.py

HOW TO FEED IT YOUR OWN DATA:
    Edit the fake payload dict below (fake_ot3d_payload) to match whatever tag
    data you want to test. Field names must match the OpenTag3D Web API spec —
    see the comments next to each field.

    If you have a real tag, query the OpenTag3D Web API and paste the response
    directly in place of fake_ot3d_payload.

    To test an edge case (e.g. missing color, no weight), just remove that key
    from the dict. The parser should leave that SpoolInfo field as None.

WHAT TO CHECK IN THE OUTPUT:
    - source should be "opentag3d"
    - brand should be "Polymaker"
    - any field not in the payload should appear as null in the output

NOTE — OpenPrintTag:
    The OpenPrintTag parser (openprinttag/parser.py) exists but is not active.
    It requires a custom ESPHome PN5180 component to read CBOR data off the tag.
    Uncomment the section below when that work is resumed.
"""

import json
from opentag3d.parser import parse_opentag3d

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

print("--- Parsing OpenTag3D ---")
ot3d_spool = parse_opentag3d(fake_ot3d_payload, "T0")
print(json.dumps(ot3d_spool.to_dict(), indent=2))


# --- OpenPrintTag (not yet active) ---
# Uncomment when the custom PN5180 ESPHome component is ready.
#
# from openprinttag.parser import parse_openprinttag
#
# fake_opt_payload = {
#     "brand_name": "Prusament",
#     "material_type": "PETG",
#     "material_name": "Prusament PETG Urban Grey",
#     "primary_color": 0xFF0000FF,        # packed RGBA → parser outputs #FF0000
#     "min_print_temperature": 230,
#     "max_print_temperature": 250,
#     "min_bed_temperature": 70,
#     "max_bed_temperature": 90,
#     "filament_diameter": 1.75,
#     "actual_netto_full_weight": 1000.0,
#     "empty_container_weight": 188.0,
#     "consumed_weight": 200.0,           # remaining = 1000 - 200 = 800g
# }
#
# print("\n--- Parsing OpenPrintTag ---")
# opt_spool = parse_openprinttag("UID-1111", fake_opt_payload)
# print(json.dumps(opt_spool.to_dict(), indent=2))
