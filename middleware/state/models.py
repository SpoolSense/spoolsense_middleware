from dataclasses import dataclass, asdict
from typing import Optional

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