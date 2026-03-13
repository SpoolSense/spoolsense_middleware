from datetime import datetime, timezone

from state.models import ScanEvent
from openprinttag.color_map import color_name_to_hex


def scan_event_from_openprinttag_scanner(payload: dict, target_id: str) -> ScanEvent:
    """
    Converts a payload from the openprinttag_scanner (ryanch/openprinttag_scanner)
    into a normalized ScanEvent.

    The scanner publishes tag data as a flattened JSON payload over MQTT on the
    topic: openprinttag/<deviceId>/tag/attributes

    Color handling:
        OpenPrintTag stores color as a descriptive name (e.g. "Galaxy Black"),
        not a hex value. We convert it using color_map.color_name_to_hex() so
        LEDs and Spoolman get a usable hex color.

    Field mapping from scanner payload to ScanEvent:
        uid              → uid
        type             → tag_type  (e.g. "OpenPrintTag")
        format_version   → tag_format_version
        valid            → tag_data_valid
        manufacturer     → brand_name
        color            → color_name (raw), color_hex (converted to hex)
        material         → material_type
        material_detail  → material_name
        remaining_g      → remaining_weight_g
        remaining_m      → remaining_length_mm (× 1000)
        initial_weight_g → full_weight_g
        nozzle_min       → nozzle_temp_min_c
        nozzle_max       → nozzle_temp_max_c
        bed_min          → bed_temp_min_c
        bed_max          → bed_temp_max_c
        diameter_um      → diameter_mm (÷ 1000)
        density          → density
        written_at       → tag_written_at
    """
    # OpenPrintTag color is a name like "Galaxy Black", not a hex value
    raw_color = payload.get("color", "")
    color_hex = color_name_to_hex(raw_color) if raw_color else None

    # Diameter comes in micrometers from the scanner
    diameter_um = payload.get("diameter_um")
    diameter_mm = diameter_um / 1000.0 if diameter_um is not None else None

    # Remaining length in meters → convert to mm
    remaining_m = payload.get("remaining_m")
    remaining_length_mm = remaining_m * 1000.0 if remaining_m is not None else None

    # written_at may be a unix timestamp
    written_at_raw = payload.get("written_at")
    tag_written_at = None
    if written_at_raw is not None:
        try:
            tag_written_at = datetime.fromtimestamp(written_at_raw, tz=timezone.utc).isoformat()
        except (ValueError, TypeError, OSError):
            tag_written_at = str(written_at_raw)

    return ScanEvent(
        source="openprinttag_scanner",
        target_id=target_id,
        scanned_at=datetime.now(timezone.utc).isoformat(),
        uid=payload.get("uid") or None,
        tag_type=payload.get("type") or None,
        tag_format_version=payload.get("format_version"),
        present=payload.get("present", True),
        tag_data_valid=payload.get("valid", False),
        brand_name=payload.get("manufacturer") or None,
        material_type=payload.get("material") or None,
        material_name=payload.get("material_detail") or None,
        color_name=raw_color or None,
        color_hex=color_hex,
        diameter_mm=diameter_mm,
        density=payload.get("density"),
        nozzle_temp_min_c=payload.get("nozzle_min"),
        nozzle_temp_max_c=payload.get("nozzle_max"),
        bed_temp_min_c=payload.get("bed_min"),
        bed_temp_max_c=payload.get("bed_max"),
        full_weight_g=payload.get("initial_weight_g"),
        remaining_weight_g=payload.get("remaining_g"),
        remaining_length_mm=remaining_length_mm,
        tag_written_at=tag_written_at,
        raw=payload,
    )
