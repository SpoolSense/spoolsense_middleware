from state.models import SpoolInfo


def _rgba_to_hex(rgba: int) -> str:
    """Convert a packed RGBA integer to an HTML hex color string (ignores alpha)."""
    r = (rgba >> 24) & 0xFF
    g = (rgba >> 16) & 0xFF
    b = (rgba >> 8)  & 0xFF
    return f"#{r:02X}{g:02X}{b:02X}"


def parse_openprinttag(uid: str, raw_data: dict) -> SpoolInfo:
    """
    Converts decoded OpenPrintTag CBOR data into a normalized SpoolInfo object.

    raw_data is expected to be the CBOR payload already decoded to a dict by
    the ESP32 firmware (or a test stub). Field names follow the OpenPrintTag spec.

    Remaining weight is not stored directly — it is calculated as:
        actual_netto_full_weight - consumed_weight
    """
    # primary_color is a packed RGBA integer in the spec
    color = None
    raw_color = raw_data.get("primary_color")
    if isinstance(raw_color, int):
        color = _rgba_to_hex(raw_color)
    elif isinstance(raw_color, str):
        color = raw_color if raw_color.startswith("#") else f"#{raw_color}"

    full_weight = raw_data.get("actual_netto_full_weight")
    consumed = raw_data.get("consumed_weight")
    remaining = (full_weight - consumed) if (full_weight is not None and consumed is not None) else None

    return SpoolInfo(
        spool_uid=uid,
        source="openprinttag",
        brand=raw_data.get("brand_name"),
        material_type=raw_data.get("material_type"),
        material_name=raw_data.get("material_name"),
        color_hex=color,
        diameter_mm=raw_data.get("filament_diameter"),
        nozzle_temp_min_c=raw_data.get("min_print_temperature"),
        nozzle_temp_max_c=raw_data.get("max_print_temperature"),
        bed_temp_min_c=raw_data.get("min_bed_temperature"),
        bed_temp_max_c=raw_data.get("max_bed_temperature"),
        full_weight_g=full_weight,
        empty_spool_weight_g=raw_data.get("empty_container_weight"),
        remaining_weight_g=remaining,
        consumed_weight_g=consumed,
    )
