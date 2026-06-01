"""Detects whether the HP schedule is currently allowing cooling.

The Grant R290 firmware bounces work_mode rapidly between `cooling` and
`standby` (every 10-30s) when its internal time-of-day schedule denies
cooling. During scheduled-on time, work_mode stays in `cooling` for
sustained periods (or sits in `standby` between cycles without flapping).

This module observes work_mode transitions in a rolling window and
infers `schedule_active`:

  - If work_mode hasn't transitioned in the last STABLE_WINDOW_S → active.
  - If transitions per minute exceeds FLAP_RATE_PER_MIN → inactive.
  - Otherwise (warm-up after boot): default to active (fail-open).

Why fail-open: a false-positive (schedule_active=True when actually off)
causes at most one wasted 4-min start-timeout per failed attempt. A
false-negative (schedule_active=False when on) would suppress legitimate
cycles. So default-active is safer.

The decision lags ~1-2 minutes after a schedule transition — this is
acceptable since cycles don't fire that quickly anyway.

No HA imports — stateful but pure.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta
from typing import Optional

STABLE_WINDOW_S = 120  # if no transition in 2 min, treat as stable (active)
FLAP_WINDOW_S = 300  # 5-min sliding window
FLAP_RATE_PER_MIN = 3  # > 15 transitions in 5 min = scheduled-off


class ScheduleDetector:
    """Stateful flapping detector for HP cooling schedule."""

    def __init__(self) -> None:
        self._transitions: deque[datetime] = deque()
        self._last_mode: Optional[str] = None
        self._last_transition_at: Optional[datetime] = None

    def observe(self, work_mode: Optional[str], now: datetime) -> None:
        """Record current work_mode reading; advance internal state."""
        if work_mode in (None, "unknown", "unavailable"):
            return  # Don't count transient/unavailable readings.

        if work_mode != self._last_mode:
            self._last_mode = work_mode
            self._last_transition_at = now
            # Only count cooling<->standby flaps as schedule-noise transitions.
            if work_mode in ("cooling", "standby"):
                self._transitions.append(now)

        # Evict old transitions outside the flap window.
        cutoff = now - timedelta(seconds=FLAP_WINDOW_S)
        while self._transitions and self._transitions[0] < cutoff:
            self._transitions.popleft()

    def is_schedule_active(self, now: datetime) -> bool:
        """Return True if the HP schedule appears to be allowing cooling.

        Default behavior is fail-open (True) so a sensor outage doesn't
        suppress legitimate cycles.
        """
        if self._last_transition_at is None:
            return True  # never observed anything; trust the system.

        # Recent stability — no flap in 2 min. Active.
        since_last = (now - self._last_transition_at).total_seconds()
        if since_last >= STABLE_WINDOW_S:
            return True

        # Active flapping → schedule probably denying cooling.
        rate_per_min = len(self._transitions) / max(FLAP_WINDOW_S / 60, 1)
        if rate_per_min > FLAP_RATE_PER_MIN:
            return False

        return True

    def stats(self) -> dict[str, float | int | str | None]:
        """Diagnostic snapshot for the binary_sensor attributes."""
        return {
            "transitions_5min": len(self._transitions),
            "last_mode": self._last_mode,
            "last_transition_at": self._last_transition_at.isoformat()
            if self._last_transition_at
            else None,
        }
