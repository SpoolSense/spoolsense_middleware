---
name: stack
description: Technology stack, library choices, and the reasoning behind them. Load when working with specific technologies or making decisions about libraries and tools.
triggers:
  - "library"
  - "package"
  - "dependency"
  - "which tool"
  - "technology"
edges:
  - target: context/decisions.md
    condition: when the reasoning behind a tech choice is needed
  - target: context/conventions.md
    condition: when understanding how to use a technology in this codebase
last_updated: 2026-04-05
---

# Stack

## Core Technologies

- **Python 3.10+** — primary language; uses `from __future__ import annotations` for deferred annotation evaluation in all modules
- **YAML** — configuration format (`~/SpoolSense/config.yaml`); loaded with `pyyaml.safe_load`
- **systemd** — production runtime; `middleware/spoolsense.service` template provided
- **MQTT** — primary inter-process bus between NFC scanners and middleware

## Key Libraries

- **`paho-mqtt>=1.6,<2`** — MQTT client; `mqtt.Client()` with `on_connect` / `on_message` callbacks and `loop_forever()`; pinned below v2 due to API changes
- **`pyyaml>=6.0`** — config file parsing; always use `yaml.safe_load`, never `yaml.load`
- **`requests>=2.28`** — all Moonraker and Spoolman HTTP calls; `.raise_for_status()` is called on every response; 5-second timeout on all calls
- **`watchdog>=3.0`** — file system watcher for Klipper `save_variables.cfg` (toolhead/single modes only); `Observer` + `FileSystemEventHandler`
- **`websocket-client>=1.6`** — Moonraker websocket connection (`moonraker_ws.py`); optional — if not installed, middleware falls back to HTTP polling gracefully; detected via try/import in `moonraker_ws.py`

## What We Deliberately Do NOT Use

- No async framework (asyncio, trio, etc.) — the entire application is synchronous threading; background tasks use `threading.Thread` + `threading.Event` for stop signaling
- No ORM or database client — there is no local database; Spoolman is the source of truth, accessed via REST API
- No dataclass validation framework (pydantic, attrs) — plain `@dataclass` from stdlib for `ScanEvent`, `SpoolInfo`, `SpoolEvent`; field validation is done manually in parsers and publishers
- No web framework — SpoolSense is headless; no HTTP server

## Version Constraints

- `paho-mqtt` is pinned `<2` because paho-mqtt 2.x introduced breaking API changes (callback signatures changed). Do not bump the upper bound without testing and migration.
- Python 3.10+ is required for `match/case` is NOT used (codebase uses if/elif chains), but `X | Y` union type syntax in annotations requires Python 3.10+ when `from __future__ import annotations` is not present. All modules include `from __future__ import annotations` so Python 3.9 may also work — but this is not tested or guaranteed.
