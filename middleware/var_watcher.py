"""
var_watcher.py — Klipper save_variables file watcher.

Monitors Klipper's save_variables.cfg for changes (e.g., user manually
changes a spool in the UI) and syncs internal state. Used for single
and toolchanger modes only — AFC mode uses afc_status.py instead.
"""
from __future__ import annotations

import configparser
import logging
import os
import time

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

import app_state

logger = logging.getLogger(__name__)


def sync_from_klipper_vars() -> None:
    """
    Reads Klipper's save_variables.cfg.
    If a user manually changes a spool in the UI, this catches it and updates internal state.
    """
    path: str | None = app_state.cfg.get("klipper_var_path")
    if not path or not os.path.exists(path):
        return

    try:
        cp = configparser.ConfigParser()
        cp.read(path)
        if 'variables' not in cp:
            return

        for t in app_state.cfg["toolheads"]:
            var_name = f"t{t[-1]}_spool_id"
            spool_id_str = cp['variables'].get(var_name)

            if spool_id_str:
                try:
                    spool_id = int(spool_id_str)
                    if app_state.active_spools.get(t) != spool_id:
                        logger.info(f"Klipper Sync: {t} -> spool {spool_id}")
                        app_state.active_spools[t] = spool_id
                except ValueError:
                    pass
            elif app_state.active_spools.get(t):
                logger.info(f"Klipper Sync: {t} cleared")
                app_state.active_spools[t] = None
    except Exception as e:
        logger.error(f"Klipper Sync failed: {e}")


class KlipperVarHandler(FileSystemEventHandler):
    """Watches Klipper's save_variables file for changes."""

    def on_modified(self, event: object) -> None:
        time.sleep(0.5)
        if event.src_path == app_state.cfg.get("klipper_var_path"):
            sync_from_klipper_vars()


def start_klipper_watcher() -> Observer | None:
    """
    Starts a file watcher for Klipper's save_variables file.

    Returns the Observer, or None if no klipper_var_path is configured.
    Only used for single/toolchanger modes — AFC mode uses afc_status.py.
    """
    klipper_path = app_state.cfg.get("klipper_var_path")
    if not klipper_path:
        return None

    klipper_dir = os.path.dirname(klipper_path)
    if not os.path.exists(klipper_dir):
        logger.warning(f"Klipper var directory not found: {klipper_dir}")
        return None

    observer = Observer()
    handler = KlipperVarHandler()
    observer.schedule(handler, klipper_dir, recursive=False)
    observer.start()
    logger.info(f"Watching Klipper var file in {klipper_dir}")
    return observer
