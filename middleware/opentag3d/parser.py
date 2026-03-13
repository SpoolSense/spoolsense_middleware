from datetime import datetime, timezone

from state.models import ScanEvent


def parse_opentag3d(payload: dict, target_id: str) -> ScanEvent:
    """
    Converts an OpenTag3D Web API JSON payload into a normalized ScanEvent.

    uid is expected in the payload. Field names follow the OpenTag3D Web API spec.

    Note: The binary tag format stores diameter in micrometers and temperatures
    as °C ÷ 5, but the Web API normalizes these to mm and °C respectively.

    Field mapping:
        uid                  → uid
        opentag_version      → tag_format_version
        manufacturer         → brand_name
        material_name        → material_type  (OpenTag3D's naming for the material type)
        color_name           → color_name
        color_hex            → color_hex
        diameter             → diameter_mm
        extruder_temp_min    → nozzle_temp_min_c
        extruder_temp_max    → nozzle_temp_max_c
        bed_temp_min         → bed_temp_min_c
        bed_temp_max         → bed_temp_max_c
        spool_weight_nominal → full_weight_g
        spool_weight_measured→ remaining_weight_g
    """
    opentag_version = payload.get("opentag_version")

    return ScanEvent(
        source="opentag3d",
        target_id=target_id,
        scanned_at=datetime.now(timezone.utc).isoformat(),
        uid=payload.get("uid") or None,
        tag_format_version=int(opentag_version) if opentag_version is not None else None,
        present=True,
        tag_data_valid=True,
        brand_name=payload.get("manufacturer") or None,
        material_type=payload.get("material_name") or None,
        color_name=payload.get("color_name") or None,
        color_hex=payload.get("color_hex") or None,
        diameter_mm=payload.get("diameter"),
        nozzle_temp_min_c=payload.get("extruder_temp_min"),
        nozzle_temp_max_c=payload.get("extruder_temp_max"),
        bed_temp_min_c=payload.get("bed_temp_min"),
        bed_temp_max_c=payload.get("bed_temp_max"),
        full_weight_g=payload.get("spool_weight_nominal"),
        remaining_weight_g=payload.get("spool_weight_measured"),
        raw=payload,
    )
