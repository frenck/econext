"""Pure state machine for the UFH cooling controller.

`tick()` is a pure function over (CoolingState, Sensors, Config, now,
tick_seconds) that returns a Decision. No HA imports. The coordinator
applies the Decision (writes HP target, persists state, etc.) and feeds
the next tick.

State graph:
    idle -> starting -> running -> cooldown -> idle

Bug fixes vs prototype:
1. HP-quiet timer uses `state.last_preempt_active_at` updated only when
   work_mode is in PREEMPT_MODES — fixes the cooling<->standby flapping
   that locked the controller out at night.
2. Idle gate also checks `sensors.schedule_active` (see schedule.py) —
   no cycle attempts during scheduled-off hours.
"""

from __future__ import annotations

from datetime import datetime

from .const import PREEMPT_MODES, SAFE_MODES, STATE_COOLDOWN, STATE_IDLE, STATE_RUNNING, STATE_STARTING
from .models import Config, CoolingState, Decision, Sensors, working_target
from .safety import SafetyVerdict, evaluate as evaluate_safety, reset_streaks


def tick(
    state: CoolingState,
    sensors: Sensors,
    config: Config,
    now: datetime,
    tick_seconds: int,
) -> Decision:
    """Run one tick of the cooling controller state machine."""

    if not config.enable:
        return Decision()

    # Update preempt-active tracker every tick. (Bug-fix 1.)
    if sensors.work_mode in PREEMPT_MODES:
        state.last_preempt_active_at = now

    # Mid-cycle preempt: HP started doing other work (DHW/heating/defrost).
    # Abandon the cycle and let the HP do its thing.
    if state.cycle_state in (STATE_STARTING, STATE_RUNNING, STATE_COOLDOWN):
        if sensors.work_mode not in (None, "unknown", "unavailable", *SAFE_MODES):
            return _finalize_decision(
                state,
                now,
                sensors.safe_target,
                f"HP preempted by work_mode={sensors.work_mode} during {state.cycle_state}; abandoning cycle",
                log_level="WARNING",
            )

    # Per-cycle running-with-comp-on tracking (drives tiered break).
    if state.cycle_state == STATE_RUNNING and sensors.comp_hz > 0:
        state.running_comp_on_seconds += tick_seconds

    # Safety check from any non-idle state (uses configured tier thresholds).
    if state.cycle_state in (STATE_STARTING, STATE_RUNNING):
        safety = evaluate_safety(state, sensors, config, tick_seconds)
        if safety.verdict == SafetyVerdict.PANIC:
            return _enter_cooldown(state, now, config, sensors, f"PANIC: {safety.reason}", "WARNING")
        if safety.verdict == SafetyVerdict.HARD:
            return _enter_cooldown(state, now, config, sensors, f"HARD SAFETY: {safety.reason}", "WARNING")
        if safety.verdict == SafetyVerdict.SOFT:
            return _enter_cooldown(state, now, config, sensors, f"stop: {safety.reason}", "INFO")

    # Dispatch to per-state handler.
    if state.cycle_state == STATE_IDLE:
        return _handle_idle(state, sensors, config, now)
    if state.cycle_state == STATE_STARTING:
        return _handle_starting(state, sensors, config, now)
    if state.cycle_state == STATE_RUNNING:
        return _handle_running(state, sensors, config, now)
    if state.cycle_state == STATE_COOLDOWN:
        return _handle_cooldown(state, sensors, config, now)

    return Decision(log_message=f"unknown state {state.cycle_state}, resetting to idle", log_level="WARNING", new_state=STATE_IDLE)


# ---------- state handlers ----------


def _handle_idle(state: CoolingState, sensors: Sensors, config: Config, now: datetime) -> Decision:
    reset_streaks(state)
    if not _gated_idle(sensors):
        return Decision()
    if sensors.safe_target > 27:
        return Decision()

    # Tiered break: full break_min after a real cycle, aborted_break after
    # a short/failed cycle.
    if state.last_cycle_end is not None:
        mins_since_last = (now - state.last_cycle_end).total_seconds() / 60
    else:
        mins_since_last = 1e9
    effective_break = (
        config.min_break_minutes
        if state.last_running_minutes >= config.real_cycle_threshold_minutes
        else config.aborted_break_minutes
    )
    if mins_since_last < effective_break:
        return Decision()

    # Cooling demand: only start when the HP's control-room thermostat is
    # above its setpoint by the start hysteresis. This mirrors the HP's own
    # demand condition — dipping the cooling-water target does nothing if
    # the thermostat is satisfied, so we must not attempt a start then.
    if sensors.thermostat_temp <= config.room_target + 0.5:
        return Decision()

    # Stale-loop guard: high flow temp means we're reading static loop
    # water left from DHW/heating, not slab return.
    if sensors.flow_hp > config.stale_loop_flow:
        return Decision()

    # HP quiet gate (bug-fix 1): require N min since HP last did real
    # non-cooling work. Doesn't reset on cooling<->standby flaps.
    if state.last_preempt_active_at is not None:
        quiet_min = (now - state.last_preempt_active_at).total_seconds() / 60
        if quiet_min < config.hp_quiet_minutes:
            return Decision()

    # Schedule gate (bug-fix 2): HP must be in a schedule-on window.
    if not sensors.schedule_active:
        return Decision()

    # Ret gate: ret must be high enough that a working_target cycle won't
    # self-stop immediately. Firmware stop at ret <= working-0.5.
    working = working_target(sensors.safe_target, config.working_target_offset, config.dip_target)
    if sensors.return_temp < working + 0.5:
        return Decision()

    state.running_comp_on_seconds = 0.0
    return Decision(
        new_state=STATE_STARTING,
        target_write=config.dip_target,
        log_message=(
            f"starting cycle: thermostat={sensors.thermostat_temp:.1f}, "
            f"ret={sensors.return_temp:.1f}, safe_target={sensors.safe_target}, "
            f"working={working}, mins_since_last={mins_since_last:.1f}, "
            f"effective_break={effective_break:.0f} (last_run={state.last_running_minutes:.1f})"
        ),
    )


def _handle_starting(state: CoolingState, sensors: Sensors, config: Config, now: datetime) -> Decision:
    if sensors.comp_hz > 0 and (sensors.return_temp - sensors.flow_hp) > 0.5:
        working = working_target(sensors.safe_target, config.working_target_offset, config.dip_target)
        return Decision(
            new_state=STATE_RUNNING,
            target_write=working,
            log_message=(
                f"cooling started: comp_hz={sensors.comp_hz:.0f}, "
                f"ret-flow={sensors.return_temp - sensors.flow_hp:.2f}, working_target={working}"
            ),
        )
    if state.state_entered is not None:
        mins_in_state = (now - state.state_entered).total_seconds() / 60
    else:
        mins_in_state = 1e9
    if mins_in_state >= config.start_timeout_minutes:
        return _finalize_decision(
            state,
            now,
            sensors.safe_target,
            f"start failed after {config.start_timeout_minutes:.0f} min; comp_hz={sensors.comp_hz:.0f}; backing off",
            log_level="WARNING",
        )
    return Decision()


def _handle_running(state: CoolingState, sensors: Sensors, config: Config, now: datetime) -> Decision:
    working = working_target(sensors.safe_target, config.working_target_offset, config.dip_target)
    decision = Decision()
    if abs(working - sensors.current_target) >= 1:
        decision.target_write = working
        decision.log_message = f"running: target {sensors.current_target:.0f} -> {working} (dp drift)"

    # Unexpected comp-off — HP stopped on its own.
    if sensors.comp_hz == 0:
        return _enter_cooldown(
            state,
            now,
            config,
            sensors,
            (
                f"HP stopped unexpectedly during running (working={working}, "
                f"ret={sensors.return_temp:.2f}); entering cooldown"
            ),
        )

    if state.state_entered is not None:
        mins_in_state = (now - state.state_entered).total_seconds() / 60
    else:
        mins_in_state = 0
    if mins_in_state >= config.max_run_minutes:
        return _enter_cooldown(state, now, config, sensors, f"stop: max_run {config.max_run_minutes:.0f} min reached")
    if sensors.thermostat_temp < config.room_target - 0.3:
        return _enter_cooldown(
            state,
            now,
            config,
            sensors,
            f"stop: thermostat={sensors.thermostat_temp:.1f} < target-0.3={config.room_target - 0.3:.1f}",
        )

    return decision


def _handle_cooldown(state: CoolingState, sensors: Sensors, config: Config, now: datetime) -> Decision:
    if sensors.comp_hz > 0:
        # Force target high until HP shuts down.
        if sensors.current_target < config.cooldown_target:
            return Decision(target_write=config.cooldown_target)
        return Decision()
    return _finalize_decision(
        state,
        now,
        sensors.safe_target,
        "cooldown complete: comp_hz=0",
    )


# ---------- helpers ----------


def _gated_idle(sensors: Sensors) -> bool:
    if not sensors.enable_switch_on:
        return False
    if not sensors.season_is_summer:
        return False
    if sensors.work_mode not in SAFE_MODES:
        return False
    return True


def _enter_cooldown(
    state: CoolingState,
    now: datetime,
    config: Config,
    sensors: Sensors,
    log_message: str,
    log_level: str = "INFO",
) -> Decision:
    reset_streaks(state)
    return Decision(
        new_state=STATE_COOLDOWN,
        target_write=config.cooldown_target,
        log_message=log_message,
        log_level=log_level,
    )


def _finalize_decision(
    state: CoolingState,
    now: datetime,
    safe_target: int,
    log_message: str,
    log_level: str = "INFO",
) -> Decision:
    """End-of-cycle: cooldown -> idle, plus the timeout-from-starting path
    and the preempt-abandon path. Sets finalize=True so the coordinator
    persists last_cycle_end and last_running_minutes."""
    return Decision(
        new_state=STATE_IDLE,
        target_write=safe_target,
        finalize=True,
        log_message=log_message,
        log_level=log_level,
    )
