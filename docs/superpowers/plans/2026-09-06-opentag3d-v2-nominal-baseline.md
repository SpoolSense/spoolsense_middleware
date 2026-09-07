# OpenTag3D v2 Nominal-Weight Baseline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deduction baselines come from Spoolman (minus the scanner's pending deduction) instead of the tag, so re-scans of stale or nominal-weight tags never double-deduct — and tag write commands are never sent to non-OpenPrintTag tags.

**Architecture:** One pure chooser function (`choose_deduction_baseline`) picks the baseline from scan + Spoolman data. The direct scan path (mqtt_handler) and the staged path (activation → pending dict → afc_status / toolchanger_status) both route through it. Two new `ScanEvent` fields carry the scanner's v2 contract (`weight_source`, `pending_deduction_g`); one new `SpoolInfo` field (`spoolman_remaining_g`) exposes Spoolman's real remaining weight, which the tag-preferred merge currently hides. The write-back policy gains a tag-format gate.

**Tech Stack:** Python 3, unittest + unittest.mock (existing suite style), no new dependencies.

**Spec:** `docs/superpowers/specs/2026-09-06-opentag3d-v2-nominal-baseline-design.md` — read it first.

## Global Constraints

- All new/changed Python functions must have type hints on all parameters and the return type.
- `app_state.state_lock` guards all multi-thread access to `active_spool_tracking`, `pending_spool*`, `tag_write_timestamps` (existing lock discipline — the touched code already follows it; keep it).
- `activation.py` must not gain Moonraker HTTP calls (it gains only a pure function call).
- Tag write-back stays opt-in (`tag_writeback_enabled: false` default) — this plan only narrows when a plan is built.
- Never swallow exceptions on activation paths; log explicitly.
- Commits: conventional prefix style (`feat:`, `fix:`, `test:`, `chore:`), reference `(#119)`. **No AI attribution, no Co-Authored-By lines, ever.**
- Run commands from the repo root. Test command: `python3 -m pytest middleware/tests/ -v` (targeted: `python3 -m pytest middleware/tests/<file>.py -v`).
- Branch: `fix/119-opentag3d-v2-nominal-baseline` (already created off `dev`). Do NOT push without the user's explicit OK.
- The scanner does not emit the new MQTT fields yet (scanner #298 in review) — build against the contract; all tests are hardware-free.

**Contract being implemented (from #119 + verified scanner answers):**
- `weight_source` appears only on opentag3d payloads: `"nominal"` or `"measured"`; absent on other formats and legacy firmware; no other values.
- `pending_deduction_g` (opentag3d payloads, ships with scanner #298): scanner-side deduction not yet applied to Spoolman; absent means 0.
- Baseline rule: Spoolman remaining − pending (floored at 0) when Spoolman has a remaining weight; else tag weight for measured/legacy; else `None` for nominal (no deduction until Spoolman knows the spool).
- `cmd/deduct` keeps flowing for ALL opentag3d tags including nominal — the scanner routes those to Spoolman. Do not touch `_WRITABLE_FORMATS`.
- `cmd/update_remaining`/`cmd/write_tag` must only ever target `tag_format == "openprinttag"`.

---

### Task 1: ScanEvent fields + OpenTag3D parser

**Files:**
- Modify: `middleware/state/models.py` (ScanEvent dataclass, after `remaining_length_mm`)
- Modify: `middleware/opentag3d/parser.py`
- Test: `middleware/tests/test_opentag3d_parser.py` (new file)

**Interfaces:**
- Consumes: nothing new.
- Produces: `ScanEvent.weight_source: Optional[str]` (`"measured"` | `"nominal"` | `None`) and `ScanEvent.pending_deduction_g: Optional[float]` — defaults `None`, set only by `parse_opentag3d`. Tasks 3–6 read them.

- [ ] **Step 1: Write the failing tests**

Create `middleware/tests/test_opentag3d_parser.py`:

```python
"""Tests for opentag3d/parser.py — OpenTag3D v2 weight_source contract (#119)."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from opentag3d.parser import parse_opentag3d  # noqa: E402


def _payload(**extra) -> dict:
    base = {
        "uid": "53AB12CD34EF56",
        "opentag_version": 2,
        "manufacturer": "Prusament",
        "material_name": "PLA",
        "spool_weight_nominal": 1000,
        "spool_weight_measured": 1000,
    }
    base.update(extra)
    return base


class TestWeightSourceParsing(unittest.TestCase):

    def test_nominal_weight_source_parsed(self):
        scan = parse_opentag3d(_payload(weight_source="nominal"), "lane1")
        self.assertEqual(scan.weight_source, "nominal")
        self.assertEqual(scan.remaining_weight_g, 1000)

    def test_measured_weight_source_parsed(self):
        scan = parse_opentag3d(
            _payload(weight_source="measured", spool_weight_measured=812), "lane1")
        self.assertEqual(scan.weight_source, "measured")
        self.assertEqual(scan.remaining_weight_g, 812)

    def test_absent_weight_source_is_none(self):
        # Legacy firmware — field missing entirely
        scan = parse_opentag3d(_payload(), "lane1")
        self.assertIsNone(scan.weight_source)

    def test_pending_deduction_parsed(self):
        scan = parse_opentag3d(_payload(pending_deduction_g=12.5), "lane1")
        self.assertEqual(scan.pending_deduction_g, 12.5)

    def test_absent_pending_deduction_is_none(self):
        scan = parse_opentag3d(_payload(), "lane1")
        self.assertIsNone(scan.pending_deduction_g)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest middleware/tests/test_opentag3d_parser.py -v`
Expected: FAIL — `AttributeError`/`TypeError`: `ScanEvent` has no `weight_source`.

- [ ] **Step 3: Add the ScanEvent fields**

In `middleware/state/models.py`, inside `ScanEvent`, directly after the `remaining_length_mm` line:

```python
    # OpenTag3D v2 weight semantics (#119) — set only by the opentag3d parser.
    # weight_source "nominal" means remaining_weight_g is the spool's nominal
    # size and will never change on the tag; None = legacy firmware / other
    # formats (measured semantics).
    weight_source: Optional[str] = None
    pending_deduction_g: Optional[float] = None  # scanner deduction not yet applied to Spoolman
```

- [ ] **Step 4: Parse the fields**

In `middleware/opentag3d/parser.py`, add to the `ScanEvent(...)` constructor call, after the `remaining_weight_g=` line:

```python
        weight_source=payload.get("weight_source"),
        pending_deduction_g=payload.get("pending_deduction_g"),
```

Also extend the docstring's field-mapping list with:

```
        weight_source        → weight_source        ("measured" | "nominal", v2 contract #119)
        pending_deduction_g  → pending_deduction_g  (scanner-side unapplied deduction)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python3 -m pytest middleware/tests/test_opentag3d_parser.py middleware/tests/test_dispatcher.py -v`
Expected: all PASS (dispatcher suite proves no regression in detection/parsing).

- [ ] **Step 6: Commit**

```bash
git add middleware/state/models.py middleware/opentag3d/parser.py middleware/tests/test_opentag3d_parser.py
git commit -m "feat: parse OpenTag3D v2 weight_source and pending_deduction_g (#119)"
```

---

### Task 2: Expose Spoolman's real remaining weight on SpoolInfo

**Files:**
- Modify: `middleware/state/models.py` (SpoolInfo dataclass, after `consumed_weight_g`)
- Modify: `middleware/spoolman/client.py` (`sync_spool_from_scan`)
- Test: `middleware/tests/test_spoolman_client.py` (append)

**Interfaces:**
- Consumes: nothing new.
- Produces: `SpoolInfo.spoolman_remaining_g: Optional[float]` — Spoolman's own `remaining_weight`, set whenever a Spoolman match exists, regardless of `prefer_tag`. `None` when unmatched or Spoolman has no weight data. Tasks 3–7 read it.

- [ ] **Step 1: Write the failing tests**

Append to `middleware/tests/test_spoolman_client.py`:

```python
class TestSpoolmanRemainingExposed(unittest.TestCase):
    """#119 — the tag-preferred merge must still expose Spoolman's own
    remaining weight; deduction baselines need it."""

    def setUp(self):
        _reset_app_state()

    def _client_with_spool(self, spool: dict | None) -> SpoolmanClient:
        with patch.object(SpoolmanClient, "_fetch_all_spools"):
            client = SpoolmanClient(BASE_URL)
        client.find_by_nfc = MagicMock(return_value=spool)
        return client

    def test_prefer_tag_still_exposes_spoolman_remaining(self):
        client = self._client_with_spool(
            {"id": 5, "remaining_weight": 812.5, "filament": {}})
        scan = _make_scan_event(remaining_weight_g=1000.0)
        info = client.sync_spool_from_scan(scan, prefer_tag=True)
        self.assertEqual(info.spoolman_remaining_g, 812.5)
        # merge semantics unchanged — tag value still wins the display field
        self.assertEqual(info.remaining_weight_g, 1000.0)

    def test_spoolman_without_weight_data_gives_none(self):
        client = self._client_with_spool({"id": 5, "filament": {}})
        scan = _make_scan_event(remaining_weight_g=1000.0)
        info = client.sync_spool_from_scan(scan, prefer_tag=True)
        self.assertIsNone(info.spoolman_remaining_g)

    def test_no_spoolman_match_returns_none_info(self):
        client = self._client_with_spool(None)
        scan = _make_scan_event(remaining_weight_g=1000.0)
        self.assertIsNone(client.sync_spool_from_scan(scan, prefer_tag=True))
```

If the file's import block lacks any of `patch`, `MagicMock`, extend it (it already has both per its header).

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest middleware/tests/test_spoolman_client.py -v -k Remaining`
Expected: FAIL — `SpoolInfo` has no `spoolman_remaining_g`.

- [ ] **Step 3: Add the field and set it**

In `middleware/state/models.py`, inside `SpoolInfo`, after `consumed_weight_g`:

```python
    # Spoolman's own remaining_weight, exposed regardless of merge preference —
    # deduction baselines read this, never the merged remaining_weight_g (#119)
    spoolman_remaining_g: Optional[float] = None
```

In `middleware/spoolman/client.py` `sync_spool_from_scan`, directly after `tag_spool.spoolman_id = spoolman_id`:

```python
        tag_spool.spoolman_remaining_g = existing.get("remaining_weight")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest middleware/tests/test_spoolman_client.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add middleware/state/models.py middleware/spoolman/client.py middleware/tests/test_spoolman_client.py
git commit -m "feat: expose Spoolman remaining weight on SpoolInfo (#119)"
```

---

### Task 3: Baseline chooser + relaxed record_tracking

**Files:**
- Modify: `middleware/tracking_store.py`
- Test: `middleware/tests/test_tracking_store.py` (append)

**Interfaces:**
- Consumes: `ScanEvent.weight_source`, `ScanEvent.pending_deduction_g` (Task 1), `SpoolInfo.spoolman_remaining_g` (Task 2) — accessed via `getattr` with defaults so test fakes and stubbed modules work.
- Produces:
  - `choose_deduction_baseline(scan, spool_info) -> float | None` — the single baseline rule. Tasks 4 and 5 call it.
  - `record_tracking(...)` now accepts `remaining=None` (records the spool with no baseline) — requires only `target` and `uid`.

- [ ] **Step 1: Write the failing tests**

Append to `middleware/tests/test_tracking_store.py` (the file already imports `record_tracking`; extend that import line with `choose_deduction_baseline` and add `from dataclasses import dataclass` below the existing imports):

```python
@dataclass
class _Scan:
    """Only the fields choose_deduction_baseline reads."""
    uid: str = "53ab12cd34ef56"
    remaining_weight_g: float | None = 1000.0
    weight_source: str | None = None
    pending_deduction_g: float | None = None


@dataclass
class _Spool:
    spoolman_remaining_g: float | None = None


class TestChooseDeductionBaseline(unittest.TestCase):
    """#119 baseline rule — Spoolman-preferred, tag fallback, nominal → None."""

    def test_spoolman_remaining_wins_for_all_sources(self):
        for source in (None, "measured", "nominal"):
            got = choose_deduction_baseline(
                _Scan(weight_source=source), _Spool(spoolman_remaining_g=812.5))
            self.assertEqual(got, 812.5, f"weight_source={source}")

    def test_pending_deduction_subtracted(self):
        got = choose_deduction_baseline(
            _Scan(pending_deduction_g=12.5), _Spool(spoolman_remaining_g=800.0))
        self.assertEqual(got, 787.5)

    def test_pending_larger_than_remaining_floors_at_zero(self):
        got = choose_deduction_baseline(
            _Scan(pending_deduction_g=50.0), _Spool(spoolman_remaining_g=20.0))
        self.assertEqual(got, 0.0)

    def test_no_spoolman_measured_falls_back_to_tag(self):
        self.assertEqual(
            choose_deduction_baseline(_Scan(weight_source="measured"), None), 1000.0)

    def test_no_spoolman_legacy_falls_back_to_tag(self):
        self.assertEqual(choose_deduction_baseline(_Scan(), None), 1000.0)

    def test_no_spoolman_nominal_gives_none(self):
        self.assertIsNone(
            choose_deduction_baseline(_Scan(weight_source="nominal"), None))

    def test_spoolman_match_without_weight_data_falls_back(self):
        # Matched spool but Spoolman has no remaining_weight → same as no match
        self.assertEqual(
            choose_deduction_baseline(_Scan(), _Spool(spoolman_remaining_g=None)),
            1000.0)
        self.assertIsNone(
            choose_deduction_baseline(_Scan(weight_source="nominal"),
                                      _Spool(spoolman_remaining_g=None)))


class TestRecordTrackingNoneWeight(unittest.TestCase):
    """#119 — a record with no baseline still carries uid/format for
    usage-based deduction routing."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self.tmp.close()
        os.unlink(self.tmp.name)
        app_state.TRACKING_FILE = self.tmp.name
        app_state.state_lock = threading.Lock()
        app_state.active_spool_tracking = {}

    def tearDown(self):
        if os.path.exists(self.tmp.name):
            os.unlink(self.tmp.name)

    def test_records_with_none_weight(self):
        self.assertTrue(record_tracking("lane1", "AABB11", "f3d360", None,
                                        1.75, 1.24, "opentag3d"))
        rec = app_state.active_spool_tracking["lane1"]
        self.assertEqual(rec.uid, "aabb11")
        self.assertIsNone(rec.weight_g)
        self.assertEqual(rec.tag_format, "opentag3d")

    def test_still_requires_uid(self):
        self.assertFalse(record_tracking("lane1", "", "f3d360", 500.0))
        self.assertNotIn("lane1", app_state.active_spool_tracking)

    def test_none_weight_round_trips_persistence(self):
        record_tracking("lane1", "AABB11", "f3d360", None, 1.75, 1.24, "opentag3d")
        app_state.active_spool_tracking = {}
        load_tracking()
        self.assertIsNone(app_state.active_spool_tracking["lane1"].weight_g)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest middleware/tests/test_tracking_store.py -v`
Expected: FAIL — `ImportError: cannot import name 'choose_deduction_baseline'`.

- [ ] **Step 3: Implement**

In `middleware/tracking_store.py`:

(a) Add the chooser above `record_tracking` (no new imports needed beyond `TYPE_CHECKING` if you want type names; use string annotations to avoid importing `state.models` at runtime — tests stub it in some suites):

```python
def choose_deduction_baseline(scan: "object", spool_info: "object | None") -> float | None:
    """Pick the UPDATE_TAG deduction baseline for a scanned spool (#119).

    Spoolman's remaining weight is authoritative whenever available: in AFC
    setups the scanner applies off-scanner deductions Spoolman-direct and
    never rewrites the tag, so the tag goes stale — and OpenTag3D v2
    nominal-weight tags never change at all. The tag weight is only a
    fallback for measured/legacy tags Spoolman doesn't know yet. A nominal
    tag with no Spoolman match gets no baseline rather than a wrong one
    that would double-deduct on every re-scan.

    scan needs: remaining_weight_g, weight_source, pending_deduction_g, uid.
    spool_info needs: spoolman_remaining_g (or be None).
    """
    spoolman_remaining = getattr(spool_info, "spoolman_remaining_g", None) if spool_info else None
    if spoolman_remaining is not None:
        pending = getattr(scan, "pending_deduction_g", None) or 0.0
        return max(0.0, spoolman_remaining - pending)
    if getattr(scan, "weight_source", None) == "nominal":
        logger.info(
            "Baseline: uid=%s reports a nominal weight and has no Spoolman "
            "match — no deduction baseline until Spoolman knows the spool",
            getattr(scan, "uid", None))
        return None
    return getattr(scan, "remaining_weight_g", None)
```

(b) Relax `record_tracking`: change the guard line

```python
    if not target or not uid or remaining is None:
```

to

```python
    if not target or not uid:
```

and update the docstring paragraph "Requires a uid and a known weight …" to:

```
    Requires a uid. remaining may be None (#119): the record still carries
    uid/device/format so usage-based (toolchanger) deductions and mobile
    deduction routing keep working; the AFC weight-delta path skips
    baselines of None.
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest middleware/tests/test_tracking_store.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add middleware/tracking_store.py middleware/tests/test_tracking_store.py
git commit -m "feat: Spoolman-preferred deduction baseline chooser (#119)"
```

---

### Task 4: Wire the direct scan path (mqtt_handler)

**Files:**
- Modify: `middleware/mqtt_handler.py` (`_record_spool_tracking` low-spool guard, `_handle_rich_tag` call site)
- Test: `middleware/tests/test_mqtt_handler.py` (append)

**Interfaces:**
- Consumes: `choose_deduction_baseline` (Task 3).
- Produces: dedicated-scanner scans (`afc_lane`, `toolhead`) record the chosen baseline. `_record_spool_tracking` tolerates `remaining=None` without running the low-spool check.

- [ ] **Step 1: Write the failing test**

Append to `middleware/tests/test_mqtt_handler.py` (extend the existing `from mqtt_handler import (...)` block with `_record_spool_tracking`):

```python
class TestRecordSpoolTrackingNoneBaseline(unittest.TestCase):
    """#119 — a None baseline records the spool but must not run the
    low-spool check (None is 'unknown', not 'empty')."""

    def setUp(self):
        app_state.state_lock = threading.Lock()
        app_state.active_spool_tracking = {}

    def test_none_baseline_records_without_low_spool_check(self):
        with patch("mqtt_handler._check_low_spool") as mock_low:
            _record_spool_tracking("lane1", "AABB11", "f3d360", None,
                                   1.75, 1.24, tag_format="opentag3d")
        rec = app_state.active_spool_tracking.get("lane1")
        self.assertIsNotNone(rec)
        self.assertIsNone(rec.weight_g)
        mock_low.assert_not_called()

    def test_real_baseline_still_checks_low_spool(self):
        with patch("mqtt_handler._check_low_spool") as mock_low:
            _record_spool_tracking("lane1", "AABB11", "f3d360", 90.0,
                                   1.75, 1.24, tag_format="opentag3d")
        mock_low.assert_called_once_with("f3d360", 90.0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest middleware/tests/test_mqtt_handler.py -v -k NoneBaseline`
Expected: `test_none_baseline_records_without_low_spool_check` FAILS — today `record_tracking` returns False for None weight, so nothing is recorded. (The second test should already pass; it pins current behavior.)

- [ ] **Step 3: Implement**

In `middleware/mqtt_handler.py`:

(a) `_record_spool_tracking` — change the low-spool guard from `if device_id:` to:

```python
    if device_id and remaining is not None:
```

(b) `_handle_rich_tag` — replace the recording block

```python
        tag_format = payload.get("tag_format", "unknown")
        _record_spool_tracking(
            target, scan.uid.lower() if scan.uid else None, device_id or "",
            scan.remaining_weight_g, scan.diameter_mm, scan.density,
            tag_format=tag_format,
        )
```

with

```python
        tag_format = payload.get("tag_format", "unknown")
        from tracking_store import choose_deduction_baseline
        _record_spool_tracking(
            target, scan.uid.lower() if scan.uid else None, device_id or "",
            choose_deduction_baseline(scan, spool_info),
            scan.diameter_mm, scan.density,
            tag_format=tag_format,
        )
```

(lazy import matches the file's existing `from tracking_store import record_tracking` pattern). Update the comment above the block from "Record initial weight …" to:

```python
        # Record the deduction baseline for UPDATE_TAG — Spoolman-preferred,
        # tag fallback, None for unmatched nominal tags (#119)
```

- [ ] **Step 4: Run the affected suites**

Run: `python3 -m pytest middleware/tests/test_mqtt_handler.py middleware/tests/test_tracking_store.py middleware/tests/test_filament_usage.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add middleware/mqtt_handler.py middleware/tests/test_mqtt_handler.py
git commit -m "feat: use chosen baseline for direct-scan tracking (#119)"
```

---

### Task 5: Carry the baseline through the staged path

**Files:**
- Modify: `middleware/activation.py` (`_cache_pending_spool`, `_route_staged`, `_activate_from_scan`)
- Modify: `middleware/afc_status.py` (staged record block, ~lines 118–129)
- Modify: `middleware/toolchanger_status.py` (`_assign_spool_to_tool` record block, ~lines 188–196)
- Test: `middleware/tests/test_activation.py`, `middleware/tests/test_afc_status.py`, `middleware/tests/test_toolchanger_status.py`

**Interfaces:**
- Consumes: `choose_deduction_baseline` (Task 3), relaxed `record_tracking` (Task 3).
- Produces: pending dicts gain key `"baseline_g": float | None` (the chosen baseline). `remaining_g` keeps its display/mobile-echo meaning and is NOT repurposed. Staged consumers record from `baseline_g` and record even when it is None (uid gate only).

**Why:** `afc_stage`/`toolhead_stage` scans don't record at scan time — `activation._cache_pending_spool` stashes the data and `afc_status`/`toolchanger_status` record on load/assign. Today they record `pending["remaining_g"]`, which is the tag-preferred display value — the same stale/nominal number the whole fix exists to avoid. Mobile staged scans flow through the same `_activate_from_scan`, so they inherit this for free.

- [ ] **Step 1: Write the failing tests**

(a) Append to `middleware/tests/test_activation.py` (add `_cache_pending_spool` to the file's existing activation imports; add `import threading` if missing):

```python
class TestPendingBaseline(unittest.TestCase):
    """#119 — the staged pending dict carries the chosen deduction baseline
    separately from the display weight."""

    def setUp(self):
        app_state.state_lock = threading.Lock()
        app_state.pending_spool_afc = None
        app_state.pending_spool_toolhead = None
        app_state.pending_spool_toolhead_gen = 0

    def test_baseline_stored_alongside_display_weight(self):
        _cache_pending_spool("afc", "FF0000", "PLA", 1000.0, None,
                             uid="AABB11", baseline_g=700.0)
        pending = app_state.pending_spool_afc
        self.assertEqual(pending["remaining_g"], 1000.0)   # display untouched
        self.assertEqual(pending["baseline_g"], 700.0)

    def test_baseline_may_be_none(self):
        _cache_pending_spool("afc", "FF0000", "PLA", 1000.0, None,
                             uid="AABB11", baseline_g=None)
        self.assertIsNone(app_state.pending_spool_afc["baseline_g"])
```

(b) In `middleware/tests/test_afc_status.py`, class `TestLaneLoadRecordsTracking`:

- In `test_staged_scan_with_uid_records_baseline`, add `"baseline_g": 250.0,` to the pending dict (keep `"remaining_g": 250.0`). Assertions unchanged.
- Replace `test_staged_scan_without_weight_records_nothing` with:

```python
    def test_staged_scan_without_baseline_records_uid_only(self):
        # #119: no baseline (e.g. nominal tag, no Spoolman match) still
        # records the uid so deduction routing works; weight stays None
        self._load_lane_with_pending({
            "color_hex": "FF0000", "material": "PLA", "remaining_g": 1000.0,
            "baseline_g": None, "spoolman_id": 5, "uid": "AABBCC",
        })
        rec = app_state.active_spool_tracking.get("lane1")
        self.assertIsNotNone(rec)
        self.assertEqual(rec.uid, "aabbcc")
        self.assertIsNone(rec.weight_g)
```

(`test_staged_scan_without_uid_records_nothing` and `test_unrecordable_pending_clears_previous_baseline` keep passing unchanged — both lack a uid.)

(c) In `middleware/tests/test_toolchanger_status.py`, class `TestAssignRecordsTracking`:

- In `test_success_records_baseline_with_lowercased_uid`, add `"baseline_g": 300.0,` to the pending dict.
- Replace `test_no_weight_records_nothing` with:

```python
    @patch("requests.post")
    def test_no_baseline_records_uid_with_no_weight(self, mock_post):
        # #119: usage-based toolchanger deductions need the uid even when
        # no baseline weight is known
        mock_post.return_value = MagicMock(raise_for_status=lambda: None)
        pending = {"spoolman_id": 10, "color_hex": "FF0000", "material": "PLA",
                   "remaining_g": None, "baseline_g": None, "uid": "AABB11"}

        _assign_spool_to_tool("T0", pending)

        rec = app_state.active_spool_tracking.get("T0")
        self.assertIsNotNone(rec)
        self.assertEqual(rec.uid, "aabb11")
        self.assertIsNone(rec.weight_g)
```

- Replace `test_unrecordable_assignment_clears_previous_baseline` with:

```python
    @patch("requests.post")
    def test_new_spool_without_baseline_replaces_previous_record(self, mock_post):
        # Spool B assigned with no baseline while spool A's record is on the
        # tool — B's record must replace A's so A never eats B's deductions
        mock_post.return_value = MagicMock(raise_for_status=lambda: None)
        app_state.active_spool_tracking["T0"] = app_state.ActiveSpool(
            uid="spool_a", weight_g=400.0)
        pending = {"spoolman_id": 10, "color_hex": "FF0000", "material": "PLA",
                   "remaining_g": None, "baseline_g": None, "uid": "SPOOLB"}

        result = _assign_spool_to_tool("T0", pending)

        self.assertTrue(result)
        rec = app_state.active_spool_tracking.get("T0")
        self.assertEqual(rec.uid, "spoolb")
        self.assertIsNone(rec.weight_g)
```

- [ ] **Step 2: Run tests to verify the new ones fail**

Run: `python3 -m pytest middleware/tests/test_activation.py middleware/tests/test_afc_status.py middleware/tests/test_toolchanger_status.py -v`
Expected: the new/replaced tests FAIL (`baseline_g` unexpected kwarg; records missing); pre-existing tests PASS.

- [ ] **Step 3: Implement**

(a) `middleware/activation.py` `_cache_pending_spool`: add parameter `baseline_g: float | None = None` (after `tag_format`), add `"baseline_g": baseline_g,` to the `pending` dict, and add to the docstring:

```
    baseline_g is the UPDATE_TAG deduction baseline chosen at scan time
    (#119) — distinct from remaining_g, which is display/mobile-echo data.
```

(b) `middleware/activation.py` `_route_staged`: add parameter `baseline_g: float | None = None` (after `device_id`), and pass `baseline_g=baseline_g` in the `_cache_pending_spool(...)` call.

(c) `middleware/activation.py` `_activate_from_scan`: after the `color_hex, remaining, filament_label = _resolve_scan_data(scan, spool_info)` line add:

```python
    from tracking_store import choose_deduction_baseline
    baseline_g = choose_deduction_baseline(scan, spool_info)
```

and extend the staged routing call to `_route_staged(action_enum, spoolman_activated, color_hex, filament_label, remaining, spoolman_id, event, scan, device_id, baseline_g=baseline_g)`.

(d) `middleware/afc_status.py` staged record block — replace:

```python
        uid = pending.get("uid")
        if uid and pending.get("remaining_g") is not None:
            from tracking_store import record_tracking
            record_tracking(lane_name, uid, pending.get("device_id", ""),
                            pending.get("remaining_g"),
                            pending.get("diameter_mm"), pending.get("density"),
                            pending.get("tag_format"))
        else:
            from tracking_store import clear_tracking
            clear_tracking(lane_name)
```

with:

```python
        uid = pending.get("uid")
        if uid:
            from tracking_store import record_tracking
            record_tracking(lane_name, uid, pending.get("device_id", ""),
                            pending.get("baseline_g"),
                            pending.get("diameter_mm"), pending.get("density"),
                            pending.get("tag_format"))
        else:
            from tracking_store import clear_tracking
            clear_tracking(lane_name)
```

and change the comment above it to say the baseline comes from `baseline_g` (Spoolman-preferred, #119) and that a uid-less pending still clears the old record.

(e) `middleware/toolchanger_status.py` `_assign_spool_to_tool` record block — replace:

```python
    uid = pending.get("uid")
    if uid and remaining_g is not None:
        from tracking_store import record_tracking
        record_tracking(macro, uid, pending.get("device_id", ""), remaining_g,
                        pending.get("diameter_mm"), pending.get("density"),
                        pending.get("tag_format"))
    else:
        from tracking_store import clear_tracking
        clear_tracking(macro)
```

with:

```python
    uid = pending.get("uid")
    if uid:
        from tracking_store import record_tracking
        record_tracking(macro, uid, pending.get("device_id", ""),
                        pending.get("baseline_g"),
                        pending.get("diameter_mm"), pending.get("density"),
                        pending.get("tag_format"))
    else:
        from tracking_store import clear_tracking
        clear_tracking(macro)
```

(`remaining_g` stays in use above for display/lane-data — do not remove it.)

- [ ] **Step 4: Verify the wiring end to end**

Run: `python3 -m pytest middleware/tests/test_activation.py middleware/tests/test_afc_status.py middleware/tests/test_toolchanger_status.py -v`
Expected: all PASS.

Run: `grep -n "baseline_g" middleware/activation.py`
Expected: 4+ hits — the `_cache_pending_spool` param, the dict entry, the `_route_staged` param + pass-through, and the `_activate_from_scan` choose/pass lines. If the `_route_staged` call site lacks `baseline_g=`, the wiring is broken even if tests pass — fix it.

- [ ] **Step 5: Commit**

```bash
git add middleware/activation.py middleware/afc_status.py middleware/toolchanger_status.py middleware/tests/test_activation.py middleware/tests/test_afc_status.py middleware/tests/test_toolchanger_status.py
git commit -m "feat: carry deduction baseline through staged assignment (#119)"
```

---

### Task 6: AFC re-scan double-deduction regression tests

**Files:**
- Test: `middleware/tests/test_filament_usage.py` (append)

**Interfaces:**
- Consumes: `choose_deduction_baseline`, `record_tracking` (Task 3), existing `_handle_afc`, `_track`, `_reset_app_state`.
- Produces: the #119 acceptance regression suite. No production code changes — if these fail, earlier tasks are wrong; fix THERE, not here.

- [ ] **Step 1: Write the tests**

Append to `middleware/tests/test_filament_usage.py` (add `from dataclasses import dataclass` and `from tracking_store import choose_deduction_baseline, record_tracking` to the imports):

```python
@dataclass
class _FakeScan:
    """Only the fields choose_deduction_baseline reads."""
    uid: str = "53ab12cd34ef56"
    remaining_weight_g: float | None = 1000.0
    weight_source: str | None = None
    pending_deduction_g: float | None = None


@dataclass
class _FakeSpoolInfo:
    spoolman_remaining_g: float | None = None


class TestRescanDoubleDeductRegression(unittest.TestCase):
    """#119 acceptance — a re-scan must never resurrect a stale or nominal
    tag weight as the AFC baseline and re-deduct prior usage."""

    def setUp(self):
        _reset_app_state()

    def _scan(self, scan, spool_info, lane="lane1", device="f3d360"):
        record_tracking(lane, scan.uid, device,
                        choose_deduction_baseline(scan, spool_info),
                        1.75, 1.24, "opentag3d")

    @patch("filament_usage._publish_deduction")
    @patch("filament_usage._fetch_afc_lane_weights")
    def test_nominal_tag_rescan_does_not_double_deduct(self, mock_fetch, mock_pub):
        scan = _FakeScan(weight_source="nominal")
        # First scan of a fresh spool — Spoolman says 1000 g
        self._scan(scan, _FakeSpoolInfo(spoolman_remaining_g=1000.0))

        # Print 1 uses 200 g → one deduction of 200
        mock_fetch.return_value = {"lane1": 800.0}
        _handle_afc()
        self.assertEqual(mock_pub.call_count, 1)
        self.assertAlmostEqual(mock_pub.call_args[0][2], 200.0)

        # Re-scan: tag still says 1000, but Spoolman (updated by the
        # scanner) now says 800 — baseline must follow Spoolman
        self._scan(scan, _FakeSpoolInfo(spoolman_remaining_g=800.0))

        # UPDATE_TAG fires again with no new usage → no second deduction
        _handle_afc()
        self.assertEqual(mock_pub.call_count, 1)

    @patch("filament_usage._publish_deduction")
    @patch("filament_usage._fetch_afc_lane_weights")
    def test_stale_v1_tag_rescan_does_not_double_deduct(self, mock_fetch, mock_pub):
        # Same failure mode, measured (v1) tag gone stale in the lane
        scan = _FakeScan(weight_source=None, remaining_weight_g=1000.0)
        self._scan(scan, _FakeSpoolInfo(spoolman_remaining_g=800.0))

        mock_fetch.return_value = {"lane1": 800.0}
        _handle_afc()
        mock_pub.assert_not_called()

    @patch("filament_usage._publish_deduction")
    @patch("filament_usage._fetch_afc_lane_weights")
    def test_nominal_without_spoolman_sends_no_deduction(self, mock_fetch, mock_pub):
        self._scan(_FakeScan(weight_source="nominal"), None)

        mock_fetch.return_value = {"lane1": 800.0}
        _handle_afc()
        mock_pub.assert_not_called()

    @patch("filament_usage._publish_deduction")
    @patch("filament_usage._fetch_afc_lane_weights")
    def test_legacy_tag_without_spoolman_keeps_today_behavior(self, mock_fetch, mock_pub):
        self._scan(_FakeScan(), None)  # baseline = tag 1000

        mock_fetch.return_value = {"lane1": 800.0}
        _handle_afc()
        self.assertEqual(mock_pub.call_count, 1)
        self.assertAlmostEqual(mock_pub.call_args[0][2], 200.0)

    @patch("filament_usage._publish_deduction")
    @patch("filament_usage._fetch_afc_lane_weights")
    def test_pending_deduction_shrinks_baseline(self, mock_fetch, mock_pub):
        # Spoolman was unreachable at apply time: it still says 1000 while
        # the scanner holds 200 pending → baseline must be 800
        scan = _FakeScan(weight_source="nominal", pending_deduction_g=200.0)
        self._scan(scan, _FakeSpoolInfo(spoolman_remaining_g=1000.0))

        mock_fetch.return_value = {"lane1": 800.0}
        _handle_afc()
        mock_pub.assert_not_called()
```

- [ ] **Step 2: Run the tests**

Run: `python3 -m pytest middleware/tests/test_filament_usage.py -v`
Expected: all PASS immediately (Tasks 3–5 built the behavior). If any fail, the defect is in `tracking_store.py` or the wiring — fix there and re-run.

- [ ] **Step 3: Commit**

```bash
git add middleware/tests/test_filament_usage.py
git commit -m "test: AFC re-scan double-deduction regression coverage (#119)"
```

---

### Task 7: Write-back format gate + real Spoolman comparison

**Files:**
- Modify: `middleware/tag_sync/policy.py` (`build_write_plan`)
- Modify: `middleware/mqtt_handler.py` (`_handle_tag_writeback` signature + call site)
- Test: `middleware/tests/test_write_cooldown.py` (update + append)

**Interfaces:**
- Consumes: `SpoolInfo.spoolman_remaining_g` (Task 2).
- Produces: `build_write_plan(scan, spool_info, device_id, tag_format: str | None = None)` — returns `None` for any `tag_format != "openprinttag"`. `_handle_tag_writeback(scan, spool_info, device_id, client, tag_format: str | None = None)`.

**Why:** the scanner's `cmd/update_remaining` is OpenPrintTag-only, and `cmd/write_tag` would rewrite a foreign tag as OpenPrintTag — the scanner team told us to gate middleware-side. Bonus fix: the stale-check currently reads `spool_info.remaining_weight_g`, which under `prefer_tag=True` is the tag value itself — tag compared to tag can never differ, so write-back never fires from the scan path. Pointing it at `spoolman_remaining_g` makes the comparison mean what it always claimed to.

- [ ] **Step 1: Update the fakes and existing calls, add failing tests**

In `middleware/tests/test_write_cooldown.py`:

(a) Change `FakeSpoolInfo` to:

```python
@dataclass
class FakeSpoolInfo:
    spoolman_remaining_g: float | None = 500.0
```

(b) Add `tag_format="openprinttag"` to all eight existing `build_write_plan(scan, spool, device_id=...)` calls.

(c) Append:

```python
class TestWritePlanFormatGate(unittest.TestCase):
    """#119 — write commands are OpenPrintTag-only; write_tag would rewrite
    a foreign tag as OpenPrintTag."""

    def setUp(self):
        app_state.state_lock = threading.Lock()
        app_state.tag_write_timestamps = {}
        app_state.WRITE_COOLDOWN_SECONDS = 10

    def test_opentag3d_never_gets_write_plan(self):
        scan = FakeScanEvent(remaining_weight_g=1000.0)
        spool = FakeSpoolInfo(spoolman_remaining_g=500.0)  # stale — would write
        self.assertIsNone(build_write_plan(scan, spool, device_id="abc123",
                                           tag_format="opentag3d"))

    def test_unknown_format_never_gets_write_plan(self):
        scan = FakeScanEvent(remaining_weight_g=1000.0)
        spool = FakeSpoolInfo(spoolman_remaining_g=500.0)
        self.assertIsNone(build_write_plan(scan, spool, device_id="abc123",
                                           tag_format=None))

    def test_gate_does_not_claim_cooldown_slot(self):
        scan = FakeScanEvent(remaining_weight_g=1000.0)
        spool = FakeSpoolInfo(spoolman_remaining_g=500.0)
        build_write_plan(scan, spool, device_id="abc123", tag_format="opentag3d")
        self.assertNotIn(scan.uid, app_state.tag_write_timestamps)

    def test_openprinttag_uses_spoolman_remaining_field(self):
        scan = FakeScanEvent(remaining_weight_g=1000.0)
        spool = FakeSpoolInfo(spoolman_remaining_g=750.0)
        plan = build_write_plan(scan, spool, device_id="abc123",
                                tag_format="openprinttag")
        self.assertIsNotNone(plan)
        self.assertEqual(plan.payload, {"remaining_g": 750.0})
```

If the setUp fields above duplicate an existing helper in the file, reuse the helper instead — match the file's conventions.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest middleware/tests/test_write_cooldown.py -v`
Expected: new tests FAIL (`unexpected keyword argument 'tag_format'`).

- [ ] **Step 3: Implement**

In `middleware/tag_sync/policy.py` `build_write_plan`:

(a) Signature becomes:

```python
def build_write_plan(
    scan: ScanEvent,
    spool_info: SpoolInfo | None,
    device_id: str | None,
    tag_format: str | None = None,
) -> TagWritePlan | None:
```

(b) After the `if not scan.uid:` block, before the cooldown check, add:

```python
    if tag_format != "openprinttag":
        # update_remaining is OpenPrintTag-only scanner-side, and write_tag
        # would rewrite a foreign tag as OpenPrintTag — never build write
        # plans for other formats (#119)
        return None
```

(c) Change the comparison source line from

```python
    spoolman_remaining = spool_info.remaining_weight_g if spool_info else None
```

to

```python
    # spoolman_remaining_g is Spoolman's own number — the merged
    # remaining_weight_g is tag-preferred and would compare the tag to
    # itself (#119)
    spoolman_remaining = spool_info.spoolman_remaining_g if spool_info else None
```

(d) Document the new parameter in the docstring Args:

```
        tag_format: scanner-reported tag format; only "openprinttag"
                    produces write plans (#119)
```

In `middleware/mqtt_handler.py`:

(e) `_handle_tag_writeback` signature gains `tag_format: str | None = None` (after `client`), and its `build_write_plan` call becomes `build_write_plan(scan, spool_info, device_id=device_id, tag_format=tag_format)`.

(f) The `_handle_rich_tag` call site becomes:

```python
        _handle_tag_writeback(scan, spool_info, device_id, client, tag_format=tag_format)
```

(`tag_format` is already in scope from Task 4's block.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest middleware/tests/test_write_cooldown.py middleware/tests/test_mqtt_handler.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add middleware/tag_sync/policy.py middleware/mqtt_handler.py middleware/tests/test_write_cooldown.py
git commit -m "fix: gate tag write-back to OpenPrintTag and compare real Spoolman remaining (#119)"
```

---

### Task 8: Version bump + CHANGELOG

**Files:**
- Modify: `middleware/spoolsense.py` (line 4)
- Modify: `CHANGELOG.md` (new section at top, below the header block)

- [ ] **Step 1: Bump the version**

`middleware/spoolsense.py` line 4: `__version__ = "1.9.0"`

- [ ] **Step 2: Add the CHANGELOG entry**

Insert directly above the `## [1.8.6]` section:

```markdown
## [1.9.0] - UNRELEASED

### Changed

- **Deduction baselines now come from Spoolman, not the tag** (#119). In AFC
  setups the scanner applies deductions Spoolman-direct and never rewrites
  the tag, so tag weights go stale — re-scanning a spool reset the baseline
  to the stale value and double-deducted prior usage in Spoolman. The
  baseline is now Spoolman's remaining weight (minus any scanner-side
  pending deduction) whenever the spool is matched; the tag weight is only
  a fallback for measured/legacy tags Spoolman doesn't know yet. This
  applies to all tag formats, v1 included.

### Added

- **OpenTag3D v2 support** (#119, scanner #298): the middleware honors the
  new `weight_source` MQTT field. `"nominal"` tags (v2 tags without a
  measured weight) report the spool's nominal size forever — they get no
  tag-weight baseline at all; deductions still flow and land in Spoolman
  exactly once. `pending_deduction_g` (a not-yet-applied scanner deduction)
  is subtracted from the baseline when present. Older firmware without the
  fields behaves as before.

### Fixed

- Tag write-back commands are only built for OpenPrintTag tags —
  `cmd/write_tag` on the scanner is not format-aware and would rewrite an
  OpenTag3D tag as OpenPrintTag (#119).
- The write-back staleness check compared the tag-preferred merged weight
  to the tag itself and could never fire; it now compares Spoolman's own
  remaining weight.
```

- [ ] **Step 3: Sanity check and commit**

Run: `python3 middleware/spoolsense.py --check-config` only if a config exists locally; otherwise run `python3 -c "import sys; sys.path.insert(0, 'middleware'); import spoolsense; print(spoolsense.__version__)"`
Expected: `1.9.0`

```bash
git add middleware/spoolsense.py CHANGELOG.md
git commit -m "chore: bump version to 1.9.0 and update CHANGELOG"
```

---

### Task 9: Full verification + paperwork

**Files:**
- No production changes. GitHub + `.mex` upkeep.

- [ ] **Step 1: Full test suite**

Run: `python3 -m pytest middleware/tests/ -v`
Expected: ALL tests pass, zero failures. Fix anything red before proceeding.

- [ ] **Step 2: Lint**

Run: `python3 -m flake8 middleware/ && python3 -m pylint middleware/ --disable=all --enable=E`
Expected: no new errors introduced by this branch (compare against `git stash`-free dev if noisy).

- [ ] **Step 3: GitHub paperwork — SHOW THE USER, get an OK, then post**

Draft comment for #119 (post with `gh issue comment 119 --body-file <tmpfile>` after user approval):

```markdown
Middleware implementation landed on `fix/119-opentag3d-v2-nominal-baseline` (targets 1.9.0). Two scope corrections from the scanner-side verification, now reflected in the code and CHANGELOG:

1. There is no middleware Spoolman-direct deduction path — "treat as non-writable" would have dropped usage entirely. Instead `cmd/deduct` keeps flowing for all opentag3d tags (the scanner routes nominal-tag deductions to Spoolman), and only the **baseline** changed.
2. The re-scan double-deduct predates v2: off-scanner deducts have always gone Spoolman-direct, so v1 measured tags go stale in AFC lanes too. Baselines now come from Spoolman's remaining weight (minus `pending_deduction_g` when the scanner reports one) for **all** formats, with the tag as fallback for measured/legacy tags unknown to Spoolman; unmatched nominal tags get no baseline. The original "AFC + v1 unchanged" acceptance line is superseded.

Also gated `update_remaining`/`write_tag` to `tag_format == "openprinttag"` per the scanner findings (`write_tag` would rewrite an OpenTag3D tag as OpenPrintTag).

Confirming for scanner #298: **yes — please ship `pending_deduction_g` in `tag/state`** alongside `weight_source`.

Spec: `docs/superpowers/specs/2026-09-06-opentag3d-v2-nominal-baseline-design.md`. E2E validation on AFC hardware after #298 ships.
```

Draft for the mobile follow-up issue (`gh issue create --title ... --body-file <tmpfile>` after user approval):

- Title: `Mobile scan paths need the Spoolman-preferred deduction baseline (#119 follow-up)`
- Body:

```markdown
#119 moved scanner-path deduction baselines to Spoolman's remaining weight (see `docs/superpowers/specs/2026-09-06-opentag3d-v2-nominal-baseline-design.md`). The mobile REST paths still record tag-derived baselines:

- `middleware/rest_api.py` calls `_record_spool_tracking(...)` with `scan.remaining_weight_g` at its direct record sites.
- Mobile **staged** scans already inherit the fix (they flow through `activation._activate_from_scan`, which computes `baseline_g` via `tracking_store.choose_deduction_baseline`).

Work:
1. Route the direct mobile record sites through `choose_deduction_baseline(scan, spool_info)`.
2. The mobile app is adding OpenTag3D v2 parsing (pure Swift, no shared code) and will send the same `weight_source` semantics in its scan POST — accept and forward that field into the mobile-built `ScanEvent`.
3. Same acceptance as #119: re-scan via phone must never re-deduct.
```

- [ ] **Step 4: .mex upkeep (After Every Task rule)**

- Update `.mex/ROUTER.md`: move #119 into "In flight" (branch, commits, "needs E2E after scanner #298 ships"), note the write-back gate and the all-formats baseline change in Working/state as appropriate. Bump `last_updated`.
- `mex log --type decision "119: deduction baselines are Spoolman-preferred for all formats; middleware keeps sending cmd/deduct for nominal tags; write-back gated to openprinttag"`
- Check `patterns/INDEX.md`; if no pattern covers "cross-repo MQTT contract change (scanner <-> middleware)", create one per `patterns/README.md` capturing: verify contract against scanner code/branch, build against contract with hardware-free tests, additive MQTT fields with absent-field legacy semantics.
- Run `mex sync` and adjudicate any AMBIGUOUS grounding.

- [ ] **Step 5: Hand back to the user**

Report: test totals, lint status, commits on the branch (`git log dev..HEAD --oneline`). Remind the user:
- The branch is NOT pushed (their call).
- E2E on AFC hardware waits for scanner #298 to merge and ship.
- The #119 comment and follow-up issue drafts need their OK before posting.
