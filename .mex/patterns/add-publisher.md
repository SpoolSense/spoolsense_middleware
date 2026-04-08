---
name: add-publisher
description: Steps to add a new output publisher (e.g. Bambu, Prusa, OctoPrint, MQTT fan-out).
triggers:
  - "new publisher"
  - "add Bambu"
  - "add Prusa"
  - "support for"
  - "output target"
last_updated: 2026-04-05
---

# Pattern: Add a New Publisher

## When to Use

When adding a new platform/output target that receives spool activation events. The existing `KlipperPublisher` is the reference implementation.

## Steps

1. **Create `publishers/<name>.py`** — implement the `Publisher` ABC from `publishers/base.py`:
   - `name` property — short string used in logs (e.g. `"bambu"`)
   - `primary` property — return `False` unless this replaces KlipperPublisher as the main platform; exactly one primary publisher should be active for any given config
   - `enabled(config)` — return `True` only if the config has the necessary fields (e.g. `config.get("bambu_url")`)
   - `publish(event: SpoolEvent) -> bool` — translate SpoolEvent into platform commands; catch all exceptions internally, never raise; return `True` on success, `False` on failure

2. **Register in `spoolsense.py main()`** — after `PublisherManager()` is created:
   ```python
   from publishers.bambu import BambuPublisher
   app_state.publisher_manager.register(BambuPublisher(app_state.cfg))
   ```

3. **Handle all four `Action` values** in `publish()`:
   - `Action.AFC_STAGE` — spool staged for next AFC load
   - `Action.AFC_LANE` — spool assigned to specific AFC lane
   - `Action.TOOLHEAD` — spool assigned to specific toolhead
   - `Action.TOOLHEAD_STAGE` — spool staged for next tool pickup
   - Unknown actions → return `True` (no-op, forward-compatible)

4. **Write tests** in `middleware/tests/test_<name>_publisher.py` following the pattern in `test_activation.py`:
   - Mock `requests.post/get` (or whatever HTTP client is used)
   - Use `_setup_app_state()` helper pattern to reset `app_state` before each test
   - Test all four action types + error path (HTTP failure returns False)

## Key Constraints

- `publish()` must never raise — the `PublisherManager` expects a `bool` return; unhandled exceptions in a secondary publisher would be caught by `PublisherManager.publish()` anyway, but it's better to handle them explicitly
- Secondary publishers (non-primary) do not affect lock decisions — their failures are logged but activation continues
- The `SpoolEvent.tag_only` field indicates Spoolman is not available — the publisher must handle this gracefully (e.g. skip spool_id activation but still set color)
- `SpoolEvent.spool_id` can be `None` in tag-only mode — always guard before using it

## Reference

See `publishers/klipper.py` for the complete reference implementation including tag-only handling, color validation, and the rollback pattern for atomic operations.
