"""
backoff.py — Shared retry delay for the Moonraker reconnect and poll loops.

The exponent is clamped before the power so the math stays small no matter
how long the printer has been offline. Without the clamp, failure 1025 makes
`2 ** 1024`, which cannot convert to float and raised OverflowError, killing
the thread for good (#128).
"""
from __future__ import annotations

# 2 ** 16 is far past any sane cap, so the clamp never shortens a real delay.
MAX_BACKOFF_EXPONENT: int = 16


def backoff_delay(failures: int, base: float, maximum: float) -> float:
    """Return the wait in seconds after `failures` consecutive failures.

    Doubles from `base` on each failure (base, 2*base, 4*base, ...) and is
    capped at `maximum`. Failures below 1 are treated as the first failure.
    """
    exponent = min(max(failures, 1), MAX_BACKOFF_EXPONENT) - 1
    return min(base * (2 ** exponent), maximum)
