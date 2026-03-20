# OpenPrintTag SpoolSense Architecture

OpenPrintTag Architecture for NFC tag reader
- single-tool Klipper systems
- multi-tool / toolchanger Klipper systems
- AFC systems using `afc-klipper-addon`

This design makes **OpenPrintTag the primary local source of truth** and keeps **Spoolman optional**.

## Why this exists

My project SpoolSense supports single toolhead, multi-toolheads and AFC systems. In order to get OpenPrintTag I need a way to read the
tags and send this info to the printer.

I am trying to implement tag-only operation, with this Spoolman will not be required

The clean approach is:

```text
NFC scanner -> MQTT -> dispatcher -> parser -> normalized spool state -> backend adapter
```

Backends can be:

- generic single-tool Klipper
- multi-tool / toolchanger Klipper
- AFC

## Hardware path for OpenPrintTag

OpenPrintTag uses ISO 15693 (NFC-V) tags, which the PN532 cannot read. The PN5180 can, but the available ESPHome community components for the PN5180 only expose the tag UID — not the full CBOR payload that OpenPrintTag requires. Writing a custom ESPHome component to read full tag memory is a significant undertaking.

The chosen approach is to use **[spoolsense/spoolsense_scanner](https://github.com/spoolsense/spoolsense_scanner)** — an ESP32-based scanner that reads the full OpenPrintTag CBOR data and publishes decoded JSON directly to MQTT. SpoolSense subscribes to the scanner's topic and picks up the payload in the middleware, the same pattern already used with ESPHome + PN532. It also supports plain UID-only tags (NTAG215) for simple Spoolman lookup workflows.

Scanner MQTT topic: `spoolsense/<deviceId>/tag/state`

Scanner payload shape:
```json
{
  "uid": "04AABBCCDD11",
  "present": true,
  "tag_data_valid": true,
  "manufacturer": "Prusament",
  "material_type": "PETG",
  "material_name": "Galaxy Black",
  "color": "#1A1A1A",
  "remaining_g": 640.0,
  "initial_weight_g": 1000.0,
  "spoolman_id": -1,
  "blank": false
}
```

## Supported tag formats

| Format | Hardware | Status |
|---|---|---|
| OpenPrintTag (via scanner) | spoolsense/spoolsense_scanner + PN5180 | Implemented — `scanner_parser.py` |
| OpenTag3D | ESPHome + PN532 | Implemented — `opentag3d/parser.py` |
| OpenPrintTag (spec/CBOR direct) | Custom ESPHome PN5180 component | Not yet supported |

## Core idea

SpoolSense Middleware owns the canonical spool state.

Do **not** make these the source of truth:

- AFC lane state
- Spoolman
- macro variables
- ad hoc Klipper config values

Instead:

1. Read the tag
2. Normalize the data
3. Store it in SpoolSense own namespace
4. Assign it to a target
5. Mirror/apply it to the active backend

## Recommended storage

Use **Moonraker DB** as the canonical shared state.

That gives:

- persistence across restarts
- one place to inspect data
- support for single-tool, multi-tool, and AFC
- easy UI and macro access
- less coupling to any one backend

## High-level architecture

```text
OpenPrintTag / NFC
        |
        v
   Parse + normalize
        |
        v
     SpoolInfo
        |
        +----------------------+
        |                      |
        v                      v
 SpoolAssignment      Optional Spoolman merge
        |                      |
        +----------+-----------+
                   |
                   v
        Moonraker DB namespace
                   |
     +-------------+-------------+
     |             |             |
     v             v             v
Single-tool   Multi-tool     AFC adapter
adapter       adapter        adapter
```

## Recommended defaults

- Canonical storage: `Moonraker DB`
- Primary source: `OpenPrintTag`
- Source mode: `prefer_tag`
- AFC role: `adapter only`
- Identity key: NFC UID or derived tag UID

## Normalized model

Use one internal object everywhere.

```python
from dataclasses import dataclass

@dataclass
class SpoolInfo:
    spool_uid: str | None
    source: str                  # spoolsense_scanner / opentag3d / spoolman / merged (tag preferred) / merged (spoolman preferred) / manual

    spoolman_id: int | None
    tag_version: str | None

    brand: str | None
    vendor: str | None
    material_type: str | None
    material_name: str | None
    color_name: str | None
    color_hex: str | None

    diameter_mm: float | None

    nozzle_temp_min_c: int | None
    nozzle_temp_max_c: int | None
    bed_temp_min_c: int | None
    bed_temp_max_c: int | None

    full_weight_g: float | None
    empty_spool_weight_g: float | None
    remaining_weight_g: float | None
    consumed_weight_g: float | None

    full_length_mm: float | None
    remaining_length_mm: float | None
    consumed_length_mm: float | None

    lot_number: str | None
    gtin: str | None
    manufactured_at: str | None
    expires_at: str | None
    updated_at: str | None

    notes: str | None
```

This object should be valid even when only part of the data exists.

## Assignment model

Keep the spool record separate from where it is assigned.

```python
from dataclasses import dataclass

@dataclass
class SpoolAssignment:
    target_type: str      # single_tool / tool / afc_lane / feeder / virtual_tool
    target_id: str        # default / T0 / T1 / lane3 / etc.
    spool_uid: str
    active: bool
    assigned_at: str | None
```

This lets the same spool data work for:

- `single_tool/default`
- `tool/T0`
- `tool/T1`
- `afc_lane/lane3`

## Suggested Moonraker DB layout

Namespace:

```text
spoolsense
```

Keys:

```text
spoolsense/spools/<spool_uid>
spoolsense/assignments/<target_type>/<target_id>
spoolsense/index/spoolman_id/<spoolman_id>
spoolsense/index/tag_uid/<tag_uid>
spoolsense/settings/backend
spoolsense/settings/source_mode
spoolsense/cache/last_scan
```

## Example data shape

```json
{
  "spools": {
    "04AABBCCDD11": {
      "spool_uid": "04AABBCCDD11",
      "source": "openprinttag",
      "spoolman_id": 123,
      "brand": "Prusament",
      "material_type": "PETG",
      "material_name": "Galaxy Black",
      "color_hex": "#1A1A1A",
      "diameter_mm": 1.75,
      "nozzle_temp_min_c": 230,
      "nozzle_temp_max_c": 250,
      "bed_temp_min_c": 75,
      "bed_temp_max_c": 90,
      "full_weight_g": 1000,
      "empty_spool_weight_g": 250,
      "remaining_weight_g": 640,
      "full_length_mm": 330000,
      "remaining_length_mm": 211000,
      "updated_at": "2026-03-11T12:00:00Z"
    }
  },
  "assignments": {
    "single_tool": {
      "default": {
        "spool_uid": "04AABBCCDD11",
        "active": true
      }
    },
    "tool": {
      "T0": {
        "spool_uid": "04AABBCCDD11",
        "active": true
      },
      "T1": {
        "spool_uid": "04EEFF001122",
        "active": true
      }
    },
    "afc_lane": {
      "lane3": {
        "spool_uid": "04AABBCCDD11",
        "active": true
      }
    }
  }
}
```

## Source precedence

Recommended default:

```text
prefer_tag
```

Supported modes:

- `tag_only`
- `prefer_tag`
- `prefer_spoolman`
- `merge`
- `manual_only`

Recommended behavior:

### `tag_only`
Only use OpenPrintTag data.

### `prefer_tag`
Use tag values when present. Fill missing fields from Spoolman.

### `prefer_spoolman`
Use Spoolman first, but still allow tag-only scans.

### `merge`
Apply explicit field-by-field precedence rules.

## Field precedence suggestion

Prefer the tag for printer-relevant fields:

- material type
- material name
- color
- diameter
- nozzle temp
- bed temp
- remaining weight
- remaining length

Let Spoolman fill inventory-style gaps when needed:

- spoolman ID
- notes
- external metadata
- organization-specific fields

## Backend strategy

Use one adapter per printer mode.

### 1. Single-tool adapter

For a normal Klipper printer with one active filament source.

Behavior:

- assign scanned spool to `single_tool/default`
- expose active spool metadata to macros/UI
- use the spool data for material-aware behavior

Good uses:

- display current material
- suggest or validate temperatures
- show remaining material
- show active color in UI

Do not try to shove the entire model into base Klipper.
Keep the canonical data in SpoolSense namespace and expose helper accessors.

### 2. Multi-tool / toolchanger adapter

For printers with multiple tools, docks, feeders, or toolheads.

Behavior:

- assign a spool per tool
- keep assignments independent from spool records
- allow each tool to have its own active material state

Examples:

- left scanner -> `tool/T0`
- right scanner -> `tool/T1`
- dock scanner -> mapped toolhead
- feeder scanner -> mapped feeder object

### 3. AFC adapter

For printers using `afc-klipper-addon`.

AFC should consume the normalized state, not own it.

Mirror the subset of fields AFC currently handles well:

- spool ID
- material
- color
- remaining weight
- diameter / empty spool weight where supported

Mapping example:

```text
SpoolInfo.color_hex            -> AFC SET_COLOR
SpoolInfo.material_type        -> AFC SET_MATERIAL
SpoolInfo.remaining_weight_g   -> AFC SET_WEIGHT
SpoolInfo.spoolman_id          -> AFC SET_SPOOL_ID (when used)
SpoolInfo.diameter_mm          -> AFC-supported material setter path
SpoolInfo.empty_spool_weight_g -> AFC-supported material setter path
```

Keep unsupported fields in SpoolSense own state, such as:

- full length
- remaining length
- consumed length
- lot number
- display metadata

## Actual module layout

```text
middleware/
├── openprinttag/
│   ├── __init__.py
│   ├── scanner_parser.py     # parses spoolsense/spoolsense_scanner MQTT payloads → SpoolInfo (active)
│   └── parser.py             # parses raw CBOR spec fields → SpoolInfo (not yet active — needs custom ESPHome component)
├── opentag3d/
│   ├── __init__.py
│   └── parser.py             # parses OpenTag3D Web API JSON → SpoolInfo (active)
├── spoolman/
│   ├── __init__.py
│   └── client.py             # NFC UID lookup, TTL cache, tag/Spoolman merge, weight sync, UID write-back
├── state/
│   ├── __init__.py
│   ├── models.py             # SpoolInfo and SpoolAssignment dataclasses
│   └── moonraker_db.py       # persists SpoolInfo and SpoolAssignment to Moonraker DB namespace
├── adapters/
│   ├── __init__.py
│   └── dispatcher.py         # detects format from payload keys, routes to correct parser
│                             # future: single_tool.py, multitool.py, afc.py
├── api/                      # placeholder — not yet implemented
├── test_parsers.py           # isolated parser tests, no hardware required
├── test_dispatcher.py        # isolated dispatcher tests, no hardware required
├── test_db.py                # Moonraker DB write test (requires running Moonraker)
└── spoolsense.py             # existing production middleware (PN532 + plain UID flow, unchanged)
```

### Scan flow

```text
1. Scanner reads NFC tag (spoolsense_scanner via PN5180, or ESPHome PN532 for OpenTag3D)
2. Scanner publishes decoded JSON payload to MQTT
3. Middleware receives MQTT message
4. dispatcher.py detects format from payload keys
5. Routes to correct parser (scanner_parser.py or opentag3d/parser.py)
6. Parser returns normalized SpoolInfo
7. Optionally merge with Spoolman (SpoolmanClient.sync_spool)
8. Store/update spool record in Moonraker DB
9. Create or update SpoolAssignment
10. Apply backend-specific mirror/update (single-tool / multi-tool / AFC adapter)
11. Let UI/macros read from canonical state
```

### Startup recovery flow

```text
1. Read saved assignments from Moonraker DB
2. Detect active backend mode
3. Re-apply backend mirror if needed
4. Restore state after restart
```

## Example internal service API

```python
class SpoolStateService:
    def upsert_spool(self, spool: SpoolInfo) -> None: ...
    def assign_spool(self, target_type: str, target_id: str, spool_uid: str) -> None: ...
    def get_spool(self, spool_uid: str) -> SpoolInfo | None: ...
    def get_assignment(self, target_type: str, target_id: str) -> SpoolAssignment | None: ...
    def get_assigned_spool(self, target_type: str, target_id: str) -> SpoolInfo | None: ...
    def list_assignments(self) -> list[SpoolAssignment]: ...
    def sync_backend(self) -> None: ...
```

## Minimal mapping table

| OpenPrintTag field | Normalized field | Single-tool | Multi-tool | AFC |
|---|---|:---|:---|:---|
| tag UID | `spool_uid` | yes | yes | yes |
| material type | `material_type` | yes | yes | yes |
| material name | `material_name` | yes | yes | maybe display only |
| color | `color_hex` | yes | yes | yes |
| diameter | `diameter_mm` | yes | yes | partial |
| nozzle temp | `nozzle_temp_*` | yes | yes | maybe custom |
| bed temp | `bed_temp_*` | yes | yes | maybe custom |
| full weight | `full_weight_g` | yes | yes | maybe |
| remaining weight | `remaining_weight_g` | yes | yes | yes |
| full length | `full_length_mm` | yes | yes | no / custom |
| remaining length | `remaining_length_mm` | yes | yes | no / custom |
| spoolman id | `spoolman_id` | optional | optional | yes |
| lot / GTIN | metadata | optional | optional | project-only |

## Good implementation order

### Phase 1 — Complete ✓
- ✓ `SpoolInfo` and `SpoolAssignment` dataclasses (`state/models.py`)
- ✓ Moonraker DB persistence (`state/moonraker_db.py`)
- ✓ OpenTag3D parser (`opentag3d/parser.py`)
- ✓ OpenPrintTag scanner parser (`openprinttag/scanner_parser.py`)
- ✓ Format dispatcher with auto-detection (`adapters/dispatcher.py`)
- ✓ Spoolman client with merge, weight sync, UID write-back (`spoolman/client.py`)
- ✓ Isolated test suite (no hardware required)

### Phase 2 — Next
- wire dispatcher into MQTT subscription (fork of spoolsense.py or new entry point)
- add single-tool adapter
- add multi-tool adapter
- add AFC adapter
- add startup restore from Moonraker DB

### Phase 3
- add Spoolman enrichment and sync policies
- add write-back behavior if needed
- support `tag_only` and `prefer_tag` source modes end-to-end

### Phase 4
- write remaining weight back to tag after print
- add UI views
- add policy checks and validation

## Bottom line

For a project that supports single-tool, multi-tool, and AFC systems:

- OpenPrintTag should be the primary local source
- Spoolman should be optional
- AFC should be an adapter
- Moonraker DB should hold canonical state

That gives you one architecture that works everywhere without forcing the project to depend on AFC or Spoolman.

