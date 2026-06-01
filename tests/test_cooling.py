"""Tests for the pure cooling-controller state machine."""

from datetime import datetime, timedelta

import pytest

from custom_components.econext.cooling.const import (
    DEFAULTS,
    STATE_COOLDOWN,
    STATE_IDLE,
    STATE_RUNNING,
    STATE_STARTING,
)
from custom_components.econext.cooling.models import Config, CoolingState, Sensors
from custom_components.econext.cooling.safety import SafetyVerdict, evaluate
from custom_components.econext.cooling.state_machine import tick

TICK = 10


def _config(**overrides) -> Config:
    data = dict(DEFAULTS)
    data.update(overrides)
    return Config(**data)


def _sensors(**overrides) -> Sensors:
    base = dict(
        work_mode="standby",
        comp_hz=0.0,
        flow_hp=25.0,
        flow_oem=25.0,
        return_temp=24.0,
        current_target=22.0,
        dew_max=14.0,
        thermostat_temp=24.0,
        enable_switch_on=True,
        season_is_summer=True,
        schedule_active=True,
        safe_target=18,
        flow_below_dew=11.0,
    )
    base.update(overrides)
    return Sensors(**base)


def _apply(state: CoolingState, decision, now: datetime) -> None:
    """Mimic the coordinator's state bookkeeping after a tick."""
    if decision.new_state is not None and decision.new_state != state.cycle_state:
        state.cycle_state = decision.new_state
        state.state_entered = now
    if decision.finalize:
        state.last_running_minutes = round(state.running_comp_on_seconds / 60.0, 1)
        state.running_comp_on_seconds = 0.0
        state.last_cycle_end = now


def test_full_cycle_idle_to_idle():
    """idle -> starting -> running -> cooldown -> idle, with the right targets."""
    config = _config()
    state = CoolingState()
    now = datetime(2026, 6, 1, 12, 0, 0)

    # idle -> starting: demand present, all gates pass.
    d = tick(state, _sensors(), config, now, TICK)
    assert d.new_state == STATE_STARTING
    assert d.target_write == config.dip_target  # dip the target to force a start
    _apply(state, d, now)

    # starting -> running: compressor on and return warmer than flow.
    now += timedelta(seconds=30)
    d = tick(
        state,
        _sensors(work_mode="cooling", comp_hz=30.0, flow_hp=14.0, return_temp=20.0, current_target=15.0),
        config,
        now,
        TICK,
    )
    assert d.new_state == STATE_RUNNING
    # working_target = max(safe_target - offset, dip_target) = max(14, 15) = 15
    assert d.target_write == 15
    _apply(state, d, now)

    # running stays running while demand persists; comp-on time accrues.
    now += timedelta(seconds=60)
    d = tick(
        state,
        _sensors(work_mode="cooling", comp_hz=30.0, flow_hp=14.0, return_temp=20.0, current_target=15.0),
        config,
        now,
        TICK,
    )
    assert d.new_state is None
    assert state.running_comp_on_seconds == TICK
    _apply(state, d, now)

    # running -> cooldown: thermostat satisfied below setpoint hysteresis.
    now += timedelta(seconds=60)
    d = tick(
        state,
        _sensors(work_mode="cooling", comp_hz=30.0, flow_hp=14.0, return_temp=20.0, current_target=15.0, thermostat_temp=21.0),
        config,
        now,
        TICK,
    )
    assert d.new_state == STATE_COOLDOWN
    assert d.target_write == config.cooldown_target
    _apply(state, d, now)

    # cooldown holds while compressor still running.
    now += timedelta(seconds=30)
    d = tick(
        state,
        _sensors(work_mode="cooling", comp_hz=20.0, current_target=25.0),
        config,
        now,
        TICK,
    )
    assert d.new_state is None
    _apply(state, d, now)

    # cooldown -> idle once compressor stops; target restored to safe_target.
    now += timedelta(seconds=30)
    d = tick(
        state,
        _sensors(work_mode="cooling", comp_hz=0.0, current_target=25.0),
        config,
        now,
        TICK,
    )
    assert d.new_state == STATE_IDLE
    assert d.finalize is True
    assert d.target_write == 18  # safe_target
    _apply(state, d, now)
    assert state.cycle_state == STATE_IDLE
    assert state.last_cycle_end == now


def test_disabled_does_nothing():
    state = CoolingState()
    d = tick(state, _sensors(), _config(enable=False), datetime(2026, 6, 1), TICK)
    assert d.new_state is None
    assert d.target_write is None


def test_no_demand_stays_idle():
    state = CoolingState()
    # thermostat at/below setpoint+0.5 -> no demand -> no start.
    d = tick(state, _sensors(thermostat_temp=22.0), _config(), datetime(2026, 6, 1), TICK)
    assert d.new_state is None


def test_mid_cycle_preempt_abandons():
    state = CoolingState(cycle_state=STATE_RUNNING, state_entered=datetime(2026, 6, 1, 12, 0, 0))
    d = tick(
        state,
        _sensors(work_mode="heating", comp_hz=30.0),
        _config(),
        datetime(2026, 6, 1, 12, 5, 0),
        TICK,
    )
    assert d.new_state == STATE_IDLE
    assert d.finalize is True


@pytest.mark.parametrize(
    ("flow_below_dew", "expected"),
    [(-7.0, SafetyVerdict.PANIC), (5.0, SafetyVerdict.NONE)],
)
def test_safety_panic_and_safe(flow_below_dew, expected):
    state = CoolingState()
    result = evaluate(state, _sensors(flow_below_dew=flow_below_dew), _config(), TICK)
    assert result.verdict == expected


def test_safety_soft_cutoff_sustained():
    config = _config(flow_below_dew_cutoff=3.0, flow_below_dew_min_duration_s=30)
    state = CoolingState()
    sensors = _sensors(flow_below_dew=-4.0)
    # Needs to be sustained for the configured duration before firing.
    assert evaluate(state, sensors, config, TICK).verdict == SafetyVerdict.NONE
    assert evaluate(state, sensors, config, TICK).verdict == SafetyVerdict.NONE
    assert evaluate(state, sensors, config, TICK).verdict == SafetyVerdict.SOFT
