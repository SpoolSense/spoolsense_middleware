from __future__ import annotations

import configparser
import json
import logging
import os
import time

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

import app_state
from activation import publish_lock

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


def sync_from_afc_file() -> None:
    """
    The core logic for keeping AFC in sync.
    AFC writes its state to a JSON file (AFC.var.unit). We watch that file.
    When AFC changes state (e.g., finishes loading, or user ejects a spool), this runs.
    """
    path = app_state.cfg["afc_var_path"]
    if not os.path.exists(path):
        logger.warning(f"AFC var file not found: {path}")
        return

    try:
        with open(path, "r") as f:
            data = json.load(f)

        for unit_name, unit_data in data.items():
            if unit_name == "system":
                continue

            for lane_name, lane_data in unit_data.items():
                spool_id = lane_data.get("spool_id")
                status = lane_data.get("status")
                is_locked = app_state.lane_locks.get(lane_name, False)

                # 1. Save the AFC status so our LED logic knows if it's safe to override
                app_state.lane_statuses[lane_name] = status

                if spool_id:
                    # 2. If AFC has a spool, lock our NFC reader so it ignores new scans
                    if not is_locked:
                        logger.info(f"AFC Sync: {lane_name} has spool {spool_id}, locking")
                        publish_lock(lane_name, "lock")
                    app_state.active_spools[lane_name] = spool_id
                else:
                    # 3. If AFC says the lane is empty, unlock the NFC reader so it can scan again
                    if is_locked:
                        logger.info(f"AFC Sync: {lane_name} empty, clearing")
                        publish_lock(lane_name, "clear")
                    app_state.active_spools[lane_name] = None
    except Exception as e:
        logger.error(f"AFC Sync failed: {e}")


class VarFileHandler(FileSystemEventHandler):
    """Watches the file system. When Klipper or AFC modifies their save file, it triggers our sync functions."""

    def on_modified(self, event: object) -> None:
        time.sleep(0.5)  # Give the OS a half-second to finish writing the file before we read it
        if event.src_path == app_state.cfg["afc_var_path"]:
            sync_from_afc_file()
        elif event.src_path == app_state.cfg.get("klipper_var_path"):
            sync_from_klipper_vars()


def start_watcher() -> Observer:
    """Hooks the VarFileHandler into the operating system's file watcher."""
    observer = Observer()
    handler = VarFileHandler()

    if app_state.cfg["toolhead_mode"] == "afc":
        afc_dir = os.path.dirname(app_state.cfg["afc_var_path"])
        if os.path.exists(afc_dir):
            observer.schedule(handler, afc_dir, recursive=False)
            logger.info(f"Watching AFC var file in {afc_dir}")
    else:
        klipper_path = app_state.cfg.get("klipper_var_path")
        if klipper_path:
            klipper_dir = os.path.dirname(klipper_path)
            if os.path.exists(klipper_dir):
                observer.schedule(handler, klipper_dir, recursive=False)
                logger.info(f"Watching Klipper var file in {klipper_dir}")

    observer.start()
    return observer
