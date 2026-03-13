from state.models import SpoolInfo


def parse_openprinttag_scanner(raw_data: dict) -> SpoolInfo:
    """
    Converts a payload from the openprinttag_scanner (ryanch/openprinttag_scanner)
    into a normalized SpoolInfo object.

    This is distinct from parser.py, which expects raw CBOR-decoded spec fields.
    The scanner publishes its own flattened JSON schema over MQTT on the topic:
        openprinttag/<deviceId>/tag/state

    The dispatcher guards against present=False and tag_data_valid=False before
    calling this function, so by the time we get here the tag data is valid.

    Field mapping from scanner schema to SpoolInfo:
        uid              → spool_uid
        manufacturer     → brand
        color            → color_hex
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

    return SpoolInfo(
        spool_uid=uid,
        source="openprinttag_scanner",
        spoolman_id=spoolman_id,
        brand=raw_data.get("manufacturer"),
        material_type=raw_data.get("material_type"),
        material_name=raw_data.get("material_name"),
        color_hex=raw_data.get("color"),
        remaining_weight_g=raw_data.get("remaining_g"),
        full_weight_g=raw_data.get("initial_weight_g"),
    )
