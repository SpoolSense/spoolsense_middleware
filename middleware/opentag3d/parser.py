from state.models import SpoolInfo


def parse_opentag3d(uid: str, raw_data: dict) -> SpoolInfo:
    """
    Converts OpenTag3D Web API JSON into a normalized SpoolInfo object.

    raw_data is expected to be the JSON response from the OpenTag3D Web API
    (or a test stub using the same field names). Field names follow the
    OpenTag3D Web API spec.

    Note: The binary tag format stores diameter in micrometers and temperatures
    as °C ÷ 5, but the Web API normalizes these to mm and °C respectively.
    """
    return SpoolInfo(
        spool_uid=uid,
        source="opentag3d",
        tag_version=str(raw_data["opentag_version"]) if raw_data.get("opentag_version") is not None else None,
        brand=raw_data.get("manufacturer"),
        material_type=raw_data.get("material_name"),
        color_name=raw_data.get("color_name"),
        color_hex=raw_data.get("color_hex"),
        diameter_mm=raw_data.get("diameter"),
        nozzle_temp_min_c=raw_data.get("extruder_temp_min"),
        nozzle_temp_max_c=raw_data.get("extruder_temp_max"),
        bed_temp_min_c=raw_data.get("bed_temp_min"),
        bed_temp_max_c=raw_data.get("bed_temp_max"),
        full_weight_g=raw_data.get("spool_weight_nominal"),
        remaining_weight_g=raw_data.get("spool_weight_measured"),
    )
