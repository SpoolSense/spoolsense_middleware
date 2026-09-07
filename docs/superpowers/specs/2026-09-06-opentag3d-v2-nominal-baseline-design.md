# OpenTag3D v2 nominal-weight deduction baselines — design

- **Date:** 2026-09-06
- **Issue:** #119 (middleware half). Scanner half: spoolsense_scanner#298 (plan #297).
- **Branch:** `fix/119-opentag3d-v2-nominal-baseline` (off `dev`)
- **Target version:** 1.9.0 (behavior change for existing v1 AFC users, not just a v2 fix)

## Problem

OpenTag3D v2 defines the on-tag weight as the spool's **nominal** size for tags
that carry no measured weight. The scanner never rewrites those tags; usage
goes to Spoolman directly. The middleware AFC path baselines deductions from
the tag's reported weight (`_record_spool_tracking` → `rec.weight_g -
current_weight`), so every re-scan resets the baseline to nominal and the next
`UPDATE_TAG` re-deducts usage that was already applied. Spoolman double-counts
on every re-scan/print cycle.

**The exposure predates v2.** Verified against scanner code (branch
`feature/opentag3d-v2-297`): when the tag is *not on the scanner* at deduct
time — always true in AFC lanes — the scanner has *always* applied deducts
Spoolman-direct and never rewritten the tag. v1 measured tags in AFC lanes go
stale the same way; a re-scan double-deducts everything since the tag was last
written. The fix therefore applies to **all tag formats**, not only
`weight_source == "nominal"`.

## Scanner contract (verified, recorded in #119 comments)

- `weight_source` appears **only on opentag3d payloads**: `"nominal"` (v2, no
  measured weight) or `"measured"` (v2 with measured weight, and all v1).
  Other formats and legacy firmware omit the field. No other values.
- The tag weight key the middleware parses (`spool_weight_measured`) is always
  populated; for nominal tags it holds the raw nominal value. The scanner
  never nets pending deductions out of the reported number.
- Deduct commands are stored in NVS, then applied to Spoolman synchronously —
  immediately when possible, otherwise on the next scan of that UID **before**
  `tag/state` is published. A Spoolman read triggered by a scan therefore sees
  the post-deduction value. Residual: if Spoolman was down at apply time the
  pending survives and retries later (can land hours later).
- To close that residual, the scanner will export `pending_deduction_g` in
  `tag/state` (ships with #298). Baseline math subtracts it; absent means 0.
- `cmd/response {"success":true}` only validates the payload — it is not an
  applied-deduction ack.
- The scanner never computes usage on its own; its SELF_DIRECTED auto-sync is
  off in middleware setups and suppressed for nominal weights everywhere.
- `cmd/update_remaining` is OpenPrintTag-only and fails gracefully on
  OpenTag3D. `cmd/write_tag` is **not** kind-aware and would rewrite an
  OpenTag3D tag as OpenPrintTag. The middleware must gate both on
  `tag_format == "openprinttag"`; a scanner-side kind-guard is a scanner
  follow-up.

## Design

### 1. Parse the new fields

`middleware/opentag3d/parser.py` reads `weight_source` and
`pending_deduction_g` from the scan payload into two new optional `ScanEvent`
fields (default `None`). No other parser sets them, matching the contract.

### 2. Expose Spoolman's real remaining weight

`SpoolInfo` gains `spoolman_remaining_g: Optional[float]`, set from Spoolman's
`remaining_weight` in `sync_spool_from_scan` in **both** merge branches. Today
`prefer_tag=True` leaves `remaining_weight_g` as the tag value, so Spoolman's
number is unavailable downstream. Merge semantics are otherwise unchanged.

### 3. Baseline rule (the core change)

At scan-record time (`mqtt_handler` → `record_tracking`), for every tag
format that flows through the rich-tag scan path (UID-only scans keep their
separate path):

| Case | Baseline |
|---|---|
| Spoolman knows the spool and has a remaining weight | `spoolman_remaining_g − (pending_deduction_g or 0)`, floored at 0 |
| Spoolman match without remaining-weight data | Fall through to the rows below |
| No Spoolman match, measured or legacy | Tag weight (today's behavior) |
| No Spoolman match, nominal | `None` — no deduction until Spoolman knows the spool (logged) |

`record_tracking` relaxes to accept `remaining=None` so the uid, device, and
tag format are still stored — the toolchanger usage path and mobile deduction
routing need them, and the AFC loop already skips `weight_g is None`. The
scan-time low-spool check uses the same chosen value and is skipped when
`None`, so nominal tags stop reading as "1000 g remaining" for free.

Staged flows (`afc_stage`, `toolhead_stage`, mobile staging) get the same
rule: activation computes the baseline once per scan and carries it in a new
`pending["baseline_g"]` key — `remaining_g` keeps its display/mobile-echo
meaning — and the lane-load / ASSIGN_SPOOL consumers record from it.

### 4. Write-back gate

`tag_sync/policy.py` `build_write_plan` returns `None` unless the scanned tag
is OpenPrintTag. The stale-check compares `spoolman_remaining_g` against the
tag value — fixing a latent bug where it compared the tag value to itself
(consequence of §2) and could never fire. Write-back remains opt-in,
default-off, downward-only.

### 5. Explicitly unchanged

- `cmd/deduct` still flows for **all** opentag3d tags, nominal included — the
  scanner routes them to Spoolman. This deliberately ignores #119's "treat as
  non-writable" instruction: the middleware has no Spoolman-direct deduction
  path, so suppressing `cmd/deduct` would drop usage entirely.
- `_WRITABLE_FORMATS`, the toolchanger usage math, UID-only handling, and the
  post-deduction re-baseline + persistence all stay as they are.

### 6. Tests

In `middleware/tests/` (unittest + mocks, no hardware):

- **Regression:** AFC print → re-scan → print deducts exactly once per print
  and never re-deducts after the re-scan (the #119 acceptance case).
- Nominal tag with no Spoolman match → record stored, no deduction sent.
- Measured / legacy payloads with no Spoolman match → tag baseline, unchanged.
- `pending_deduction_g` subtracted when present; absent treated as 0.
- Parser: `weight_source` and `pending_deduction_g` parsed; absent → `None`.
- Write-back: no plan for opentag3d scans; plan for openprinttag when Spoolman
  remaining < tag remaining (now using `spoolman_remaining_g`).

Test payloads mirror the real parser keys (`spool_weight_measured`), not the
illustrative names from the issue comment.

## Out of scope (follow-ups)

- **Mobile/REST baseline path** — `rest_api.py` records baselines from phone
  scans and needs the same rule; the app will send the same `weight_source`
  semantics in its scan POST. Separate issue to file.
- Activation display fallback (`activation.py` reports the tag's nominal value
  when Spoolman is unmatched) — cosmetic.
- Scanner-side `cmd/write_tag` kind-guard — scanner repo follow-up.

## Acceptance (supersedes #119's list)

- AFC + v2 nominal tag: print, re-scan, print → Spoolman deducted once per
  print; a re-scan never re-deducts.
- AFC + any opentag3d tag with a Spoolman match: baseline comes from Spoolman
  minus pending; tag staleness is irrelevant. **This intentionally changes
  behavior for v1 tags and legacy firmware** — #119's "AFC + v1 unchanged"
  line was based on a wrong premise and is superseded.
- No Spoolman match: measured/legacy → tag baseline as today; nominal → no
  deduction, logged.
- `update_remaining` / `write_tag` are never sent for non-OpenPrintTag tags.

## Process

- Update #119 with the corrected scope; file the mobile follow-up issue.
- Reply to the scanner session: "Yes — ship `pending_deduction_g` in
  `tag/state`."
- CHANGELOG entry under 1.9.0.
