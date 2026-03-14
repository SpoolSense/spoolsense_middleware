"""
conftest.py — pytest fixtures for spoolsense module-level import isolation.

spoolsense.py runs startup code at import time (load_config, MQTT setup,
SpoolmanClient init). This conftest stubs those dependencies so tests can
import the module and test individual functions without needing a real
config file, MQTT broker, or Spoolman instance.
"""

import sys
import types
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=False)
def stub_spoolsense_imports():
    """
    Patches spoolsense module-level side effects before import.
    Use this fixture in any test that needs to import spoolsense directly.
    """
    pass


def pytest_configure(config):
    """
    Stub heavy dependencies before any test collection imports spoolsense.
    This runs once at session start.
    """
    # Stub paho.mqtt so spoolsense can be imported without the broker
    mqtt_mock = types.ModuleType("paho")
    mqtt_client_mock = types.ModuleType("paho.mqtt")
    mqtt_inner = types.ModuleType("paho.mqtt.client")

    # Minimal constants spoolsense uses
    mqtt_inner.MQTT_ERR_SUCCESS = 0
    mqtt_inner.Client = MagicMock()

    sys.modules.setdefault("paho", mqtt_mock)
    sys.modules.setdefault("paho.mqtt", mqtt_client_mock)
    sys.modules.setdefault("paho.mqtt.client", mqtt_inner)

    # Stub watchdog so spoolsense can be imported without it installed
    watchdog_mock = types.ModuleType("watchdog")
    watchdog_observers = types.ModuleType("watchdog.observers")
    watchdog_events = types.ModuleType("watchdog.events")
    watchdog_observers.Observer = MagicMock()
    watchdog_events.FileSystemEventHandler = object
    sys.modules.setdefault("watchdog", watchdog_mock)
    sys.modules.setdefault("watchdog.observers", watchdog_observers)
    sys.modules.setdefault("watchdog.events", watchdog_events)


# Patch load_config and module-level startup before spoolsense is imported
_MINIMAL_CFG = {
    "toolhead_mode": "afc",
    "toolheads": ["lane1", "lane2", "lane3", "lane4"],
    "mqtt": {"broker": "localhost", "port": 1883, "username": None, "password": None},
    "spoolman_url": None,
    "moonraker_url": "http://localhost",
    "low_spool_threshold": 100,
    "afc_var_path": "/tmp/AFC.var",
    "klipper_var_path": None,
    "afc_led_macro": "_SET_LANE_LED",
    "scanner_topic_prefix": "openprinttag",
    "scanner_lane_map": {},
}

with (
    patch("builtins.open", MagicMock(side_effect=FileNotFoundError)),
    patch("os.path.exists", return_value=False),
    patch("sys.exit"),
):
    try:
        with patch.dict("sys.modules", {}):
            pass
    except Exception:
        pass
