from state.models import SpoolInfo
from openprinttag.color_map import color_name_to_hex


def parse_openprinttag_scanner(raw_data: dict) -> SpoolInfo:
    """
    Converts a payload from the openprinttag_scanner (ryanch/openprinttag_scanner)
    into a normalized SpoolInfo object.

    This is distinct from parser.py, which expects raw CBOR-decoded spec fields.
    The scanner publishes its own flattened JSON schema over MQTT on the topic:
        openprinttag/<deviceId>/tag/state

    The dispatcher guards against present=False and tag_data_valid=False before
    calling this function, so by the time we get here the tag data is valid.

    Color handling:
        OpenPrintTag stores color as a descriptive name (e.g. "Galaxy Black"),
        not a hex value. We convert it using color_map.color_name_to_hex() so
        LEDs and Spoolman get a usable hex color.

    Field mapping from scanner schema to SpoolInfo:
        uid              → spool_uid
        manufacturer     → brand
        color            → color_name (raw), color_hex (converted to hex)
        material_type    → material_type
        material_name    → material_name
        remaining_g      → remaining_weight_g
        initial_weight_g → full_weight_g
        spoolman_id      → spoolman_id  (only if not -1)
    """
    uid = raw_data.get("uid", "")

    # spoolman_id of -1 means the scanner has not linked this tag to Spoolman yet
    spoolman_id = raw_data.get("spoolman_id")
    if spoolman_id == -1:
        spoolman_id = None

    # OpenPrintTag color is a name like "Galaxy Black", not a hex value
    raw_color = raw_data.get("color", "")
    color_hex = color_name_to_hex(raw_color)

    return SpoolInfo(
        spool_uid=uid,
        source="openprinttag_scanner",
        spoolman_id=spoolman_id,
        brand=raw_data.get("manufacturer"),
        material_type=raw_data.get("material_type"),
        material_name=raw_data.get("material_name"),
        color_name=raw_color or None,
        color_hex=color_hex,
        remaining_weight_g=raw_data.get("remaining_g"),
        full_weight_g=raw_data.get("initial_weight_g"),
    )
