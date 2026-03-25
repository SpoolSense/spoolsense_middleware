"""
publisher_manager.py — Fan-out dispatcher for spool activation events.

Maintains a registry of enabled Publisher instances and routes SpoolEvent
objects to all of them. Designed for fault isolation: one publisher failing
never blocks another.

Primary vs Secondary publishers:
    One publisher is designated as primary (e.g., klipper). Its success or
    failure is returned to the orchestrator (activation.py) to drive lock
    decisions. Secondary publishers (lane_data, mqtt) run after the primary;
    their failures are logged but do not affect the activation flow.

    If no primary publisher is registered (or none is enabled), publish()
    returns True so the orchestrator always proceeds.
"""
from __future__ import annotations

import logging

from publishers.base import Publisher, SpoolEvent

logger = logging.getLogger(__name__)


class PublisherManager:
    """
    Registry and fan-out dispatcher for Publisher instances.

    Usage:
        manager = PublisherManager()
        manager.register(KlipperPublisher(cfg))
        manager.register(LaneDataPublisher(cfg))

        success = manager.publish(event)   # True if primary publisher succeeded

    Publishers are stored in registration order. The primary publisher runs
    first; secondary publishers follow regardless of primary success/failure.
    """

    def __init__(self) -> None:
        self._publishers: list[Publisher] = []

    def register(self, publisher: Publisher) -> None:
        """
        Add a publisher to the registry.

        The publisher's enabled() method is called immediately. If disabled,
        the publisher is silently skipped and not added to the registry.
        """
        from app_state import cfg as current_cfg  # imported late to avoid circular import

        if publisher.enabled(current_cfg):
            self._publishers.append(publisher)
            logger.info(
                "Publisher registered: %s (primary=%s)",
                publisher.name,
                publisher.primary,
            )
        else:
            logger.debug(
                "Publisher skipped (disabled): %s",
                publisher.name,
            )

    def publish(self, event: SpoolEvent) -> bool:
        """
        Route a SpoolEvent to all registered publishers.

        Returns True if the primary publisher succeeded (or if no primary
        publisher is registered). Secondary publisher failures are logged
        but do not affect the return value.
        """
        primary_succeeded: bool = True  # default: no primary means proceed
        primary_ran: bool = False

        # Primary publishers first, then secondary
        primaries = [p for p in self._publishers if p.primary]
        secondaries = [p for p in self._publishers if not p.primary]

        for publisher in primaries:
            primary_ran = True
            try:
                result = publisher.publish(event)
            except Exception:
                logger.exception(
                    "Publisher '%s' raised unexpectedly during publish (event=%s action=%s)",
                    publisher.name,
                    event.scanner_id,
                    event.action,
                )
                result = False

            if not result:
                logger.error(
                    "Primary publisher '%s' failed for action=%s target=%s",
                    publisher.name,
                    event.action,
                    event.target,
                )
            primary_succeeded = primary_succeeded and result

        for publisher in secondaries:
            try:
                result = publisher.publish(event)
                if not result:
                    logger.warning(
                        "Secondary publisher '%s' reported failure for action=%s target=%s",
                        publisher.name,
                        event.action,
                        event.target,
                    )
            except Exception:
                logger.exception(
                    "Secondary publisher '%s' raised unexpectedly (event=%s action=%s)",
                    publisher.name,
                    event.scanner_id,
                    event.action,
                )

        if not primary_ran:
            return True
        return primary_succeeded

    def shutdown(self) -> None:
        """Call teardown() on all registered publishers. Used during graceful shutdown."""
        for publisher in self._publishers:
            try:
                publisher.teardown()
            except Exception:
                logger.exception("Publisher '%s' raised during teardown", publisher.name)
