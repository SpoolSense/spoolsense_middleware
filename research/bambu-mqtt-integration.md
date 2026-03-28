# Bambu Lab MQTT Integration Research

Research compiled from SpoolEase (github.com/yanshay/spoolease) codebase analysis
and OpenBambuAPI community documentation. For use when implementing middleware issue #31.

This document is **self-contained** — all code-level details are included so we
don't need to re-analyze the SpoolEase codebase.

---

## 1. Connection

### MQTT Broker
- **Host:** `{printer_ip}:8883` (TLS required)
- **Username:** `bblp` (hardcoded, same for all printers)
- **Password:** LAN access code from printer LCD (converted to bytes)
- **Client ID:** Printer serial number
- **Protocol:** MQTT 3.1.1, clean session = true
- **Subscribe:** `device/{serial}/report` (QoS 1 — AtLeastOnce)
- **Publish:** `device/{serial}/request` (QoS 0 — AtMostOnce)

### TLS
- Bambu printers use a **custom CA certificate** (not a public CA)
- SpoolEase bundles 3 PEM certs for different printer models:
  - Default: `bambulab.pem` (X1C, P1S, A1, etc.)
  - P2S: `bambulab_p2s.pem`
  - H2C: `bambulab_h2c.pem`
- Certificate CN contains the printer's serial number
- **SNI required** when connecting by IP — set servername to the printer serial
- TLS version: 1.2
- Python 3.13+ has a `VERIFY_X509_STRICT` issue — may need workaround

### Python TLS Connection (pseudocode for our implementation)
```python
import ssl
import paho.mqtt.client as mqtt

# Create SSL context with Bambu CA cert
ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
ssl_ctx.load_verify_locations("bambulab.pem")

client = mqtt.Client(client_id=serial_number, protocol=mqtt.MQTTv311)
client.username_pw_set("bblp", access_code)
client.tls_set_context(ssl_ctx)
# SNI: paho-mqtt uses the hostname for SNI by default
# When connecting by IP, may need to override SNI to serial number
client.connect(printer_ip, 8883)
client.subscribe(f"device/{serial}/report", qos=1)
```

### Requires Developer Mode
| | LAN Only Mode | Developer Mode |
|---|---|---|
| Read printer status | Yes | Yes |
| Send commands | Auth verification blocks it | Accepted freely |
| FTP / camera stream | No | Yes |
| Enable | Printer LCD -> Settings | Enable LAN Only first, then toggle Dev Mode |

Both modes use the same access code. The difference is command authorization
verification — Dev Mode disables it.

### Keepalive & Reconnection
- SpoolEase uses a **20-second message timeout**
- If no message received for 20s, send `pushall` to check connectivity
- On connection failure: **full teardown and reconnect** (no partial recovery)
- Clear all queued messages before reconnecting
- MQTT buffer: starts at 32KB, grows by 8KB steps, max 48KB

---

## 2. Printer Discovery (Optional, v2+)

SpoolEase supports SSDP discovery — finds the printer by serial number on
the local network without knowing the IP. Nice-to-have, not required for v1.

---

## 3. AMS Hardware Types

Three different AMS types with different ID schemes:

| Hardware | AMS ID Range | Trays Per Unit | Total Slots | Notes |
|----------|-------------|----------------|-------------|-------|
| Standard AMS | 0-3 | 4 each | 16 | Most common (X1C, P1S, A1) |
| AMS-HT | 128-135 | 1 each | 8 | High-temp, single slot |
| External | 254, 255 | 1 each | 2 | No AMS, direct feed to extruder |

### Tray Index Mapping (internal SpoolEase convention)
```
Standard AMS:  tray_index = ams_id * 4 + tray_id    (0-15)
AMS-HT:        tray_index = 16 + (ams_id - 128)     (16-23)
External:      254 = left/extruder 1, 255 = right/extruder 0
```

### For ams_filament_setting Command
```
Standard AMS:  ams_id = tray_index / 4, tray_id = tray_index % 4, slot_id = tray_index % 4
AMS-HT:        ams_id = 128+, tray_id = 0, slot_id = 0
External:      ams_id = 254 or 255, tray_id = 0, slot_id = 0
```

### Supported Printer Lines
- X1 series (X1C, X1E) — Standard AMS
- P1 series (P1S, P1P) — Standard AMS
- A1 series (A1, A1 Mini) — AMS Lite (same protocol, 4 trays)
- H2 series (H2D) — Dual extruder, external slots 254/255
- P2 series (P2S) — AMS2-Pro, AMS-HT
- AMS-HT — High-temp single-slot units (ID 128-135)

---

## 4. Commands — Complete JSON Payloads

### 4.1 ams_filament_setting (Primary — set filament on tray)

```json
{
  "print": {
    "command": "ams_filament_setting",
    "sequence_id": "1",
    "ams_id": 0,
    "tray_id": 0,
    "slot_id": 0,
    "tray_info_idx": "GFL99",
    "setting_id": "",
    "tray_color": "FF0000FF",
    "nozzle_temp_min": 190,
    "nozzle_temp_max": 230,
    "tray_type": "PLA"
  }
}
```

**Field types and details:**
- `command`: string, always `"ams_filament_setting"`
- `sequence_id`: string (not int), e.g. `"1"` — fire-and-forget, not for matching
- `ams_id`: i32 — AMS unit ID (0-3 standard, 128-135 HT, 254/255 external)
- `tray_id`: i32 — tray within AMS (0-3 for standard, 0 for HT/external)
- `slot_id`: i32 — **separate from tray_id** (same as tray_id for standard, 0 for HT/external)
- `tray_info_idx`: string — slicer filament profile ID (see section 5)
- `setting_id`: optional string — calibration setting ID (empty string or omit)
- `tray_color`: string — **RRGGBBAA hex** (8 chars, alpha always FF)
- `nozzle_temp_min`: u32 — min nozzle temp in Celsius
- `nozzle_temp_max`: u32 — max nozzle temp in Celsius
- `tray_type`: string — material type ("PLA", "ABS", "PETG", etc.)

### 4.2 extrusion_cali_sel (K-factor / pressure advance selection)

Sent immediately after ams_filament_setting.

```json
{
  "print": {
    "command": "extrusion_cali_sel",
    "sequence_id": "1",
    "cali_idx": -1,
    "filament_id": "GFL99",
    "nozzle_diameter": "0.4",
    "ams_id": 0,
    "tray_id": 0,
    "slot_id": 0
  }
}
```

**Field types:**
- `cali_idx`: i32 — calibration index (-1 = no calibration, use this for v1)
- `filament_id`: string — same as tray_info_idx
- `nozzle_diameter`: string — e.g. "0.4", "0.6", "0.8"

### 4.3 extrusion_cali_set (Write K-factor to printer, v2+)

```json
{
  "print": {
    "command": "extrusion_cali_set",
    "sequence_id": "1",
    "nozzle_diameter": "0.4",
    "filaments": [
      {
        "ams_id": 0,
        "extruder_id": 0,
        "filament_id": "GFL99",
        "k_value": "0.150000",
        "n_coef": "0.000000",
        "name": "My PLA Calibration",
        "nozzle_diameter": "0.4",
        "nozzle_id": "HS00-0.4",
        "setting_id": "",
        "slot_id": 0,
        "tray_id": 0
      }
    ]
  }
}
```

### 4.4 pushall (Request full printer status)

```json
{
  "pushing": {
    "command": "pushall"
  }
}
```

**WARNING:** On P1 series printers, don't send pushall more than every 5 minutes.

### 4.5 get_version (Get firmware info)

```json
{
  "info": {
    "command": "get_version"
  }
}
```

### 4.6 extrusion_cali_get (Fetch calibration data)

```json
{
  "print": {
    "command": "extrusion_cali_get",
    "sequence_id": "1",
    "filament_id": "",
    "nozzle_diameter": "0.4"
  }
}
```

### 4.7 Reset a Tray (clear filament info)

Send ams_filament_setting with empty/zero values:
```json
{
  "print": {
    "command": "ams_filament_setting",
    "sequence_id": "1",
    "ams_id": 0,
    "tray_id": 0,
    "slot_id": 0,
    "tray_info_idx": "",
    "tray_color": "",
    "nozzle_temp_min": 0,
    "nozzle_temp_max": 0,
    "tray_type": ""
  }
}
```

### Lock Mode Check

Before sending any command, check the printer's lock state. The `fun` field
in report messages contains a hex bitmask — bit `0x20000000` indicates locked mode.
Commands will be rejected when locked.

```python
def is_locked(fun_hex: str) -> bool:
    if not fun_hex:
        return False
    return (int(fun_hex, 16) & 0x20000000) != 0
```

---

## 5. The tray_info_idx Field — Complete Lookup Table

This is the **slicer filament profile ID**. Without it, the printer can't apply
slicer-specific settings (like flow rate, retraction, speed profiles).

### Generic IDs (use for SpoolSense v1 — map from material_type)

| Material | tray_info_idx | temp_min | temp_max |
|----------|--------------|----------|----------|
| PLA | GFL99 | 190 | 240 |
| ABS | GFB99 | 240 | 280 |
| ASA | GFB98 | 240 | 280 |
| PETG | GFG99 | 220 | 270 |
| TPU | GFU99 | 200 | 250 |
| PA/Nylon | GFN99 | 240 | 280 |
| PC | GFC99 | 260 | 290 |
| PVA | GFS99 | 190 | 240 |
| HIPS | GFS98 | 220 | 270 |
| BVOH | GFS97 | 190 | 240 |
| PLA-CF | GFL98 | 190 | 240 |
| PETG-CF | GFG98 | 220 | 270 |
| PA-CF | GFN98 | 260 | 300 |
| PA6-CF | GFN05 | 260 | 300 |
| ASA-CF | GFB51 | 240 | 280 |
| PC-FR | GFC01 | 260 | 290 |
| PCTG | GFG97 | 240 | 270 |
| PP | GFP97 | 220 | 250 |
| PP-CF | GFP96 | 220 | 250 |
| PP-GF | GFP95 | 220 | 250 |
| PE | GFP99 | 175 | 220 |
| PE-CF | GFP98 | 175 | 220 |
| PHA | GFR98 | 190 | 240 |
| EVA | GFR99 | 175 | 220 |
| PPS | GFT97 | 300 | 340 |
| PPS-CF | GFT98 | 310 | 340 |
| PET-CF | GFT01 | 220 | 260 |
| PPA-CF | GFN97 | 280 | 320 |
| PPA-GF | GFN96 | 280 | 320 |
| TPU-AMS | GFU98 | 200 | 250 |

### Python Mapping (for publishers/bambu.py)

```python
# SpoolSense material_type enum → (tray_info_idx, tray_type, temp_min, temp_max)
MATERIAL_TO_BAMBU = {
    "PLA":     ("GFL99", "PLA",     190, 240),
    "PETG":    ("GFG99", "PETG",    220, 270),
    "ABS":     ("GFB99", "ABS",     240, 280),
    "ASA":     ("GFB98", "ASA",     240, 280),
    "TPU":     ("GFU99", "TPU",     200, 250),
    "PC":      ("GFC99", "PC",      260, 290),
    "PA":      ("GFN99", "PA",      240, 280),
    "PA6":     ("GFN05", "PA6-CF",  260, 300),
    "PVA":     ("GFS99", "PVA",     190, 240),
    "HIPS":    ("GFS98", "HIPS",    220, 270),
    "BVOH":    ("GFS97", "BVOH",    190, 240),
    "PCTG":    ("GFG97", "PCTG",    240, 270),
    "PP":      ("GFP97", "PP",      220, 250),
    "PPS":     ("GFT97", "PPS",     300, 340),
    "PHA":     ("GFR98", "PHA",     190, 240),
    "PET":     ("GFT01", "PET-CF",  220, 260),
    "PEI":     ("GFL99", "PLA",     190, 240),  # fallback
    "PBT":     ("GFL99", "PLA",     190, 240),  # fallback
    "PVB":     ("GFL99", "PLA",     190, 240),  # fallback
    "CPE":     ("GFG99", "PETG",    220, 270),  # closest match
    "TPE":     ("GFU99", "TPU",     200, 250),  # closest match
    "TPC":     ("GFU99", "TPU",     200, 250),  # closest match
    "PEKK":    ("GFT97", "PPS",     300, 340),  # closest match
    "PEEK":    ("GFT97", "PPS",     300, 340),  # closest match
}

def get_bambu_filament_info(material_type: str) -> tuple:
    """Returns (tray_info_idx, tray_type, temp_min, temp_max)"""
    return MATERIAL_TO_BAMBU.get(material_type, ("GFL99", "PLA", 190, 240))
```

### Bambu-Specific IDs (from Bambu Lab RFID tags — Phase 4)

Complete list from SpoolEase `base-filaments-index.csv`:

**PLA Family:**
- `GFA00` Bambu PLA Basic (190-240)
- `GFA01` Bambu PLA Matte (190-240)
- `GFA05` Bambu PLA Silk (190-240)
- `GFA06` Bambu PLA Silk+ (190-240)
- `GFA07` Bambu PLA Marble (190-240)
- `GFA09` Bambu PLA Tough (190-240)
- `GFA11` Bambu PLA Aero (210-260)
- `GFA16` Bambu PLA Wood (190-240)
- `GFA50` Bambu PLA-CF (210-250)
- `GFL00` PolyLite PLA (190-240)
- `GFL01` PolyTerra PLA (190-240)
- `GFL95` Generic PLA High Speed (190-240)
- `GFL96` Generic PLA Silk (190-240)

**ABS/ASA Family:**
- `GFB00` Bambu ABS (240-280)
- `GFB01` Bambu ASA (240-280)
- `GFB02` Bambu ASA-Aero (240-280)
- `GFB50` Bambu ABS-GF (240-280)
- `GFB51` Bambu ASA-CF (250-280)
- `GFB60` PolyLite ABS (240-280)
- `GFB61` PolyLite ASA (240-280)

**PETG Family:**
- `GFG00` Bambu PETG Basic (230-270)
- `GFG01` Bambu PETG Translucent (230-270)
- `GFG02` Bambu PETG HF (230-270)
- `GFG50` Bambu PETG-CF (240-270)
- `GFG60` PolyLite PETG (220-260)
- `GFG96` Generic PETG HF (220-270)

**Nylon/PA Family:**
- `GFN03` Bambu PA-CF (260-300)
- `GFN04` Bambu PAHT-CF (260-300)
- `GFN05` Bambu PA6-CF (260-300)
- `GFN06` Bambu PPA-CF (280-320)
- `GFN07` Bambu PPA-GF (280-320)
- `GFN08` Bambu PA6-GF (260-300)

**TPU Family:**
- `GFU00` Bambu TPU 95A HF (200-250)
- `GFU01` Bambu TPU 95A (200-250)
- `GFU02` Bambu TPU for AMS (220-240)

**Support Materials:**
- `GFS00` Bambu Support W (190-240)
- `GFS01` Bambu Support G (260-300)
- `GFS02` Bambu Support For PLA (190-240)
- `GFS04` Bambu PVA (210-250)
- `GFS06` Bambu Support for ABS (240-270)

**Specialty:**
- `GFT01` Bambu PET-CF (260-290)
- `GFT02` Bambu PPS-CF (310-340)
- `GFL50` Fiberon PA6-CF (280-300)
- `GFL51` Fiberon PA6-GF (280-300)
- `GFL52` Fiberon PA12-CF (260-300)
- `GFL54` Fiberon PET-CF (270-300)
- `GFL55` Fiberon PETG-rCF (240-270)

---

## 6. Data Mapping: SpoolSense -> Bambu

### Tag Field Mapping

| SpoolSense Tag Field | Bambu MQTT Field | Conversion |
|---------------------|-----------------|------------|
| `color[0..2]` (RGB) | `tray_color` | `f"{R:02X}{G:02X}{B:02X}FF"` |
| `material_type` enum | `tray_type` | Lookup MATERIAL_TO_BAMBU table |
| `material_type` enum | `tray_info_idx` | Lookup MATERIAL_TO_BAMBU table |
| `min_print_temp` | `nozzle_temp_min` | Direct (Celsius). If 0, use table default |
| `max_print_temp` | `nozzle_temp_max` | Direct (Celsius). If 0, use table default |

### Color Format

- Bambu uses **RRGGBBAA** (8 chars) not RRGGBB (6 chars)
- Alpha byte is always `FF` for solid colors
- SpoolSense tags store RGBA in `color[4]` (R=0, G=1, B=2, A=3)

```python
def spoolsense_color_to_bambu(color: list[int]) -> str:
    """Convert SpoolSense [R, G, B, A] to Bambu 'RRGGBBFF' hex string."""
    return f"{color[0]:02X}{color[1]:02X}{color[2]:02X}FF"
```

### Multi-Color Filaments (edge case for v2)
- Secondary color stored on Bambu tags at block 16, bytes 4-7
- Byte order is **reversed ABGR** (not RGBA)
- Number of colors at block 16, bytes 2-3 (little-endian i16)

---

## 7. Report Message Structure (device/{serial}/report)

### Top-Level Message Wrapper

```json
{
  "print": { ... }
}
```

Or for other message types:
```json
{
  "info": { ... }
}
```

### PrintData Fields (all optional — only changed fields sent)

```python
# Key fields from the print report message
class PrintData:
    gcode_state: str       # "IDLE", "RUNNING", "PAUSE", "FINISH", "FAILED", etc.
    sequence_id: str       # Message sequence ID
    command: str           # Command type (for responses)
    result: str            # "success" or error
    reason: str            # Error reason

    # AMS status
    ams: PrintAms          # AMS data (see below)
    vt_tray: PrintTray     # External tray (old firmware)
    vir_slot: list[PrintTray]  # Virtual slots (new firmware, H2D)

    # Print progress
    layer_num: int
    total_layer_num: int
    subtask_name: str
    project_id: str

    # AMS mapping for current print
    ams_mapping: list[int]    # Old format: [-1, -1, -1, 1, 0]
    ams_mapping2: list[dict]  # New format: [{"ams_id": 0, "slot_id": 0}]
    use_ams: bool

    # Filament setting response fields
    tray_id: int
    slot_id: int
    ams_id: int
    tray_info_idx: str
    tray_type: str
    tray_color: str
    nozzle_temp_min: int
    nozzle_temp_max: int
    cali_idx: int

    # Device state
    device: PrintDevice    # Extruder/nozzle info
    fun: str               # Function bitmask (hex), bit 0x20000000 = locked

    # Calibration response
    filaments: list[dict]  # Calibration data from extrusion_cali_get
    nozzle_diameter: str
    filament_id: str
```

### PrintAms Structure

```python
class PrintAms:
    ams: list[PrintAmsData]       # List of AMS units
    ams_exist_bits: str           # Hex, which AMS units present (e.g., "1" = AMS 0)
    tray_exist_bits: str          # Hex, which trays have spools
    tray_is_bbl_bits: str         # Hex, which trays have Bambu Lab spools
    tray_tar: str                 # Target tray (string-encoded int)
    tray_now: str                 # Current tray (string-encoded int)
    tray_pre: str                 # Previous tray (string-encoded int)
    tray_read_done_bits: str      # Hex, which trays are fully read
    tray_reading_bits: str        # Hex, which trays are currently rotating
```

### PrintAmsData (Single AMS Unit)

```python
class PrintAmsData:
    id: str          # AMS ID as string (e.g., "0", "128")
    humidity: str     # Humidity reading
    tray: list[PrintTray]  # Trays in this AMS
```

### PrintTray Structure

```python
class PrintTray:
    id: str                 # Tray ID as string (e.g., "0", "1")
    k: float                # K-factor (pressure advance)
    cali_idx: int           # Calibration index
    tray_info_idx: str      # Filament profile ID (e.g., "GFL99")
    tray_type: str          # Material type (e.g., "PLA")
    tray_color: str         # RRGGBBAA hex (e.g., "2323F7FF")
    nozzle_temp_max: str    # Max temp as STRING (not int!)
    nozzle_temp_min: str    # Min temp as STRING (not int!)
```

**IMPORTANT:** Many numeric fields in report messages are **string-encoded**
(e.g., `"190"` not `190`). The commands use actual ints, but reports use strings.

### PrintDevice Structure

```python
class PrintDevice:
    extruder: {
        "info": [{"id": 0, "snow": 0, "spre": 0, "star": 0}],
        "state": 0
    }
    nozzle: {
        "info": [{"id": 0, "diameter": 0.4, "type": "HS00-0.4"}],
        "exist": 1,
        "state": 0
    }
```

### Tray States (derived, not a direct field)

| State | Standard AMS | External Slot | How to Determine |
|-------|-------------|---------------|-----------------|
| Empty | Yes | Yes | tray_exist_bits bit = 0 |
| Reading | Yes (rotating) | No | tray_reading_bits bit = 1 |
| Ready | Yes (read, idle) | No | tray_read_done_bits = 1, not active |
| Loaded | Yes (in extruder) | Yes | tray_now or tray_tar matches |

### P1 Series Quirk
P1 printers **only report changed values** — not the full state on every message.
Must request `pushall` for full state, but **no more than every 5 minutes**.

---

## 8. Bambu RFID Tag Decryption (Scanner #24)

SpoolEase has cracked Bambu Lab's MIFARE Classic tag encryption.

### Key Derivation (HKDF-SHA256)

```
Master Key (16 bytes):
  [0x9a, 0x75, 0x9c, 0xf2, 0xc4, 0xf7, 0xca, 0xff,
   0x22, 0x2c, 0xb9, 0x76, 0x9b, 0x41, 0xbc, 0x96]

Context: b"RFID-A\0" (7 bytes, null-terminated)

Input: Tag UID (typically 4 or 7 bytes)

Output: 96 bytes = 16 sector keys x 6 bytes each
```

### HKDF Algorithm (exact implementation)

```python
import hmac
import hashlib

def bambulab_keys(uid: bytes) -> list[bytes]:
    """Derive 16 MIFARE Classic Key A values from tag UID."""
    master = bytes([
        0x9a, 0x75, 0x9c, 0xf2, 0xc4, 0xf7, 0xca, 0xff,
        0x22, 0x2c, 0xb9, 0x76, 0x9b, 0x41, 0xbc, 0x96
    ])
    context = b"RFID-A\x00"
    num_keys = 16
    key_length = 6
    total_length = num_keys * key_length  # 96 bytes
    hash_len = 32  # SHA-256 output

    # HKDF-Extract
    prk = hmac.new(master, uid, hashlib.sha256).digest()

    # HKDF-Expand
    okm = b""
    t = b""
    for i in range(1, (total_length + hash_len - 1) // hash_len + 1):
        t = hmac.new(prk, t + context + bytes([i]), hashlib.sha256).digest()
        okm += t

    # Split into 16 x 6-byte keys
    keys = []
    for i in range(num_keys):
        keys.append(okm[i * key_length : (i + 1) * key_length])
    return keys
```

### Tag Data Layout

| Block | Sector | Bytes | Field | Format | Example |
|-------|--------|-------|-------|--------|---------|
| 1 | 0 | 0-7 | Material Variant ID | ASCII string | "A00-G1" |
| 1 | 0 | 8-15 | Material ID (= tray_info_idx) | ASCII string | "GFA00" |
| 2 | 0 | 0-15 | Filament Type | ASCII string | "PLA" |
| 4 | 1 | 0-15 | Detailed Type | ASCII string | "PLA Basic" |
| 5 | 1 | 0-3 | Color RGBA | 4 bytes raw | [0xFF, 0x00, 0x00, 0xFF] |
| 5 | 1 | 4-5 | Spool Weight (grams) | Little-endian i16 | 250 |
| 6 | 1 | — | (additional data) | — | — |
| 13 | 3 | — | (additional data) | — | — |
| 16 | 4 | 2-3 | Secondary Color Count | Little-endian i16 | 0 or 1 |
| 16 | 4 | 4-7 | Secondary Color RGBA | **Reversed ABGR** | [0xFF, 0x00, 0x00, 0xFF] |

### Blocks Read by SpoolEase
`[1, 2, 4, 5, 6, 13, 16]` — spans sectors 0, 1, 3, and 4.

### Authentication Details
- MIFARE Classic 1K = 16 sectors x 4 blocks x 16 bytes
- Each sector requires **separate authentication** with its own Key A
- Key A for sector N = `bambulab_keys(uid)[N]`
- Cache the currently authenticated sector — only re-auth when switching sectors
- Auth failure error code: `0x14` (indicates non-Bambu MIFARE tag)
- Auth failure recovery: clear crypto state, disable CRC, force transceiver idle,
  flush RX data, poll for idle (50ms timeout), clear all IRQs

### ESP32 Implementation Notes (for scanner #24)
- `mbedtls` is already available on ESP32, provides HMAC-SHA256
- MIFARE Classic auth needs `mifareAuthenticate(keyA, key, blockNo, uid)` in PN5180 lib
- After auth: set `MFC_CRYPTO_ON` bit in `SYSTEM_CONFIG` register
- On failure: clear crypto bit (`SYSTEM_CONFIG & 0xFFFFFFBF`)

---

## 9. Firmware Version Quirks

### External Tray ID Format
- **Old firmware:** `tray_id = 254`, no `ams_id` field in message
- **New firmware:** `ams_id = 254 or 255`, `tray_id = 0`
- Must handle both formats when parsing report messages

```python
def parse_external_tray(msg: dict) -> tuple[int, int]:
    """Returns (ams_id, tray_id) handling both firmware formats."""
    ams_id = msg.get("ams_id")
    tray_id = msg.get("tray_id", 0)
    if ams_id is None and tray_id == 254:
        # Old firmware
        return (254, 0)
    return (ams_id, tray_id)
```

### Partial Updates for External Trays
- Messages with `id: None` for external tray = partial update
- SpoolEase workaround: request full `pushall` instead of parsing partial
- Happens when user changes external filament via printer display

### Dual External Slots (H2D printers)
- Newer firmware uses `vir_slot` array instead of `vt_tray`
- One entry per external slot
- Must check both `vt_tray` and `vir_slot` in report messages

### Nozzle Type Detection
- Nozzle type from `device.nozzle.info[].type` string (e.g., "HS00-0.4")
- High-flow nozzle: type string starts with "HH" (e.g., "HH00-0.6")
- Standard nozzle: starts with "HS" (e.g., "HS00-0.4")
- Affects calibration matching in v2+

### String-Encoded Numbers
Many fields in report messages are strings, not numbers:
- `tray.id` — string "0", "1", etc.
- `tray.nozzle_temp_min/max` — string "190", "240"
- `ams.id` — string "0"
- `tray_tar`, `tray_now`, `tray_pre` — string-encoded ints

Commands use actual ints. Parse carefully.

---

## 10. NFC Tag Quirks (from SpoolEase)

### PN532-Specific (they use PN532, we use PN5180)
- 10ms delay needed after RF field establishment before writes
- After failed page reads, must re-run target selection to clear error state
- **No retry on write failures** — single attempt only (prevents bricking)
- 500ms debounce window for same-tag rescans
- PN532 wake-up after tag emulation: send INRELEASE, may fail first time, retry up to 5x

### MIFARE Classic
- Each sector needs separate auth — cache current sector number
- Auth failure recovery sequence:
  1. Clear Crypto1 bit (`SYSTEM_CONFIG & 0xFFFFFFBF`)
  2. Disable CRC
  3. Force transceiver idle
  4. Flush stale RX data
  5. Poll for idle state (50ms timeout)
  6. Clear all IRQs
- SpoolEase treats MIFARE as **read-only** — does not write to Bambu tags

### NTAG
- Auto-detect size by testing boundary pages:
  - Page 44 → NTAG213 (180 bytes)
  - Page 134 → NTAG215 (540 bytes)
  - Page 230 → NTAG216 (924 bytes)
- Error code `0x19` = "page not available" (used for size detection)
- Page 3 contains manufactured properties (UID, BCC, lock bits) — never overwrite
- NDEF write starts from page 4

### Tag Classification by SAK
- `SAK 0x00` → NTAG (Type 2 tag)
- `SAK 0x08` → MIFARE Classic 1K (likely Bambu tag)
- `SAK 0x18` → MIFARE Classic 4K
- SpoolSense already does this in `NFCManager::classifyTag()`

---

## 11. Implementation Plan for SpoolSense

### Phase 1: Basic Bambu Publisher (middleware #31)
1. New `publishers/bambu.py` implementing Publisher ABC
2. TLS MQTT client to `printer_ip:8883` using paho-mqtt + ssl
3. Bundle Bambu CA cert (obtain from OpenBambuAPI or ha-bambulab repos)
4. Send `ams_filament_setting` on spool assignment event
5. Send `extrusion_cali_sel` with `cali_idx = -1` immediately after
6. Use `MATERIAL_TO_BAMBU` lookup table for `tray_info_idx`
7. Config: `printer_ip`, `serial`, `access_code`, scanner-to-tray mapping
8. Handle standard AMS only (IDs 0-3, 4 trays each) for v1
9. RRGGBBAA color format with FF alpha
10. Use tag temps if available, fall back to table defaults if 0

### Phase 2: AMS State Awareness
1. Subscribe to `device/{serial}/report`
2. Parse AMS tray status (exist/read_done/reading bits)
3. Track what's loaded where — maintain local state
4. Publish state to SpoolSense MQTT for scanner display
5. Handle string-encoded numbers in report messages
6. Lock mode detection (check `fun` bitmask before commands)

### Phase 3: Extended Hardware Support
1. AMS-HT support (IDs 128-135, single slot each)
2. External slot support (IDs 254, 255)
3. Handle both old and new firmware tray ID formats
4. Handle `vir_slot` array for H2D printers
5. P1 series: rate-limit `pushall` to every 5 minutes

### Phase 4: Bambu Tag Reading (scanner side)
1. Implement MIFARE Classic auth in PN5180 library (scanner #24)
2. Port HKDF-SHA256 key derivation to ESP32 using mbedtls
3. Read Bambu tag data: material_id, color, weight, detailed type
4. Send real `tray_info_idx` from tag instead of generic IDs
5. Parse spool weight from block 5

### Phase 5: Advanced Features
1. K-factor / pressure advance auto-restore (extrusion_cali_set)
2. SSDP printer discovery (find printer by serial on network)
3. Print monitoring (filament usage tracking via gcode_state)
4. Multi-printer support (up to 5, per SpoolEase's limit)
5. Calibration data fetch and matching

---

## 12. Dependencies

### Python Libraries (middleware)
- `paho-mqtt` — already used for SpoolSense MQTT
- `ssl` — stdlib, for TLS connection
- Bambu CA cert — bundle as PEM file (get from ha-bambulab or OpenBambuAPI)

### ESP32 Libraries (scanner, Phase 4)
- `mbedtls` — already available on ESP32, provides HMAC-SHA256 for key derivation
- MIFARE Classic auth in PN5180 library — needs implementation (scanner #24)

### Bambu CA Certificate
Can be extracted from:
- SpoolEase: `spoolease/core/src/certs/bambulab.pem` (locally available)
- ha-bambulab: GitHub repo
- OpenBambuAPI: `tls.md` references it

---

## 13. Config Schema (for middleware config.yaml)

```yaml
bambu:
  enabled: true
  printer_ip: "192.168.1.100"
  serial: "01P00A000000000"
  access_code: "12345678"
  # Which AMS tray to set when a scanner reports a spool
  tray_mapping:
    # scanner_device_id: { ams_id, tray_id }
    f3d360: { ams_id: 0, tray_id: 0 }
    a1b2c3: { ams_id: 0, tray_id: 1 }
  # Optional: override pushall interval (default 300s for P1, 30s otherwise)
  pushall_interval_sec: 300
```

---

## 14. References

- [OpenBambuAPI](https://github.com/Doridian/OpenBambuAPI) — community MQTT docs
- [ha-bambulab](https://github.com/greghesp/ha-bambulab) — HA integration (Python reference)
- [SpoolEase](https://github.com/yanshay/spoolease) — Rust ESP32 implementation (source of this analysis)
- SpoolSense middleware #31 — Bambu support issue
- SpoolSense scanner #24 — MIFARE Classic auth
- SpoolSense scanner #6 — Direct printer API mode
- Local copy of SpoolEase: `~/Code/spoolease/`
