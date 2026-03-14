# Tag Writeback Design (SpoolSense + openprinttag_scanner)

## Overview

`openprinttag_scanner` should be responsible for **physically writing NFC tags**.

SpoolSense should be responsible for **deciding what data should be written and when**.

This keeps responsibilities clean:

| Component | Responsibility |
|-----------|---------------|
| SpoolSense | Business logic, Spoolman sync, policy decisions |
| openprinttag_scanner | NFC tag presence detection, UID validation, CBOR/NDEF encoding, PN5180 writes |

SpoolSense communicates write requests to the scanner via **MQTT command topics** already implemented by the scanner firmware.

---

# Source-of-Truth Rule

Spoolman is the authoritative data source.

The NFC tag is treated as a **portable snapshot or cache** of spool metadata.

Meaning:

- Spoolman holds the real state
- Tag updates are best-effort
- Failure to write a tag **must never break spool activation or Spoolman sync**

---

# When Tags Should Be Written

Tag writes should be **conditional**, not automatic.

Only write when one of these conditions occurs.

---

## 1. After a Print Completes

Remaining filament has changed and the tag should reflect the new state.

Example:

Tag before print:

742g

After print Spoolman value:

701g

Write new remaining value to the tag.

---

## 2. When a Scan Detects a Stale Tag

If SpoolSense detects the tag's stored remaining value is higher than Spoolman's value.

Example:

Tag:

742g

Spoolman:

701g

Tag is out of date → write Spoolman value to tag.

---

## 3. Tag Provisioning or Repair

Situations where the tag needs metadata written:

- blank tag
- corrupted tag
- missing metadata
- manual rewrite requested

---

# Write Decision Logic

For remaining filament updates:

```
if spoolman_remaining is None:
    do_not_write()

if tag_remaining is None:
    write(spoolman_remaining)

if spoolman_remaining < tag_remaining:
    write(spoolman_remaining)

else:
    do_not_write()
```

This ensures:

- Tags only move **downward** in remaining filament
- Prevents accidental overwrites from stale or incorrect Spoolman values

---

# High Level Flow

```
scan tag
   ↓
parse ScanEvent
   ↓
sync/resolve spool in Spoolman
   ↓
activate spool
   ↓
compare tag vs Spoolman remaining
   ↓
if stale
    publish write command to scanner
else
    do nothing
```

Important rules:

- Spool activation **must not depend on tag write success**
- Spoolman sync **must not depend on tag write success**
- Tag writing is **best effort**

---

# Code Layout

## spoolman/client.py

Responsible only for Spoolman API communication.

Responsibilities:

- lookup spool by UID
- vendor lookup/creation
- filament lookup/creation
- spool creation
- weight synchronization

No tag-write logic should exist here.

---

## tag_sync/policy.py

Contains pure decision logic.

Example functions:

```python
def should_write_remaining(tag_remaining, spoolman_remaining) -> bool

def build_write_plan(scan_event, spool_info) -> TagWritePlan | None
```

This module decides:

- whether a tag write should occur
- what fields should be written

---

## tag_sync/scanner_writer.py

MQTT interface to `openprinttag_scanner`.

Example functions:

```python
def update_remaining(device_id: str, uid: str, remaining_g: float)

def write_tag(device_id: str, uid: str, payload: dict)
```

Responsibilities:

- publish MQTT write commands
- listen for command responses
- correlate responses to requests

---

## spoolsense.py

Main orchestration layer.

Simplified flow:

```python
scan = detect_and_parse(...)
spool_info = try_sync_spool(...)
_activate_from_scan(...)

write_plan = build_write_plan(scan, spool_info)
if write_plan:
    scanner_writer.execute(write_plan)
```

---

# Example Behavior

## Case A – Fresh Tag

Tag: 742g  
Spoolman: 742g  

Result:

No write

---

## Case B – Stale Tag

Tag: 742g  
Spoolman: 701g  

Result:

Write 701g to tag

---

## Case C – Tag Missing Remaining

Tag: None  
Spoolman: 701g  

Result:

Write 701g

---

## Case D – Spoolman Higher

Tag: 701g  
Spoolman: 742g  

Result:

No automatic write

Prevents accidental overwrites.

---

# Future Phases

## Phase 1

Implement **remaining weight writeback when tag is stale**.

## Phase 2

Optional periodic tag synchronization.

## Phase 3

Full tag provisioning and repair tools.

---

# Summary

Design goals:

- Spoolman remains the source of truth
- Tags act as portable metadata snapshots
- Writes are conditional and safe
- Tag writing failures do not impact spool activation
