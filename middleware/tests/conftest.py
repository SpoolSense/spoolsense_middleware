"""Shared test fixtures.

Redirects the middleware's on-disk state files into the pytest tmp dir for
EVERY test, so no test — present or future — can write ~/SpoolSense/*.json
on a dev machine or in CI just by reaching a persistence path.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_state_files(tmp_path, monkeypatch):
    import app_state
    monkeypatch.setattr(app_state, "TRACKING_FILE", str(tmp_path / "tracking.json"))
    monkeypatch.setattr(app_state, "DEDUCTIONS_FILE", str(tmp_path / "deductions.json"))
