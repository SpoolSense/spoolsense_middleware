import logging

logger = logging.getLogger(__name__)

from state.models import ScanEvent
from opentag3d.parser import parse_opentag3d
from openprinttag.scanner_parser import scan_event_from_spoolsense_scanner

# OpenPrintTag spec parser (openprinttag/parser.py) is implemented but not yet active.
# Requires a custom ESPHome component to read full CBOR data from ISO 15693 tags
# via the PN5180 — the available ESPHome PN5180 components only expose the UID.
# from openprinttag.parser import parse_openprinttag


def detect_format(payload: dict) -> str:
    """
    Auto-detects the tag format from payload keys/values.
    """
    # spoolsense_scanner payloads always contain both 'present' and
    # 'tag_data_valid' — these keys don't appear in any other supported format.
    if "present" in payload and "tag_data_valid" in payload:
        return "spoolsense_scanner"

    # OpenTag3D uses 'opentag_version' or 'spool_weight_nominal'
    if any(k in payload for k in ("opentag_version", "spool_weight_nominal")):
        return "opentag3d"

    # OpenPrintTag spec (CBOR direct) — not yet supported but detected so users
    # get a clear error instead of a confusing "unknown format" message
    if any(k in payload for k in ("brand_name", "primary_color", "actual_netto_full_weight")):
        return "openprinttag"

    return "unknown"


def detect_and_parse(payload: dict, target_id: str, topic: str = "") -> ScanEvent:
    """
    Detects the tag format and routes to the correct parser.

    Always returns a ScanEvent. Check event.tag_data_valid before acting on the
    data — a False value means the tag was present but data could not be read.

    Args:
        payload:   Raw MQTT payload dict (the full tag/attributes JSON).
        target_id: Target identifier from config (e.g. "T0", "lane1", "default").
        topic:     MQTT topic the message arrived on — used for logging only.
    """
    fmt = payload.get("format") or detect_format(payload)

    logger.debug("Detected format: %s | target: %s | topic: %s", fmt, target_id, topic)

    if fmt == "opentag3d":
        return parse_opentag3d(payload, target_id)

    elif fmt == "spoolsense_scanner":
        return scan_event_from_spoolsense_scanner(payload, target_id, topic=topic)

    elif fmt == "openprinttag":
        raise NotImplementedError(
            "OpenPrintTag spec format (CBOR direct) is not yet supported. "
            "Use spoolsense/spoolsense_scanner instead."
        )

    else:
        raise ValueError(
            f"Unknown or unsupported tag format. Payload keys: {list(payload.keys())}"
        )
