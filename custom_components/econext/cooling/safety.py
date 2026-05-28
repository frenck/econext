"""Three-tier dew-safety policy.

Pure function over (state, sensors, config) — no HA imports — so the
safety logic is testable in isolation.

Tiers:
  - Panic (instant): flow_below_dew < -panic_floor. Sensor glitch /
    runaway. Single tick = stop.
  - Hard floor (sustained): flow_below_dew < -hard_floor for
    hard_floor_duration_s. Filters transient dew spikes (cooking) that
    recover within minutes.
  - Soft cutoff (sustained): flow_below_dew < -flow_below_dew_cutoff for
    flow_below_dew_min_duration_s. The intended normal stop condition.

`SafetyVerdict.NONE` means OK to continue. Counters live in CoolingState
so they can persist across ticks; this function mutates them.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .models import Config, CoolingState, Sensors


class SafetyVerdict(Enum):
    NONE = "none"  # safe to continue
    SOFT = "soft"  # soft cutoff fired (preferred normal stop)
    HARD = "hard"  # hard floor fired (transient dew spike)
    PANIC = "panic"  # instant — something is very wrong


@dataclass
class SafetyResult:
    verdict: SafetyVerdict
    reason: str = ""


def evaluate(
    state: CoolingState, sensors: Sensors, config: Config, tick_seconds: int
) -> SafetyResult:
    """Evaluate safety; mutates streak counters on `state`."""

    fbd = sensors.flow_below_dew

    # Panic floor — instant, regardless of streak.
    if fbd < -config.panic_floor:
        return SafetyResult(
            SafetyVerdict.PANIC,
            f"flow_below_dew={fbd:.2f} < -{config.panic_floor}",
        )

    # Hard floor — sustain to filter transient spikes.
    if fbd < -config.hard_floor:
        state.hard_floor_streak_s += tick_seconds
        if state.hard_floor_streak_s >= config.hard_floor_duration_s:
            return SafetyResult(
                SafetyVerdict.HARD,
                (
                    f"flow_below_dew={fbd:.2f} sustained "
                    f"{state.hard_floor_streak_s:.0f}s < -{config.hard_floor}"
                ),
            )
    else:
        state.hard_floor_streak_s = 0.0

    # Soft cutoff — preferred stop.
    if fbd < -config.flow_below_dew_cutoff:
        state.below_dew_streak_s += tick_seconds
        if state.below_dew_streak_s >= config.flow_below_dew_min_duration_s:
            return SafetyResult(
                SafetyVerdict.SOFT,
                (
                    f"flow_below_dew={fbd:.2f} sustained "
                    f"{state.below_dew_streak_s:.0f}s < -{config.flow_below_dew_cutoff}"
                ),
            )
    else:
        state.below_dew_streak_s = 0.0

    return SafetyResult(SafetyVerdict.NONE)


def reset_streaks(state: CoolingState) -> None:
    """Called when entering cooldown — drops both streak counters."""
    state.below_dew_streak_s = 0.0
    state.hard_floor_streak_s = 0.0
