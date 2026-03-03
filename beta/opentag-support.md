# OpenTag3D / OpenPrintTag Support (Future / Speculative)

> ⚠️ **Status: Aspirational — Not Actively Planned**
>
> The features described in this document are only practical if filament manufacturers begin shipping spools with pre-encoded NFC tags at the factory. Until OpenTag3D or OpenPrintTag sees significant real-world manufacturer adoption, this is a design reference, not an active roadmap item.

---

## Background

Two competing NFC filament tag standards exist as of early 2026:

### OpenTag3D
- Community-driven open standard
- Uses NTAG213/215/216 tags (ISO 14443A) — **same tags our PN532 hardware already reads**
- Supported by: Polar Filament, American Filament, Numakers, 3D Fuel, Ecogenesis
- Spec: https://opentag3d.info/spec.html

### OpenPrintTag (Prusa)
- Launched October 2025, ships on all new Prusament spools
- Uses ISO 15693 tags — **different protocol, requires hardware investigation**
- Likely to gain broad adoption due to Prusament's scale
- Spec: https://openprinttag.org

---

## Why Auto-Creation Only Makes Sense With Pre-Encoded Tags

Right now, NFC tags in this project are blank stickers that store only a UID. The UID is used as a key to look up filament data in Spoolman, which the user entered manually. This works well.

Auto-creating Spoolman entries from tag data only adds value if:

1. The tag was **pre-encoded by the manufacturer** at the factory (filament type, color, temps, weight already on the tag when you receive the spool)
2. The user has **zero manual data entry** — scan the spool, Spoolman entry appears automatically

If the user still has to program the tag themselves, they might as well just enter the data in Spoolman directly. There is no time savings.

---

## Future Workflow (If Manufacturer Adoption Occurs)

If a user scans a tag that is not found in Spoolman but contains OpenTag3D data:

1. Read tag fields: `material`, `color_hex`, `brand`, `extruder_temp`, `bed_temp`, `weight`
2. Find or create vendor in Spoolman via `GET /api/v1/vendor?name=<brand>`
3. Find or create filament via `GET /api/v1/filament?vendor_id=<id>&material=<material>`, match on `color_hex`
4. Create spool via `POST /api/v1/spool` with `filament_id` and NFC UID in `extra.nfc_id`
5. Flash LED green 3× to signal "new spool registered"

### API Calls Reference

```python
# 1. Find or create vendor
GET  /api/v1/vendor?name=Polymaker
POST /api/v1/vendor           { "name": "Polymaker" }

# 2. Find or create filament
GET  /api/v1/filament?vendor_id=3&material=PETG
POST /api/v1/filament         {
                                "name": "PolyLite PETG Blue",
                                "vendor_id": 3,
                                "material": "PETG",
                                "color_hex": "1A6BD4",
                                "diameter": 1.75,
                                "weight": 1000,
                                "spool_weight": 200,
                                "density": 1.27,
                                "settings_extruder_temp": 240,
                                "settings_bed_temp": 80
                              }

# 3. Create spool
POST /api/v1/spool            {
                                "filament_id": 12,
                                "initial_weight": 1000,
                                "extra": { "nfc_id": "\"04:AB:CD:EF\"" }
                              }
```

**Notes:**
- `color_hex` has no `#` prefix
- `extra.nfc_id` must be a JSON-encoded string (escaped quotes), matching how the middleware reads it
- `extra` fields must be pre-registered as custom fields in Spoolman UI before writing via API

---

## Hardware Consideration for OpenPrintTag

Prusa's OpenPrintTag uses ISO 15693 (also called NFC-V / vicinity cards), which the PN532 does **not** support natively. Supporting Prusament spools would require either:

- A different NFC reader IC (e.g. ST25R3911B, which supports both 14443 and 15693)
- A dual-reader setup per toolhead

This is a significant hardware change and would require a custom PCB revision.

---

## When to Revisit

This feature becomes worth building when:

- [ ] OpenTag3D or OpenPrintTag has 5+ major filament brands encoding tags at the factory
- [ ] Users are actually receiving spools with pre-encoded NFC data
- [ ] ISO 15693 support becomes necessary (drives hardware revision)
