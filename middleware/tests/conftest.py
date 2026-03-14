"""
conftest.py — pytest session setup for the middleware test suite.

After the Phase 1 refactor, spoolsense.py no longer runs startup code at
import time. All runtime logic lives in main(). This means tests can simply:

    import spoolsense

without triggering config loading, MQTT connections, or sys.exit() calls.

paho.mqtt and watchdog are still stubbed here so the test suite can run
without those packages installed. test_scanner_writer.py imports
paho.mqtt.client directly for MQTT_ERR_SUCCESS / MQTT_ERR_NO_CONN constants,
which are provided by the stub below.
"""

import sys
import types
from unittest.mock import MagicMock


def pytest_configure(config):
    """
    Stub paho.mqtt and watchdog before any test module is imported.
    Runs once at session start.
    """
    # --- paho.mqtt stub ---
    paho_mock = types.ModuleType("paho")
    paho_mqtt_mock = types.ModuleType("paho.mqtt")
    paho_client_mock = types.ModuleType("paho.mqtt.client")

    paho_client_mock.MQTT_ERR_SUCCESS = 0
    paho_client_mock.MQTT_ERR_NO_CONN = 4
    paho_client_mock.Client = MagicMock()

    sys.modules.setdefault("paho", paho_mock)
    sys.modules.setdefault("paho.mqtt", paho_mqtt_mock)
    sys.modules.setdefault("paho.mqtt.client", paho_client_mock)

    # --- watchdog stub ---
    watchdog_mock = types.ModuleType("watchdog")
    watchdog_observers = types.ModuleType("watchdog.observers")
    watchdog_events = types.ModuleType("watchdog.events")

    watchdog_observers.Observer = MagicMock()
    watchdog_events.FileSystemEventHandler = object

    sys.modules.setdefault("watchdog", watchdog_mock)
    sys.modules.setdefault("watchdog.observers", watchdog_observers)
    sys.modules.setdefault("watchdog.events", watchdog_events)
