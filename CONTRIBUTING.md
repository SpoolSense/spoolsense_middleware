# Contributing to SpoolSense Middleware

Thanks for your interest in contributing! Here's how to get started.

## Reporting Bugs

Open an issue using the Bug Report template. Include:
- Exact steps to reproduce
- Middleware logs (`journalctl -u spoolsense -f`)
- Your config mode (single / toolchanger / AFC)

## Suggesting Features

Open an issue using the Feature Request template. Explain the use case and why it matters for your setup.

## Submitting Pull Requests

1. Fork the repo and create a branch from `dev`
2. Make your changes
3. Test with a live scanner — the middleware depends on real MQTT scan events
4. Open a PR targeting the `dev` branch (not `master`)

### Branch workflow

- `dev` — active development, all PRs target here
- `master` — production releases only, merged from dev when stable

### Code guidelines

- Config templates live in `middleware/config.example.*.yaml`
- Tag format parsers live in their own module (e.g. `middleware/openprinttag/`, `middleware/opentag3d/`)
- All tag formats normalize to `ScanEvent` via the dispatcher
- Spoolman integration is optional — code must work with Spoolman disabled

## Questions?

Open an issue using the Question template or start a discussion.
