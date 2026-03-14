# Hardware Guide

This section helps determine what hardware you need to run SpoolSense depending on your printer setup and the type of NFC tags you want to use.

SpoolSense supports two NFC reader builds. Each reader requires **its own ESP32 board**.

**Important rule:**
1 reader = **1 ESP32 + 1 NFC module**

Examples:
- A **4-lane AFC system** requires **4 readers (4 ESP32s)**
- A **2-tool toolchanger** requires **2 readers (2 ESP32s)**
- If you mix reader types, each scanner still needs its own ESP32

---

## Reader Types

| Reader Type | ESP32 Board | NFC Module | Supported Tags | Notes |
|-------------|-------------|-----------|---------------|------|
| PN532 reader | ESP32-S3-Zero (recommended) | PN532 | Plain UID tags, OpenTag3D | Cheapest and simplest option |
| PN5180 reader | ESP32-WROOM-32 | PN5180 | Plain UID, OpenTag3D, OpenPrintTag | Required for ISO15693 / OpenPrintTag |

---

## Tag Compatibility

| Tag Type | PN532 | PN5180 |
|---------|------|-------|
| Plain UID NFC tags | ✔ | ✔ |
| OpenTag3D | ✔ | ✔ |
| OpenPrintTag (ISO 15693 / SLIX2) | ❌ | ✔ |

If you want to use **OpenPrintTag tags (ISO 15693 / SLIX2)**, you must use a **PN5180-based reader**.

That means:
- ESP32-WROOM-32
- PN5180 NFC module
- one build per scanner location

---

## Scanner Placement

Readers are normally placed at the **spool location**, not at the printer toolhead.
Each spool position should have **one scanner**.

## Single Toolhead

```
[ Spool ]
    │
[ NFC Scanner ]
    │
[ Printer ]
```

Hardware required:
- 1 × ESP32
- 1 × NFC reader

---

## Multi-Toolhead (2 Tools)

```
[ Spool 1 ]        [ Spool 2 ]
     │                  │
[ NFC Scanner ]   [ NFC Scanner ]
     │                  │
      └───────[ Printer ]───────┘
            (Toolchanger)
```

Hardware required:
- 2 × ESP32
- 2 × NFC readers

---

## 4-Lane AFC

```
[ Spool 1 ] [ Spool 2 ] [ Spool 3 ] [ Spool 4 ]
     │           │           │           │
 [ Scanner ]  [ Scanner ]  [ Scanner ]  [ Scanner ]
     │           │           │           │
      └──────────────[ AFC Unit ]──────────────┘
                          │
                       [ Printer ]
```

Hardware required:
- 4 × ESP32
- 4 × NFC readers

---

## Toolchanger + AFC

Most systems place scanners **at the AFC lanes**, not at the toolheads.

```
[ Spool 1 ] [ Spool 2 ] [ Spool 3 ] [ Spool 4 ]
     │           │           │           │
 [ Scanner ]  [ Scanner ]  [ Scanner ]  [ Scanner ]
     │           │           │           │
      └──────────────[ AFC Unit ]──────────────┘
                          │
                  [ Toolchanger Printer ]
                   (T0 / T1 / T2 / etc)
```

Hardware required:
- 4 × ESP32
- 4 × NFC readers

Even if the printer has multiple tools, scanners are usually sized by **AFC lanes / spool positions**.

---

## How Many Readers Do I Need?

The number of scanners depends on **spool locations**, not how many tags you own.

| Printer Setup | Readers Needed | ESP32 Count | Notes |
|---------------|---------------|-------------|------|
| Single toolhead | 1 | 1 | Scanner placed at the spool |
| Multi-toolhead (2 tools) | 2 | 2 | One scanner per spool/tool |
| Multi-toolhead (4 tools) | 4 | 4 | One scanner per spool/tool |
| AFC with 4 lanes | 4 | 4 | One scanner per lane |
| AFC with 8 lanes | 8 | 8 | One scanner per lane |
| Toolchanger + 4-lane AFC | 4 | 4 | Usually one scanner per AFC lane |

---

## Quick Rules

**Single toolhead**
→ usually **1 ESP32 + 1 reader**

**Multi-toolhead / toolchanger**
→ **1 ESP32 + 1 reader per spool**

**AFC**
→ **1 ESP32 + 1 reader per lane**

**Toolchanger + AFC**
→ usually size the readers by the **AFC lanes**, not the toolheads.

---

## Example Hardware Builds

## Single Toolhead (UID tags or OpenTag3D)
- 1 × ESP32-S3-Zero
- 1 × PN532

---

## Single Toolhead (OpenPrintTag)
- 1 × ESP32-WROOM-32
- 1 × PN5180

---

## 4-Lane AFC using OpenPrintTag
- 4 × ESP32-WROOM-32
- 4 × PN5180

---

## 2-Tool Toolchanger using UID tags or OpenTag3D
- 2 × ESP32-S3-Zero
- 2 × PN532

---

## 2-Tool Toolchanger using OpenPrintTag
- 2 × ESP32-WROOM-32
- 2 × PN5180

---

## Toolchanger + 4-Lane AFC using OpenPrintTag
- 4 × ESP32-WROOM-32
- 4 × PN5180

In most real setups, scanners are placed at **spool locations**, not at the toolhead itself.
For example, a toolchanger pulling filament from a **4-lane AFC** typically uses **4 scanners**, not 2.
