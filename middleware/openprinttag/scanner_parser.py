from datetime import datetime, timezone

from state.models import ScanEvent


def scan_event_from_openprinttag_scanner(payload: dict, target_id: str) -> ScanEvent:
    """
    Converts a payload from ryanch/openprinttag_scanner into a normalized ScanEvent.

    The scanner publishes to two topics — both carry an identical payload:
        openprinttag/<deviceId>/tag/state
        openprinttag/<deviceId>/tag/attributes

    Color handling:
        color is published as a hex string (e.g. "#1A1A2E"). We strip the leading
        "#" to match the canonical no-prefix format used everywhere in SpoolSense
        (ESPHome expects 6 bare chars; Spoolman stores without "#").

    spoolman_id handling:
        The scanner embeds the Spoolman spool ID if the tag was linked via the
        openprinttag_scanner UI. -1 means unlinked. SpoolSense stores it as a hint
        in scanner_spoolman_id but re-resolves via the NFC UID as the authority.

    Field mapping:
        uid              → uid
        present          → present
        tag_data_valid   → tag_data_valid
        manufacturer     → brand_name
        material_type    → material_type
        material_name    → material_name
        color            → color_hex  ("#RRGGBB" stripped to "RRGGBB")
        remaining_g      → remaining_weight_g
        initial_weight_g → full_weight_g
        spoolman_id      → scanner_spoolman_id  (-1 → None)
        blank            → blank

    Fields NOT published by this firmware (exist in tag CBOR but not in MQTT payload):
        diameter, density, remaining_m, temp_min, temp_max, bed_temp,
        uuid, format_version, type
    """
    raw_color = payload.get("color")
    color_hex = raw_color.lstrip("#").upper() if raw_color else None

    spoolman_id = payload.get("spoolman_id")
    if spoolman_id == -1:
        spoolman_id = None

    return ScanEvent(
        source="openprinttag_scanner",
        target_id=target_id,
        scanned_at=datetime.now(timezone.utc).isoformat(),
        uid=payload.get("uid") or None,
        present=bool(payload.get("present", True)),
        tag_data_valid=bool(payload.get("tag_data_valid", False)),
        scanner_spoolman_id=spoolman_id,
        blank=bool(payload.get("blank", False)),
        brand_name=payload.get("manufacturer") or None,
        material_type=payload.get("material_type") or None,
        material_name=payload.get("material_name") or None,
        color_hex=color_hex,
        full_weight_g=payload.get("initial_weight_g"),
        remaining_weight_g=payload.get("remaining_g"),
        raw=payload,
    )
