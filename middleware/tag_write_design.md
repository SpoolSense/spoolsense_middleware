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

Not all conditions are implemented in Phase 1 — see Future Phases for the rollout plan.

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
compare tag vs Spoolman remaining
   ↓
activate spool
   ↓
if stale → publish write command to scanner
else     → do nothing
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

> **Note:** Response correlation is aspirational for Phase 1. The
> `openprinttag_scanner` command response topic and schema are not yet
> documented. Phase 1 may publish write commands fire-and-forget without
> waiting for a response. Full response handling is a Phase 2 concern.

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

# TagWritePlan

`build_write_plan` returns a `TagWritePlan` dataclass or `None` if no write is needed.

```python
from dataclasses import dataclass
from typing import Any, Literal

@dataclass
class TagWritePlan:
    device_id: str                              # Scanner deviceId extracted from the MQTT scan topic
                                                # (openprinttag/<deviceId>/...)
    uid: str                                    # NFC tag UID to target
    command: Literal["update_remaining", "write_tag"]  # Allowed write commands
    payload: dict[str, Any]                     # Command payload
    reason: str | None = None                   # Optional — logged when the write is dispatched
```

For Phase 1, `command` will be whatever the scanner firmware expects for a
remaining-weight update. This is currently undocumented in the scanner firmware
and must be confirmed before Phase 1 can ship — see `openprinttag-notes.md`.

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

Prevents accidental overwrites from stale or incorrect Spoolman values (e.g. a
filament profile with a wrong nominal weight). If a user has manually corrected
the Spoolman value upward (e.g. after weighing a spool), the tag will not be
updated automatically. Use the Phase 3 manual rewrite / provisioning tools to
correct the tag in that case.

---

# Future Phases

## Phase 1 — Scan-time stale-tag reconciliation

Triggered when a user scans a tag and SpoolSense detects the tag is out of date.

- Compare tag remaining vs Spoolman remaining at scan time
- If Spoolman is lower → write updated remaining to the tag (Case B)
- If tag is missing remaining → write Spoolman value to the tag (Case C)

This is achievable because the scanner that saw the tag is already known, the tag
is physically present, and the UID is fresh and confirmed.

## Phase 2 — Post-print proactive writeback

Triggered when a print completes and Spoolman remaining changes.

- Push updated remaining to the tag immediately if it is present at a scanner
- Defer the write until the next scan if the tag is not currently present

This is harder than Phase 1: the spool may have been removed from the scanner,
the target device must be determined, writes may need to be queued, and
cmd/response correlation becomes more important.

## Phase 3 — Tag provisioning and repair

Full tooling for blank tags, corrupted tags, missing metadata, and manual rewrites.

---

# Summary

Design goals:

- Spoolman remains the source of truth
- Tags act as portable metadata snapshots
- Writes are conditional and safe
- Tag writing failures do not impact spool activation

---

# Future Considerations

## Write loop protection

If the scanner republishes tag state after a successful write (e.g. a fresh
`tag/state` MQTT message), SpoolSense must not treat that republish as a new
stale-tag event and issue another write. Tag writes should not immediately
trigger another write cycle. This can be addressed by tracking the last write
per UID/device or by suppressing writeback for a short window after a write
command is dispatched.
