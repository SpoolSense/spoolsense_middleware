import logging
from state.models import SpoolInfo
from opentag3d.parser import parse_opentag3d

# OpenPrintTag parser is implemented (openprinttag/parser.py) but not yet active.
# Requires a custom ESPHome component to read full CBOR data from ISO 15693 tags
# via the PN5180 — the available ESPHome PN5180 components only expose the UID.
# Uncomment the import and routing below when that work is resumed.
# from openprinttag.parser import parse_openprinttag


def detect_format(raw_data: dict) -> str:
    """
    Auto-detects the NFC tag format based on the unique keys present in the JSON payload.
    """
    # OpenTag3D uses 'manufacturer', 'opentag_version', and 'spool_weight_nominal'
    if any(k in raw_data for k in ("opentag_version", "manufacturer", "spool_weight_nominal")):
        return "opentag3d"

    # OpenPrintTag uses 'brand_name', 'primary_color', and 'actual_netto_full_weight'
    # Detection is kept so users get a clear "not yet supported" message instead of "unknown format"
    if any(k in raw_data for k in ("brand_name", "primary_color", "actual_netto_full_weight")):
        return "openprinttag"

    return "unknown"


def detect_and_parse(uid: str, raw_data: dict) -> SpoolInfo:
    """
    The main entry point for raw MQTT payloads.
    Detects the format and routes it to the correct parser.

    The ESP32 firmware can explicitly declare the format by including a 'format'
    key in the payload (e.g. "format": "opentag3d"). If absent, format is
    auto-detected from whichever unique keys are present in the payload.

    Currently supported:
      - opentag3d

    Not yet supported:
      - openprinttag (requires custom ESPHome PN5180 component for CBOR reading)
    """
    fmt = raw_data.get("format") or detect_format(raw_data)

    logging.debug(f"Detected tag format: {fmt} for UID: {uid}")

    if fmt == "opentag3d":
        return parse_opentag3d(uid, raw_data)
    elif fmt == "openprinttag":
        raise NotImplementedError(
            "OpenPrintTag is not yet supported. Full CBOR tag reading requires a custom "
            "ESPHome PN5180 component. See docs for details."
        )
    else:
        raise ValueError(f"Unknown or unsupported tag format. Payload keys: {list(raw_data.keys())}")
