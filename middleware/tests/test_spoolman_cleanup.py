"""Regression tests for the standalone Spoolman cleanup utility."""
from __future__ import annotations

import importlib.util
from pathlib import Path


_SCRIPT = Path(__file__).parents[2] / "scripts" / "spoolman-cleanup.py"
_SPEC = importlib.util.spec_from_file_location("spoolman_cleanup", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
cleanup = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(cleanup)


def test_filaments_without_identity_are_not_grouped_as_duplicates():
    filaments = [{"id": 1}, {"id": 2}, {"id": 3}]
    assert cleanup.find_duplicate_filaments(filaments) == {}


def test_filaments_with_same_identity_are_grouped():
    filaments = [
        {"id": 1, "material": "PLA", "color_hex": "FF0000"},
        {"id": 2, "material": "pla", "color_hex": "ff0000"},
    ]
    groups = cleanup.find_duplicate_filaments(filaments)
    assert list(groups.values()) == [filaments]


def test_nfc_ids_are_compared_case_insensitively():
    spools = [
        {"id": 1, "extra": {"nfc_id": '"AABBCC"'}},
        {"id": 2, "extra": {"nfc_id": '"aabbcc"'}},
    ]
    groups = cleanup.find_duplicate_spools(spools)
    assert list(groups.values()) == [spools]
