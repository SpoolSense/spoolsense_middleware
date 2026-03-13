from dataclasses import dataclass, asdict, field
from typing import Any, Literal, Optional

ScanSource = Literal["legacy_uid", "openprinttag_scanner", "opentag3d"]


@dataclass(slots=True)
class ScanEvent:
    source: ScanSource
    target_id: str
    scanned_at: str

    # NFC identity
    uid: str | None = None           # hardware NFC chip UID — used for Spoolman lookup
    tag_uuid: str | None = None      # UUID embedded in tag data by OpenPrintTag spec
    tag_type: str | None = None      # e.g. "OpenPrintTag"
    tag_format_version: int | None = None

    present: bool = True
    tag_data_valid: bool = False

    # Normalized filament fields
    brand_name: str | None = None
    material_type: str | None = None
    material_name: str | None = None
    color_name: str | None = None
    color_hex: str | None = None     # derived from color_name via lookup when not provided directly

    diameter_mm: float | None = None
    density: float | None = None

    nozzle_temp_min_c: int | None = None
    nozzle_temp_max_c: int | None = None
    bed_temp_min_c: int | None = None
    bed_temp_max_c: int | None = None

    full_weight_g: float | None = None
    remaining_weight_g: float | None = None
    remaining_length_mm: float | None = None  # converted from remaining_m × 1000

    # Tag provenance
    tag_written_at: str | None = None    # when tag was written (unix → ISO)

    # Original payload — available for debugging and future fields
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
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