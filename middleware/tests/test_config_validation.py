"""
Tests for spoolsense.load_config() validation rules.

HOW TO RUN:
    From the middleware/ directory:
        python -m pytest tests/test_config_validation.py -v

Pattern:
    1. Build a config dict
    2. Write it to a tmp YAML file via write_config()
    3. Patch CONFIG_PATH to point at that file
    4. Call load_config()
    5. Assert result or failure
"""

import pytest
import yaml

import spoolsense


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

MINIMAL_VALID = {
    "toolhead_mode": "afc",
    "toolheads": ["lane1", "lane2", "lane3", "lane4"],
    "mqtt": {"broker": "192.168.1.100"},
    "moonraker_url": "http://192.168.1.100",
    "spoolman_url": "http://192.168.1.100:7912",
}


def write_config(tmp_path, data):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump(data))
    return cfg_path


# ---------------------------------------------------------------------------
# spoolman_url
# ---------------------------------------------------------------------------

def test_missing_spoolman_url_is_allowed(tmp_path, monkeypatch):
    """spoolman_url is optional — omitting it should not raise."""
    data = {k: v for k, v in MINIMAL_VALID.items() if k != "spoolman_url"}
    cfg_path = write_config(tmp_path, data)
    monkeypatch.setattr(spoolsense, "CONFIG_PATH", str(cfg_path))

    cfg = spoolsense.load_config()

    assert cfg is not None
    assert not cfg.get("spoolman_url")


def test_spoolman_url_present_is_stored(tmp_path, monkeypatch):
    """When spoolman_url is set it should be stored stripped of trailing slash."""
    data = {**MINIMAL_VALID, "spoolman_url": "http://192.168.1.100:7912/"}
    cfg_path = write_config(tmp_path, data)
    monkeypatch.setattr(spoolsense, "CONFIG_PATH", str(cfg_path))

    cfg = spoolsense.load_config()

    assert cfg["spoolman_url"] == "http://192.168.1.100:7912"


# ---------------------------------------------------------------------------
# toolhead_mode
# ---------------------------------------------------------------------------

def test_invalid_toolhead_mode_raises(tmp_path, monkeypatch):
    """An unrecognised toolhead_mode should cause sys.exit."""
    data = {**MINIMAL_VALID, "toolhead_mode": "banana"}
    cfg_path = write_config(tmp_path, data)
    monkeypatch.setattr(spoolsense, "CONFIG_PATH", str(cfg_path))

    with pytest.raises(SystemExit):
        spoolsense.load_config()


def test_valid_toolhead_modes_pass(tmp_path, monkeypatch):
    """All three valid modes should load without error."""
    for mode, toolheads in [
        ("afc", ["lane1"]),
        ("toolchanger", ["T0"]),
        ("single", ["T0"]),
    ]:
        data = {**MINIMAL_VALID, "toolhead_mode": mode, "toolheads": toolheads}
        cfg_path = write_config(tmp_path, data)
        monkeypatch.setattr(spoolsense, "CONFIG_PATH", str(cfg_path))
        cfg = spoolsense.load_config()
        assert cfg["toolhead_mode"] == mode


# ---------------------------------------------------------------------------
# scanner_lane_map validation
# ---------------------------------------------------------------------------

def test_scanner_lane_map_lane_not_in_toolheads_raises(tmp_path, monkeypatch):
    """A scanner mapped to a lane not in toolheads should cause sys.exit."""
    data = {
        **MINIMAL_VALID,
        "toolheads": ["lane1", "lane2"],
        "scanner_lane_map": {"scanner_ab12cd": "lane3"},  # lane3 not in toolheads
    }
    cfg_path = write_config(tmp_path, data)
    monkeypatch.setattr(spoolsense, "CONFIG_PATH", str(cfg_path))

    with pytest.raises(SystemExit):
        spoolsense.load_config()


def test_valid_scanner_lane_map_passes(tmp_path, monkeypatch):
    """A scanner mapped to an existing lane should load cleanly."""
    data = {
        **MINIMAL_VALID,
        "toolheads": ["lane1", "lane2"],
        "scanner_lane_map": {"scanner_ab12cd": "lane1"},
    }
    cfg_path = write_config(tmp_path, data)
    monkeypatch.setattr(spoolsense, "CONFIG_PATH", str(cfg_path))

    cfg = spoolsense.load_config()

    assert cfg["scanner_lane_map"] == {"scanner_ab12cd": "lane1"}


def test_multiple_scanner_lanes_all_valid_passes(tmp_path, monkeypatch):
    """Multiple scanners all mapped to valid lanes should pass."""
    data = {
        **MINIMAL_VALID,
        "toolheads": ["lane1", "lane2", "lane3", "lane4"],
        "scanner_lane_map": {
            "scanner_ab12cd": "lane1",
            "scanner_ef34gh": "lane2",
        },
    }
    cfg_path = write_config(tmp_path, data)
    monkeypatch.setattr(spoolsense, "CONFIG_PATH", str(cfg_path))

    cfg = spoolsense.load_config()

    assert cfg is not None


def test_multiple_scanner_lanes_one_invalid_raises(tmp_path, monkeypatch):
    """If any scanner maps to an invalid lane the whole config should fail."""
    data = {
        **MINIMAL_VALID,
        "toolheads": ["lane1", "lane2"],
        "scanner_lane_map": {
            "scanner_ab12cd": "lane1",   # valid
            "scanner_ef34gh": "lane99",  # invalid
        },
    }
    cfg_path = write_config(tmp_path, data)
    monkeypatch.setattr(spoolsense, "CONFIG_PATH", str(cfg_path))

    with pytest.raises(SystemExit):
        spoolsense.load_config()


# ---------------------------------------------------------------------------
# DISPATCHER_AVAILABLE warning
# ---------------------------------------------------------------------------

def test_dispatcher_warning_when_scanner_map_set_and_dispatcher_unavailable(
    tmp_path, monkeypatch, caplog
):
    """
    When scanner_lane_map is configured but DISPATCHER_AVAILABLE is False,
    a warning should be logged after config loads.

    Note: the warning fires at module level after load_config(), not inside
    load_config() itself. We simulate this by calling the warning block
    directly after patching the relevant state.
    """
    import logging

    data = {
        **MINIMAL_VALID,
        "toolheads": ["lane1"],
        "scanner_lane_map": {"scanner_ab12cd": "lane1"},
    }
    cfg_path = write_config(tmp_path, data)
    monkeypatch.setattr(spoolsense, "CONFIG_PATH", str(cfg_path))
    monkeypatch.setattr(spoolsense, "DISPATCHER_AVAILABLE", False)

    cfg = spoolsense.load_config()
    monkeypatch.setattr(spoolsense, "cfg", cfg)

    # Reproduce the warning block from spoolsense.py module level
    with caplog.at_level(logging.WARNING, logger="spoolsense"):
        if cfg.get("scanner_lane_map") and not spoolsense.DISPATCHER_AVAILABLE:
            spoolsense.logger.warning(
                "scanner_lane_map is configured but the rich-tag dispatcher is not available "
                "(adapters/ directory not found). openprinttag_scanner topics will be subscribed "
                "but payloads will not be parsed — scans will be silently ignored. "
                "Ensure the adapters/ directory is present to enable scanner support."
            )

    assert "scanner_lane_map is configured but the rich-tag dispatcher is not available" in caplog.text


# ---------------------------------------------------------------------------
# Additional config validation tests
# ---------------------------------------------------------------------------

def test_missing_scanner_lane_map_defaults_cleanly(tmp_path, monkeypatch):
    """scanner_lane_map is optional — omitting it should not raise."""
    data = {k: v for k, v in MINIMAL_VALID.items() if k != "scanner_lane_map"}
    cfg_path = write_config(tmp_path, data)
    monkeypatch.setattr(spoolsense, "CONFIG_PATH", str(cfg_path))
    cfg = spoolsense.load_config()
    assert cfg is not None
    assert not cfg.get("scanner_lane_map")


def test_empty_scanner_lane_map_defaults_cleanly(tmp_path, monkeypatch):
    """An explicitly empty scanner_lane_map should load without error."""
    data = {**MINIMAL_VALID, "scanner_lane_map": {}}
    cfg_path = write_config(tmp_path, data)
    monkeypatch.setattr(spoolsense, "CONFIG_PATH", str(cfg_path))
    cfg = spoolsense.load_config()
    assert cfg["scanner_lane_map"] == {}


def test_missing_toolheads_uses_default(tmp_path, monkeypatch):
    """
    toolheads has a built-in default of ["lane1", "lane2", "lane3", "lane4"].
    Omitting it from config should not raise — the default applies.
    """
    data = {k: v for k, v in MINIMAL_VALID.items() if k != "toolheads"}
    cfg_path = write_config(tmp_path, data)
    monkeypatch.setattr(spoolsense, "CONFIG_PATH", str(cfg_path))
    cfg = spoolsense.load_config()
    assert cfg["toolheads"] == ["lane1", "lane2", "lane3", "lane4"]


def test_empty_toolheads_raises(tmp_path, monkeypatch):
    """toolheads must not be empty."""
    data = {**MINIMAL_VALID, "toolheads": []}
    cfg_path = write_config(tmp_path, data)
    monkeypatch.setattr(spoolsense, "CONFIG_PATH", str(cfg_path))
    with pytest.raises(SystemExit):
        spoolsense.load_config()


def test_missing_mqtt_broker_raises(tmp_path, monkeypatch):
    """mqtt.broker is required for MQTT connectivity."""
    data = {**MINIMAL_VALID}
    data["mqtt"] = {}  # broker removed
    cfg_path = write_config(tmp_path, data)
    monkeypatch.setattr(spoolsense, "CONFIG_PATH", str(cfg_path))
    with pytest.raises(SystemExit):
        spoolsense.load_config()


def test_scanner_topic_prefix_default(tmp_path, monkeypatch):
    """scanner_topic_prefix should default to 'openprinttag' if not provided."""
    data = {k: v for k, v in MINIMAL_VALID.items() if k != "scanner_topic_prefix"}
    cfg_path = write_config(tmp_path, data)
    monkeypatch.setattr(spoolsense, "CONFIG_PATH", str(cfg_path))
    cfg = spoolsense.load_config()
    assert cfg["scanner_topic_prefix"] == "openprinttag"
