"""
Tests for var_watcher.py — Klipper save_variables file watcher.

Covers sync_from_klipper_vars() reading spool IDs from Klipper's
save_variables.cfg and updating active_spools, plus start_klipper_watcher()
returning an Observer and the KlipperVarHandler event dispatch path.
"""
from __future__ import annotations

import configparser
import os
import sys
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch, call

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

sys.modules.setdefault("paho", MagicMock())
sys.modules.setdefault("paho.mqtt", MagicMock())
sys.modules.setdefault("paho.mqtt.client", MagicMock())
sys.modules.setdefault("watchdog", MagicMock())
sys.modules.setdefault("watchdog.observers", MagicMock())

# FileSystemEventHandler must be a real class, not a MagicMock, so that
# `class KlipperVarHandler(FileSystemEventHandler):` produces a proper subclass
# whose on_modified method body is reachable. Other test files use setdefault which
# leaves a plain MagicMock here — we overwrite unconditionally to guarantee it.
_watchdog_events_mock = MagicMock()
_watchdog_events_mock.FileSystemEventHandler = object
sys.modules["watchdog.events"] = _watchdog_events_mock

# Force var_watcher to re-evaluate with the corrected watchdog.events mock.
# When the full test suite runs alphabetically, earlier test files import app_state
# and watchdog shims first — var_watcher may have been cached under the plain-mock
# version of FileSystemEventHandler before this module runs.
import importlib
if "var_watcher" in sys.modules:
    del sys.modules["var_watcher"]

import app_state  # noqa: E402
import var_watcher as _vw_module  # noqa: E402  (force fresh import with correct shims)
from var_watcher import sync_from_klipper_vars, start_klipper_watcher, KlipperVarHandler  # noqa: E402


def _reset_app_state(klipper_var_path: str = "") -> None:
    app_state.cfg = {
        "klipper_var_path": klipper_var_path,
        "toolheads": ["T0", "T1"],
    }
    app_state.active_spools = {}
    app_state.state_lock = threading.Lock()


def _write_var_file(path: str, variables: dict[str, str]) -> None:
    """Write a minimal save_variables.cfg so tests don't need real Klipper files."""
    cp = configparser.ConfigParser()
    cp["variables"] = variables
    with open(path, "w") as f:
        cp.write(f)


class TestSyncFromKlipperVars(unittest.TestCase):

    def setUp(self) -> None:
        _reset_app_state()

    def test_reads_spool_ids_into_active_spools(self) -> None:
        # Normal path: both tools have spool IDs stored in Klipper variables
        with tempfile.NamedTemporaryFile(suffix=".cfg", delete=False, mode="w") as f:
            tmp_path = f.name
        try:
            _write_var_file(tmp_path, {"t0_spool_id": "7", "t1_spool_id": "12"})
            _reset_app_state(klipper_var_path=tmp_path)

            sync_from_klipper_vars()

            self.assertEqual(app_state.active_spools["T0"], 7)
            self.assertEqual(app_state.active_spools["T1"], 12)
        finally:
            os.unlink(tmp_path)

    def test_clears_active_spool_when_var_absent(self) -> None:
        # If the variable is missing from the file, we treat the tool as unloaded
        with tempfile.NamedTemporaryFile(suffix=".cfg", delete=False, mode="w") as f:
            tmp_path = f.name
        try:
            _write_var_file(tmp_path, {})  # no spool IDs at all
            _reset_app_state(klipper_var_path=tmp_path)
            app_state.active_spools["T0"] = 5  # previously loaded

            sync_from_klipper_vars()

            self.assertIsNone(app_state.active_spools["T0"])
        finally:
            os.unlink(tmp_path)

    def test_does_nothing_when_path_not_configured(self) -> None:
        # No klipper_var_path means we're in AFC mode — watcher should be a no-op
        _reset_app_state(klipper_var_path="")
        app_state.active_spools["T0"] = 3

        sync_from_klipper_vars()

        # State untouched — no path means we never tried to read anything
        self.assertEqual(app_state.active_spools["T0"], 3)

    def test_does_nothing_when_file_missing(self) -> None:
        # File may be temporarily absent during a Klipper restart — should not crash
        _reset_app_state(klipper_var_path="/tmp/spoolsense_nonexistent_vars.cfg")

        sync_from_klipper_vars()  # must not raise

        self.assertEqual(app_state.active_spools, {})

    def test_no_variables_section_is_ignored(self) -> None:
        # ConfigParser file without a [variables] section is valid but empty
        with tempfile.NamedTemporaryFile(suffix=".cfg", delete=False, mode="w") as f:
            f.write("[other_section]\nfoo = bar\n")
            tmp_path = f.name
        try:
            _reset_app_state(klipper_var_path=tmp_path)

            sync_from_klipper_vars()

            # No crash, no state changes
            self.assertEqual(app_state.active_spools, {})
        finally:
            os.unlink(tmp_path)

    def test_non_integer_spool_id_is_skipped(self) -> None:
        # Malformed variable value should not crash or corrupt state
        with tempfile.NamedTemporaryFile(suffix=".cfg", delete=False, mode="w") as f:
            tmp_path = f.name
        try:
            _write_var_file(tmp_path, {"t0_spool_id": "not_a_number"})
            _reset_app_state(klipper_var_path=tmp_path)

            sync_from_klipper_vars()

            self.assertNotIn("T0", app_state.active_spools)
        finally:
            os.unlink(tmp_path)

    def test_does_not_overwrite_when_value_unchanged(self) -> None:
        # Avoid redundant state mutations — only update if the value actually differs
        with tempfile.NamedTemporaryFile(suffix=".cfg", delete=False, mode="w") as f:
            tmp_path = f.name
        try:
            _write_var_file(tmp_path, {"t0_spool_id": "9"})
            _reset_app_state(klipper_var_path=tmp_path)
            app_state.active_spools["T0"] = 9  # already correct

            sync_from_klipper_vars()

            self.assertEqual(app_state.active_spools["T0"], 9)
        finally:
            os.unlink(tmp_path)


class TestStartKlipperWatcher(unittest.TestCase):

    def setUp(self) -> None:
        _reset_app_state()

    def test_returns_none_when_path_not_configured(self) -> None:
        # AFC mode never calls start_klipper_watcher, but defensive check still matters
        _reset_app_state(klipper_var_path="")

        result = start_klipper_watcher()

        self.assertIsNone(result)

    def test_returns_none_when_directory_missing(self) -> None:
        # Parent directory must exist for watchdog to schedule — graceful fallback otherwise
        _reset_app_state(klipper_var_path="/tmp/nonexistent_dir_xyz/vars.cfg")

        result = start_klipper_watcher()

        self.assertIsNone(result)

    def test_returns_observer_when_path_configured(self) -> None:
        # Happy path: valid directory exists → watcher starts and returns Observer
        with tempfile.TemporaryDirectory() as tmpdir:
            var_path = os.path.join(tmpdir, "save_variables.cfg")
            _reset_app_state(klipper_var_path=var_path)

            mock_observer = MagicMock()
            with patch("var_watcher.Observer", return_value=mock_observer):
                result = start_klipper_watcher()

            self.assertIs(result, mock_observer)
            mock_observer.schedule.assert_called_once()
            mock_observer.start.assert_called_once()


class TestKlipperVarHandler(unittest.TestCase):

    def setUp(self) -> None:
        _reset_app_state()

    def test_on_modified_calls_sync_when_path_matches(self) -> None:
        # File change on the watched path triggers a re-read of variables
        target_path = "/tmp/save_variables.cfg"
        _reset_app_state(klipper_var_path=target_path)

        event = MagicMock()
        event.src_path = target_path

        handler = KlipperVarHandler()
        with patch("var_watcher.sync_from_klipper_vars") as mock_sync, \
             patch("time.sleep"):
            handler.on_modified(event)
            mock_sync.assert_called_once()

    def test_on_modified_ignores_unrelated_files(self) -> None:
        # A change to another file in the same dir (e.g. printer.cfg) must be ignored
        _reset_app_state(klipper_var_path="/tmp/save_variables.cfg")

        event = MagicMock()
        event.src_path = "/tmp/printer.cfg"

        handler = KlipperVarHandler()
        with patch("var_watcher.sync_from_klipper_vars") as mock_sync, \
             patch("time.sleep"):
            handler.on_modified(event)
            mock_sync.assert_not_called()


if __name__ == "__main__":
    unittest.main()
