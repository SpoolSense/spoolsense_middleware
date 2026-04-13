"""
models.py — Data models for scan events and spool info.

ScanEvent is the normalized output from all tag parsers — every tag format
(spoolsense_scanner, OpenTag3D, mobile) produces one of these.
SpoolInfo carries resolved Spoolman data after enrichment.
"""
from dataclasses import dataclass, asdict, field
from typing import Any, Dict, Literal, Optional

ScanSource = Literal["legacy_uid", "spoolsense_scanner", "opentag3d", "mobile"]


@dataclass
class ScanEvent:
    source: ScanSource
    target_id: str
    scanned_at: str

    # NFC identity
    uid: Optional[str] = None           # hardware NFC chip UID — used for Spoolman lookup
    tag_uuid: Optional[str] = None      # UUID embedded in tag data by OpenPrintTag spec
    tag_type: Optional[str] = None      # e.g. "OpenPrintTag"
    tag_format_version: Optional[int] = None

    present: bool = True
    tag_data_valid: bool = False
    scanner_spoolman_id: Optional[int] = None  # spoolman_id from scanner payload — hint only, -1 stripped to None
    blank: bool = False                        # tag is blank/uninitialized

    # Normalized filament fields
    brand_name: Optional[str] = None
    material_type: Optional[str] = None
    material_name: Optional[str] = None
    color_name: Optional[str] = None
    color_hex: Optional[str] = None     # derived from color_name via lookup when not provided directly

    diameter_mm: Optional[float] = None
    density: Optional[float] = None

    nozzle_temp_min_c: Optional[int] = None
    nozzle_temp_max_c: Optional[int] = None
    bed_temp_min_c: Optional[int] = None
    bed_temp_max_c: Optional[int] = None

    full_weight_g: Optional[float] = None
    remaining_weight_g: Optional[float] = None
    remaining_length_mm: Optional[float] = None  # converted from remaining_m × 1000

    # Tag provenance
    tag_written_at: Optional[str] = None    # when tag was written (unix → ISO)

    # Original payload — available for debugging and future fields
    raw: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

@dataclass
class SpoolInfo:
    spool_uid: Optional[str]
    source: str                  # 'openprinttag', 'opentag3d', 'spoolman', 'merged', 'manual'

    spoolman_id: Optional[int] = None
    tag_version: Optional[str] = None

    brand: Optional[str] = None
    vendor: Optional[str] = None
    material_type: Optional[str] = None
    material_name: Optional[str] = None
    color_name: Optional[str] = None
    color_hex: Optional[str] = None

    diameter_mm: Optional[float] = None

    nozzle_temp_min_c: Optional[int] = None
    nozzle_temp_max_c: Optional[int] = None
    bed_temp_min_c: Optional[int] = None
    bed_temp_max_c: Optional[int] = None

    full_weight_g: Optional[float] = None
    empty_spool_weight_g: Optional[float] = None
    remaining_weight_g: Optional[float] = None
    consumed_weight_g: Optional[float] = None

    full_length_mm: Optional[float] = None
    remaining_length_mm: Optional[float] = None
    consumed_length_mm: Optional[float] = None

    lot_number: Optional[str] = None
    gtin: Optional[str] = None
    manufactured_at: Optional[str] = None
    expires_at: Optional[str] = None
    updated_at: Optional[str] = None

    notes: Optional[str] = None

    def to_dict(self):
        """Helper to easily convert to JSON for Moonraker/MQTT"""
        return asdict(self)

@dataclass
class SpoolAssignment:
    target_type: str      # 'single_tool', 'tool', 'afc_lane'
    target_id: str        # 'default', 'T0', 'lane3'
    spool_uid: str
    active: bool
    assigned_at: Optional[str] = None

    def to_dict(self):
        return asdict(self)
