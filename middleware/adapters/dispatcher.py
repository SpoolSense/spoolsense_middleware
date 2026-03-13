import logging
from state.models import SpoolInfo
from opentag3d.parser import parse_opentag3d
from openprinttag.scanner_parser import parse_openprinttag_scanner

# OpenPrintTag spec parser (openprinttag/parser.py) is implemented but not yet active.
# Requires a custom ESPHome component to read full CBOR data from ISO 15693 tags
# via the PN5180 — the available ESPHome PN5180 components only expose the UID.
# Uncomment the import and routing below when that work is resumed.
# from openprinttag.parser import parse_openprinttag


def detect_format(raw_data: dict) -> str:
    """
    Auto-detects the NFC tag format based on the unique keys present in the JSON payload.
    """
    # openprinttag_scanner payloads (ryanch/openprinttag_scanner) contain 'present'
    # and 'tag_data_valid' — these keys don't appear in any other format
    if "present" in raw_data and "tag_data_valid" in raw_data:
        return "openprinttag_scanner"

    # OpenTag3D uses 'opentag_version', 'manufacturer', or 'spool_weight_nominal'
    if any(k in raw_data for k in ("opentag_version", "spool_weight_nominal")):
        return "opentag3d"

    # OpenPrintTag spec uses 'brand_name', 'primary_color', or 'actual_netto_full_weight'
    # Detection is kept so users get a clear "not yet supported" message
    if any(k in raw_data for k in ("brand_name", "primary_color", "actual_netto_full_weight")):
        return "openprinttag"

    return "unknown"


def detect_and_parse(uid: str, raw_data: dict) -> SpoolInfo:
    """
    The main entry point for raw MQTT payloads.
    Detects the format and routes it to the correct parser.

    The ESP32 firmware can explicitly declare the format by including a 'format'
    key in the payload. If absent, format is auto-detected from the keys present.

    Currently supported:
      - opentag3d           (PN532 + OpenTag3D tags)
      - openprinttag_scanner (ryanch/openprinttag_scanner via PN5180)

    Not yet supported:
      - openprinttag spec   (requires custom ESPHome PN5180 component for CBOR reading)
    """
    fmt = raw_data.get("format") or detect_format(raw_data)

    logging.debug(f"Detected tag format: {fmt} for UID: {uid}")

    if fmt == "opentag3d":
        return parse_opentag3d(uid, raw_data)

    elif fmt == "openprinttag_scanner":
        # Guard: scanner publishes present=False when no tag is on the reader
        if not raw_data.get("present", False):
            raise ValueError(f"Scanner reported no tag present for UID: {uid}")
        # Guard: tag_data_valid=False means the tag was read but data is corrupt/incomplete
        if not raw_data.get("tag_data_valid", False):
            raise ValueError(f"Scanner reported invalid tag data for UID: {uid}")
        return parse_openprinttag_scanner(raw_data)

    elif fmt == "openprinttag":
        raise NotImplementedError(
            "OpenPrintTag spec format is not yet supported. Full CBOR tag reading requires "
            "a custom ESPHome PN5180 component. Use the openprinttag_scanner instead."
        )

    else:
        raise ValueError(f"Unknown or unsupported tag format. Payload keys: {list(raw_data.keys())}")
