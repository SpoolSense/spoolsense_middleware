"""
publishers/base.py — Core types for the SpoolSense publisher system.

Defines the SpoolEvent dataclass (platform-agnostic spool assignment event)
and the Publisher ABC (contract all publishers must implement).

Adding a new output target (Bambu, Prusa, OctoPrint, MQTT, etc.) means:
  1. Create a new file in publishers/
  2. Implement Publisher ABC
  3. Register it in spoolsense.py main()

No changes to activation.py or mqtt_handler.py needed.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum


class Action(str, Enum):
    """
    Spool activation action types.

    Values correspond to the 'action' key in scanner config entries.
    They are platform-prefixed by convention (e.g., "afc_" for AFC-specific
    actions, "toolhead" for direct toolhead assignment).

    Publishers return True for unknown action values (no-op success) so that
    new action types added in future PRs do not break existing publishers.
    """
    AFC_STAGE = "afc_stage"
    AFC_LANE = "afc_lane"
    TOOLHEAD = "toolhead"
    TOOLHEAD_STAGE = "toolhead_stage"


@dataclass
class SpoolEvent:
    """
    Platform-agnostic spool assignment event.

    Constructed by the activation orchestrator (activation.py) from resolved
    ScanEvent + SpoolInfo data. Publishers translate this into platform-specific
    commands without touching raw tag data or Spoolman internals.

    Fields:
        spool_id        Spoolman spool ID, or None in tag-only mode.
        action          Routing action (see Action enum). Publishers branch on this
                        to determine what commands to send.
        target          Lane name (afc_lane), toolhead macro name (toolhead/toolhead_stage),
                        or empty string for shared-scanner actions (afc_stage).
        color           Resolved hex color string (no '#' prefix), or None.
        material        Resolved filament type string, or None.
        weight          Remaining spool weight in grams, or None.
        nozzle_temp_min Min nozzle temperature from tag data, or None.
        nozzle_temp_max Max nozzle temperature from tag data, or None.
        bed_temp_min    Min bed temperature from tag data, or None.
        bed_temp_max    Max bed temperature from tag data, or None.
        scanner_id      Device ID of the scanner that triggered this event.
        tag_only        True when there is no Spoolman backing (spool_id is None,
                        data comes from tag fields alone).
    """

    spool_id: int | None
    action: Action
    target: str
    color: str | None
    material: str | None
    weight: float | None
    nozzle_temp_min: int | None
    nozzle_temp_max: int | None
    bed_temp_min: int | None
    bed_temp_max: int | None
    scanner_id: str
    tag_only: bool


class Publisher(ABC):
    """
    Abstract base class for all SpoolSense output publishers.

    Implement this interface to add a new output target. Register the instance
    via publisher_manager.register() during startup in spoolsense.py.

    Contract:
    - enabled() is called once at startup. If it returns False, the publisher
      is skipped entirely and publish() is never called.
    - publish() must not raise. Catch all exceptions internally and return False.
    - teardown() is called on graceful shutdown. No-op for stateless publishers.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable identifier used in logs (e.g., "klipper")."""
        ...

    @property
    @abstractmethod
    def primary(self) -> bool:
        """
        True if this is the primary platform publisher.

        The primary publisher's success/failure return value is propagated back
        to the orchestrator to drive lock decisions. Secondary publishers
        (lane_data, mqtt) run after the primary; their failures are logged but
        do not affect the activation flow.

        Exactly one publisher should be primary for a given configuration.
        """
        ...

    @abstractmethod
    def enabled(self, config: dict) -> bool:
        """
        Return True if this publisher should be active given the current config.

        Called once during publisher registration at startup. The publisher
        receives config at __init__ time and should hold a reference if needed
        for publish().
        """
        ...

    @abstractmethod
    def publish(self, event: SpoolEvent) -> bool:
        """
        Handle a spool assignment event.

        Returns True on success, False on failure. Must not raise — catch all
        exceptions internally and return False.

        Publishers should return True for unknown action values (no-op success)
        so that new actions added in future PRs do not break existing publishers.
        """
        ...

    def teardown(self) -> None:
        """
        Clean up resources on shutdown.

        Called by PublisherManager.shutdown() during spoolsense.py shutdown.
        Stateless publishers (klipper, lane_data) need not override this.
        Connection-holding publishers (websocket, persistent MQTT) use it to
        close connections and cancel timers.
        """
