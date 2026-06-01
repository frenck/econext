"""Dataclasses for the cooling controller — pure data, no HA imports.

The state machine in `state_machine.py` is a pure function over these
dataclasses, which makes it unit-testable without an HA runtime.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass
class Sensors:
    """Snapshot of all HA-side sensor readings consumed by one tick."""

    # HP-side
    work_mode: Optional[str]
    comp_hz: float
    flow_hp: float  # HP-internal flow temp (used for ret-flow start detection)
    flow_oem: float  # OEM/manifold flow temp (used for dew safety)
    return_temp: float
    current_target: float

    # Environment
    dew_max: float
    thermostat_temp: float  # the UFH/ecoMAX control-room thermostat temp;
    # drives cooling demand (mirrors what the HP actually responds to)

    # Gating
    enable_switch_on: bool
    season_is_summer: bool

    # Schedule gate — true when HP schedule allows cooling.
    # See schedule.py for derivation.
    schedule_active: bool

    # Computed
    safe_target: int  # ceil(dp + 1 + delta_t_est + safety_margin), clamped 16-27
    flow_below_dew: float  # flow_oem - dew_max


@dataclass
class Config:
    """All tunables read each tick from HA entities."""

    safety_margin: float
    delta_t_est: float
    max_run_minutes: float
    min_break_minutes: float
    aborted_break_minutes: float
    real_cycle_threshold_minutes: float
    flow_below_dew_cutoff: float
    flow_below_dew_min_duration_s: float
    room_target: float
    working_target_offset: float
    stale_loop_flow: float
    hp_quiet_minutes: float
    hard_floor: float
    hard_floor_duration_s: float
    panic_floor: float
    start_timeout_minutes: float
    dip_target: float
    cooldown_target: float
    dry_run: bool
    enable: bool


@dataclass
class CoolingState:
    """Persistent state across ticks. Owned by the coordinator; persisted
    via RestoreEntity on key state changes.
    """

    cycle_state: str = "idle"
    state_entered: Optional[datetime] = None
    last_cycle_end: Optional[datetime] = None
    last_running_minutes: float = 0.0
    running_comp_on_seconds: float = 0.0
    below_dew_streak_s: float = 0.0
    hard_floor_streak_s: float = 0.0
    # Bug-fix 1: tracked separately from work_mode.last_changed so idle
    # cooling<->standby flaps don't reset the HP-quiet timer.
    last_preempt_active_at: Optional[datetime] = None
    last_target_written: Optional[float] = None


@dataclass
class Decision:
    """What the state machine wants the coordinator to do this tick.

    `new_state` is `None` if the cycle phase didn't change. `target_write`
    is the HP target temp to write (skipped if equal to last_target_written
    by debounce). `finalize` triggers end-of-cycle bookkeeping (stamps
    last_cycle_end, commits last_running_minutes, resets streaks).
    `log_level` of "INFO"/"WARNING" controls how the coordinator surfaces
    the log message.
    """

    new_state: Optional[str] = None
    target_write: Optional[float] = None
    finalize: bool = False
    log_message: Optional[str] = None
    log_level: str = "INFO"


def working_target(safe_target: int, offset: float, dip_target: float) -> int:
    """Operating target during running. Lower than safe_target so HP keeps
    cooling instead of stopping at the safe_target threshold. Floored at
    dip_target to avoid asking below Circuit2MinSetPointCooling.
    """
    return max(int(safe_target - offset), int(dip_target))
