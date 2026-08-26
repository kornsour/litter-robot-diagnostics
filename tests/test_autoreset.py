import asyncio
from pathlib import Path
from typing import Any

import pytest

from lr4_diagnostics.autoreset import (
    CYCLE_OVERRUN,
    CYCLE_STALL,
    IDLE_LATCH,
    Assessment,
    AutoResetConfig,
    Observation,
    RecoveryPolicy,
    Watchdog,
    _record,
    _recover,
    _refuse_unsafe_probe,
    log_stuck,
    probe_reset_recovery,
)
from lr4_diagnostics.lambda_handler import _DynamoInterventionStore
from lr4_diagnostics.store import EventStore

#: The shipped configuration, so the gates are exercised as they actually run.
POLICY = RecoveryPolicy()

HEALTHY: dict[str, Any] = {
    "robotStatus": "ROBOT_IDLE",
    "displayCode": "DC_MODE_IDLE",
    "robotCycleStatus": "CYCLE_IDLE",
    "litterLevel": 454.0,
    "isBonnetRemoved": False,
}
LATCHED: dict[str, Any] = {
    "robotStatus": "ROBOT_CAT_DETECT_DELAY",
    "displayCode": "DC_CAT_DETECT_30M",
    "robotCycleStatus": "CYCLE_IDLE",
    "litterLevel": 452.0,
    "isBonnetRemoved": False,
}
DUMPING: dict[str, Any] = {
    "robotStatus": "ROBOT_CLEAN",
    "displayCode": "DC_MODE_CYCLE",
    "robotCycleStatus": "CYCLE_DUMP",
    "robotCycleState": "CYCLE_STATE_CAT_DETECT",
    "litterLevel": 453.0,
    "isBonnetRemoved": False,
}
#: What a Reset on a stalled cycle actually produces, captured from the unit at
#: 2026-07-30T15:22:48Z. Note `robotCycleStatus` still reads CYCLE_DUMP: the
#: pause shows up only in `robotCycleState` and `displayCode`.
PAUSED_MID_DUMP: dict[str, Any] = {
    "robotStatus": "ROBOT_CLEAN",
    "displayCode": "DC_USER_PAUSE",
    "robotCycleStatus": "CYCLE_DUMP",
    "robotCycleState": "CYCLE_STATE_PAUSE",
    "litterLevel": 453.0,
    "isBonnetRemoved": False,
}
#: A globe away from home that is neither cycling nor paused -- no Reset toggle
#: applies, so the only lever left is a Cycle press.
STALLED_AFTER_RESET: dict[str, Any] = {
    "robotStatus": "ROBOT_CLEAN",
    "displayCode": "DC_MODE_CYCLE",
    "robotCycleStatus": "CYCLE_IDLE",
    "litterLevel": 453.0,
    "isBonnetRemoved": False,
}
#: A cleaning session on this unit: the bonnet sensor trips and the status
#: reads exactly like an idle latch. Taken from a real capture on 2026-07-26.
SERVICING: dict[str, Any] = {
    "robotStatus": "ROBOT_CAT_DETECT_DELAY",
    "displayCode": "DC_BONNET_OFF",
    "robotCycleStatus": "CYCLE_IDLE",
    "litterLevel": 453.0,
    "isBonnetRemoved": True,
}


def observe(at: float, payload: dict[str, Any]) -> Observation:
    return Observation.from_state(at, payload)


class FakeRobot:
    serial = "LR4C-SECRET-SERIAL"


class FakeDynamoTable:
    def __init__(self) -> None:
        self.item: dict[str, Any] | None = None

    def put_item(self, *, Item: dict[str, Any]) -> None:  # noqa: N803 - AWS API spelling
        self.item = Item


def test_state_payload_maps_onto_an_observation() -> None:
    observation = observe(0.0, LATCHED)
    assert observation.robot_status == "ROBOT_CAT_DETECT_DELAY"
    assert observation.display_code == "DC_CAT_DETECT_30M"
    assert observation.litter_level_mm == 452.0
    assert observation.is_latched
    assert not observation.is_cycling
    assert not observation.is_healthy


def test_dynamo_intervention_store_serializes_floats_as_decimals() -> None:
    table = FakeDynamoTable()
    store = _DynamoInterventionStore(table, "robot")

    store.add(
        observed_at="2026-07-30T00:00:00+00:00",
        source="intervention",
        robot_id="ignored",
        payload={"assessment": {"stuck_for_seconds": 900.5}},
    )

    assert table.item is not None
    assert str(table.item["payload"]["assessment"]["stuck_for_seconds"]) == "900.5"


def test_healthy_unit_is_never_stuck() -> None:
    watchdog = Watchdog(POLICY)
    assert observe(0.0, HEALTHY).is_healthy
    assert not watchdog.assess(observe(10_000.0, HEALTHY)).stuck


def test_latch_must_persist_past_the_grace_period() -> None:
    watchdog = Watchdog(POLICY)
    watchdog.assess(observe(0.0, LATCHED))
    assert not watchdog.assess(observe(899.0, LATCHED)).stuck

    assessment = watchdog.assess(observe(901.0, LATCHED))
    assert assessment.reason == IDLE_LATCH
    assert assessment.should_act
    assert assessment.blocked_by is None


def test_a_latch_that_self_resolves_restarts_the_clock() -> None:
    """Latches self-resolve routinely; a later one must serve its own grace."""
    watchdog = Watchdog(POLICY)
    watchdog.assess(observe(0.0, LATCHED))
    watchdog.assess(observe(500.0, HEALTHY))
    watchdog.assess(observe(600.0, LATCHED))
    assert not watchdog.assess(observe(1_400.0, LATCHED)).stuck
    assert watchdog.assess(observe(1_501.0, LATCHED)).stuck


def test_a_cycle_stalled_on_one_state_is_detected() -> None:
    watchdog = Watchdog(POLICY)
    watchdog.assess(observe(0.0, DUMPING))
    assert not watchdog.assess(observe(299.0, DUMPING)).stuck

    assessment = watchdog.assess(observe(301.0, DUMPING))
    assert assessment.reason == CYCLE_STALL
    assert assessment.should_act


def test_a_stall_is_acted_on_like_any_other_stuck_state() -> None:
    watchdog = Watchdog(POLICY)
    watchdog.assess(observe(0.0, DUMPING))
    assert watchdog.assess(observe(301.0, DUMPING)).should_act


def test_a_stuck_unit_alarms_even_when_the_watchdog_will_not_act(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The alarm is the whole deliverable for stalls, so it cannot ride on dispatch."""
    watchdog = Watchdog(POLICY)
    watchdog.assess(observe(0.0, DUMPING))
    watchdog.note_cat_activity(at=300.0)
    assessment = watchdog.assess(observe(301.0, DUMPING))
    assert not assessment.should_act

    with caplog.at_level("INFO"):
        log_stuck(assessment, POLICY)

    assert "WATCHDOG_STUCK" in caplog.text
    assert "cycle-stall" in caplog.text


def test_a_cycle_that_keeps_advancing_is_not_stalled() -> None:
    watchdog = Watchdog(POLICY)
    watchdog.assess(observe(0.0, DUMPING))
    watchdog.assess(observe(250.0, DUMPING))
    levelling = {**DUMPING, "robotCycleStatus": "CYCLE_LEVEL"}
    watchdog.assess(observe(300.0, levelling))
    assert not watchdog.assess(observe(550.0, levelling)).stuck


def test_a_cycle_that_never_finishes_trips_the_overall_ceiling() -> None:
    """State churn resets the stall timer, so retries need their own ceiling."""
    watchdog = Watchdog(POLICY)
    states = ["CYCLE_STATE_PROCESS", "CYCLE_STATE_CAT_DETECT", "CYCLE_STATE_WAIT_ON"]
    at = 0.0
    while at < 580.0:
        churning = {**DUMPING, "robotCycleState": states[int(at / 60) % len(states)]}
        assert not watchdog.assess(observe(at, churning)).stuck
        at += 60.0

    assessment = watchdog.assess(observe(620.0, DUMPING))
    assert assessment.reason == CYCLE_OVERRUN
    assert assessment.should_act


def test_a_cycle_that_finishes_resets_the_ceiling() -> None:
    watchdog = Watchdog(POLICY)
    watchdog.assess(observe(0.0, DUMPING))
    watchdog.assess(observe(500.0, HEALTHY))
    watchdog.assess(observe(510.0, DUMPING))
    assert not watchdog.assess(observe(700.0, DUMPING)).stuck


def test_an_attempt_restarts_the_stuck_timers() -> None:
    """Timers must not measure across an intervention that moved the unit."""
    watchdog = Watchdog(POLICY)
    watchdog.assess(observe(0.0, LATCHED))
    assert watchdog.assess(observe(1_000.0, LATCHED)).should_act

    watchdog.note_attempt(dispatched=False, at=1_000.0)
    watchdog.assess(observe(1_010.0, LATCHED))
    # Cooldown aside, the latch itself is only 10s old again, not 1010s.
    assert not watchdog.assess(observe(1_900.0, LATCHED)).stuck
    assert watchdog.assess(observe(2_000.0, LATCHED)).should_act


def test_recent_cat_activity_blocks_intervention() -> None:
    watchdog = Watchdog(POLICY)
    watchdog.assess(observe(0.0, LATCHED))
    watchdog.note_cat_activity(at=800.0)

    assessment = watchdog.assess(observe(1_000.0, LATCHED))
    assert assessment.stuck
    assert not assessment.should_act
    assert "cat activity" in (assessment.blocked_by or "")

    assert watchdog.assess(observe(1_500.0, LATCHED)).should_act


def test_the_quiet_period_measures_from_the_newest_marker() -> None:
    """Activity rows arrive newest-first; the oldest must not win."""
    watchdog = Watchdog(POLICY)
    watchdog.assess(observe(0.0, LATCHED))
    for marker in (950.0, 500.0, 100.0):  # as the stream orders them
        watchdog.note_cat_activity(at=marker)

    assessment = watchdog.assess(observe(1_000.0, LATCHED))
    assert not assessment.should_act
    assert "cat activity 50s ago" in (assessment.blocked_by or "")


def test_cooldown_blocks_back_to_back_attempts() -> None:
    """A latch that re-forms quickly still has to wait out the cooldown."""
    watchdog = Watchdog(RecoveryPolicy(latch_grace=60.0, cooldown=900.0))
    watchdog.note_attempt(dispatched=False, at=1_000.0)
    watchdog.assess(observe(1_010.0, LATCHED))

    blocked = watchdog.assess(observe(1_100.0, LATCHED))
    assert blocked.stuck
    assert "cooldown" in (blocked.blocked_by or "")
    assert watchdog.assess(observe(1_950.0, LATCHED)).should_act


def test_hourly_rate_limit_stops_a_runaway_loop() -> None:
    watchdog = Watchdog(RecoveryPolicy(latch_grace=60.0, max_per_hour=4))
    for index in range(4):
        watchdog.note_attempt(dispatched=False, at=float(index))
    watchdog.assess(observe(1_000.0, LATCHED))

    blocked = watchdog.assess(observe(1_100.0, LATCHED))
    assert "rate limit" in (blocked.blocked_by or "")
    # The window rolls: an hour after the first attempt, capacity returns.
    assert watchdog.assess(observe(3_700.0, LATCHED)).should_act


def test_the_rate_limit_must_be_tighter_than_the_cooldown_already_is() -> None:
    """A limit the cooldown alone satisfies is not a second gate at all."""
    with pytest.raises(ValueError, match="max per hour"):
        RecoveryPolicy(cooldown=900.0, max_per_hour=8).validate()


def test_a_fault_that_re_forms_counts_the_recovery_as_failed() -> None:
    """Reaching home is not recovery if the scale re-latches minutes later."""
    policy = RecoveryPolicy(latch_grace=900.0, cooldown=900.0, max_per_hour=2)
    watchdog = Watchdog(policy)

    watchdog.note_attempt(dispatched=True, at=0.0)
    # The unit does park at home, exactly as the in-attempt check would see.
    watchdog.assess(observe(100.0, HEALTHY))
    assert watchdog.consecutive_failures == 0

    # ...and then the latch comes back and ripens again.
    watchdog.assess(observe(200.0, LATCHED))
    watchdog.assess(observe(1_200.0, LATCHED))
    assert watchdog.consecutive_failures == 1


def test_repeated_failures_escalate_and_stand_the_watchdog_down() -> None:
    watchdog = Watchdog(POLICY)
    for index in range(POLICY.max_consecutive_failures):
        watchdog.note_attempt(dispatched=True, at=float(index) * 10_000.0)
        watchdog.note_recovery_failed()
    assert watchdog.escalated
    assert watchdog.consecutive_failures == 3

    watchdog.assess(observe(90_000.0, LATCHED))
    blocked = watchdog.assess(observe(100_000.0, LATCHED))
    assert not blocked.should_act
    assert "escalated" in (blocked.blocked_by or "")


def test_a_recovery_that_holds_clears_the_failure_counter() -> None:
    """Only staying healthy past `recovery_hold` counts as a real recovery."""
    policy = RecoveryPolicy(recovery_hold=1_800.0)
    watchdog = Watchdog(policy)
    watchdog.note_attempt(dispatched=True, at=0.0)
    watchdog.note_recovery_failed()
    watchdog.note_attempt(dispatched=True, at=10_000.0)

    # Home again, but not yet long enough to prove anything.
    watchdog.assess(observe(11_000.0, HEALTHY))
    assert watchdog.consecutive_failures == 1

    watchdog.assess(observe(11_900.0, HEALTHY))
    assert watchdog.consecutive_failures == 0
    assert not watchdog.escalated


def test_an_unarmed_attempt_leaves_no_verdict_to_judge() -> None:
    """The dry run must not escalate a watchdog that never sent a command."""
    watchdog = Watchdog(POLICY)
    watchdog.note_attempt(dispatched=False, at=0.0)
    watchdog.assess(observe(100.0, LATCHED))
    watchdog.assess(observe(1_100.0, LATCHED))
    assert watchdog.consecutive_failures == 0


def test_snapshot_restores_all_safety_timers_for_a_scheduled_watch() -> None:
    policy = RecoveryPolicy(latch_grace=60.0, cooldown=900.0)
    watchdog = Watchdog(policy)
    watchdog.assess(observe(0.0, LATCHED))
    watchdog.note_attempt(dispatched=True, at=0.0)
    watchdog.note_recovery_failed()
    watchdog.assess(observe(10.0, LATCHED))

    restored = Watchdog(policy)
    restored.restore(watchdog.snapshot())

    assert restored.consecutive_failures == 1
    blocked = restored.assess(observe(100.0, LATCHED))
    assert "cooldown" in (blocked.blocked_by or "")


def test_an_object_in_the_globe_blocks_resetting_an_idle_latch() -> None:
    watchdog = Watchdog(POLICY)
    occupied = {**LATCHED, "litterLevel": 120.0}
    watchdog.assess(observe(0.0, occupied))

    assessment = watchdog.assess(observe(1_000.0, occupied))
    assert assessment.stuck
    assert not assessment.should_act
    assert "clear floor" in (assessment.blocked_by or "")


def test_a_missing_tof_reading_blocks_rather_than_defaults_open() -> None:
    watchdog = Watchdog(POLICY)
    unknown = {key: value for key, value in LATCHED.items() if key != "litterLevel"}
    watchdog.assess(observe(0.0, unknown))
    assert "unavailable" in (watchdog.assess(observe(1_000.0, unknown)).blocked_by or "")


def test_the_tof_gate_is_skipped_mid_cycle_because_the_field_is_frozen() -> None:
    """`litterLevel` holds its last at-rest value while the globe turns."""
    watchdog = Watchdog(POLICY)
    stalled = {**DUMPING, "litterLevel": 120.0}
    watchdog.assess(observe(0.0, stalled))

    assessment = watchdog.assess(observe(400.0, stalled))
    assert assessment.reason == CYCLE_STALL
    assert assessment.should_act


def test_a_removed_bonnet_blocks_an_idle_latch() -> None:
    """A cleaning session reports the latch status; a person is inside it."""
    watchdog = Watchdog(POLICY)
    watchdog.assess(observe(0.0, SERVICING))

    assessment = watchdog.assess(observe(1_000.0, SERVICING))
    assert assessment.stuck
    assert assessment.reason == IDLE_LATCH
    assert not assessment.should_act
    assert "bonnet removed" in (assessment.blocked_by or "")


def test_a_removed_bonnet_also_blocks_a_cycle_stall() -> None:
    """The gate guards a person at the unit, so no reason may bypass it."""
    watchdog = Watchdog(POLICY)
    stalled_open = {**DUMPING, "isBonnetRemoved": True}
    watchdog.assess(observe(0.0, stalled_open))

    assessment = watchdog.assess(observe(400.0, stalled_open))
    assert assessment.reason == CYCLE_STALL
    assert not assessment.should_act
    assert "bonnet removed" in (assessment.blocked_by or "")


def test_a_missing_bonnet_reading_blocks_rather_than_defaults_open() -> None:
    watchdog = Watchdog(POLICY)
    unknown = {key: value for key, value in LATCHED.items() if key != "isBonnetRemoved"}
    watchdog.assess(observe(0.0, unknown))

    assessment = watchdog.assess(observe(1_000.0, unknown))
    assert assessment.stuck
    assert not assessment.should_act
    assert "bonnet state unavailable" in (assessment.blocked_by or "")


def test_reseating_the_bonnet_restarts_the_latch_grace() -> None:
    """Time the unit spent open measured a person, not a ripening fault."""
    watchdog = Watchdog(POLICY)
    watchdog.assess(observe(0.0, SERVICING))
    watchdog.assess(observe(1_000.0, SERVICING))

    # Bonnet back on, unit still showing the latch status: the grace period
    # must start over rather than fire immediately on the reseat.
    assert not watchdog.assess(observe(1_010.0, LATCHED)).stuck
    assert not watchdog.assess(observe(1_800.0, LATCHED)).stuck
    assert watchdog.assess(observe(1_911.0, LATCHED)).should_act


def test_snapshot_carries_bonnet_state_so_a_scheduled_watch_can_accumulate() -> None:
    """Losing this across invocations would wipe the timers once a minute."""
    policy = RecoveryPolicy(latch_grace=900.0)
    watchdog = Watchdog(policy)
    watchdog.assess(observe(0.0, LATCHED))

    restored = Watchdog(policy)
    restored.restore(watchdog.snapshot())

    assessment = restored.assess(observe(901.0, LATCHED))
    assert assessment.reason == IDLE_LATCH
    assert assessment.should_act


def test_snapshot_carries_a_pending_verdict_across_invocations() -> None:
    """Dropping it would silently forgive every failed recovery in Lambda."""
    policy = RecoveryPolicy(latch_grace=900.0)
    watchdog = Watchdog(policy)
    watchdog.note_attempt(dispatched=True, at=0.0)

    restored = Watchdog(policy)
    restored.restore(watchdog.snapshot())

    restored.assess(observe(100.0, LATCHED))
    restored.assess(observe(1_100.0, LATCHED))
    assert restored.consecutive_failures == 1


def test_a_stall_is_caught_sooner_than_a_latch() -> None:
    """The stall is the urgent mode: the globe is parked away from home."""
    policy = RecoveryPolicy()
    assert policy.stall_grace < policy.latch_grace

    watchdog = Watchdog(policy)
    watchdog.assess(observe(0.0, DUMPING))
    assert watchdog.assess(observe(policy.stall_grace + 1.0, DUMPING)).reason == CYCLE_STALL


class FakeCommandRobot:
    """Records which device commands a recovery actually dispatches."""

    serial = "LR4C-SECRET-SERIAL"

    def __init__(self, after_reset: dict[str, Any]) -> None:
        self.after_reset = after_reset
        self.commands: list[str] = []

    async def reset(self) -> bool:
        self.commands.append("reset")
        return True

    async def start_cleaning(self) -> bool:
        self.commands.append("clean_cycle")
        return True

    async def refresh(self) -> None:
        return None

    def to_dict(self) -> dict[str, Any]:
        return self.after_reset


class FakeStore:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def add(self, **kwargs: Any) -> bool:
        self.records.append(kwargs)
        return True


def run_recovery(reason: str, after_reset: dict[str, Any]) -> list[str]:
    policy = RecoveryPolicy(armed=True, settle=0.0, verify_timeout=0.0)
    robot = FakeCommandRobot(after_reset)
    assessment = Assessment(stuck=True, reason=reason, stuck_for=1_000.0, should_act=True)
    asyncio.run(
        _recover(
            robot,  # type: ignore[arg-type] - only the command surface is used
            observe(0.0, LATCHED if reason == IDLE_LATCH else DUMPING),
            assessment,
            FakeStore(),
            policy,
        )
    )
    return robot.commands


def test_a_stall_that_the_reset_brought_home_needs_no_cycle_press() -> None:
    """No point re-running the rotation that just stalled if it is already home."""
    assert run_recovery(CYCLE_STALL, HEALTHY) == ["reset"]


def test_a_globe_stranded_away_from_home_gets_a_cycle_press() -> None:
    """Neither cycling nor paused, so no Reset toggle applies."""
    assert run_recovery(CYCLE_STALL, STALLED_AFTER_RESET) == ["reset", "clean_cycle"]


def test_a_stalled_cycle_takes_two_resets_not_a_cycle_press() -> None:
    """Reset toggles: the first breaks the stall into a pause, the second resumes.

    Measured 2026-07-30. `cleanCycle` is inert on a paused unit -- it was
    dispatched and ignored for five minutes -- so sending it here would strand
    the globe mid-dump with the box unusable.
    """
    observation = observe(0.0, PAUSED_MID_DUMP)
    assert observation.is_cycling  # the trap: a pause still reports its phase
    assert observation.is_paused
    assert not observation.is_healthy

    assert run_recovery(CYCLE_STALL, PAUSED_MID_DUMP) == ["reset", "reset"]


def test_reset_presses_are_capped_so_the_loop_cannot_toggle_forever() -> None:
    """A third press would re-pause the cycle the second one just resumed."""
    assert run_recovery(CYCLE_STALL, PAUSED_MID_DUMP).count("reset") == 2


def test_a_paused_globe_is_still_caught_as_a_stall() -> None:
    """`is_paused` must not exempt it from detection, only change the remedy."""
    watchdog = Watchdog(POLICY)
    watchdog.assess(observe(0.0, PAUSED_MID_DUMP))
    assert watchdog.assess(observe(301.0, PAUSED_MID_DUMP)).reason == CYCLE_STALL


def test_an_idle_latch_runs_the_cleaning_it_was_refusing_to_do() -> None:
    """Here the globe is already home; the cycle it owed is what was missing."""
    assert run_recovery(IDLE_LATCH, HEALTHY) == ["reset", "clean_cycle"]


def test_a_cycle_already_running_is_never_given_a_second_one() -> None:
    assert run_recovery(IDLE_LATCH, DUMPING) == ["reset"]
    assert run_recovery(CYCLE_STALL, DUMPING) == ["reset"]


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({**HEALTHY, "isBonnetRemoved": True}, "bonnet"),
        (LATCHED, "not idle at home"),
        (DUMPING, "not idle at home"),
        ({**HEALTHY, "litterLevel": 120.0}, "may be in the globe"),
        ({key: v for key, v in HEALTHY.items() if key != "litterLevel"}, "may be in the globe"),
    ],
)
def test_the_probe_refuses_an_unsafe_starting_state(payload: dict[str, Any], message: str) -> None:
    """It rotates the globe on purpose, so the pre-flight state must be known."""
    with pytest.raises(RuntimeError, match=message):
        _refuse_unsafe_probe(observe(0.0, payload), RecoveryPolicy())


def test_the_probe_accepts_a_clean_starting_state() -> None:
    _refuse_unsafe_probe(observe(0.0, HEALTHY), RecoveryPolicy())


def test_the_probe_refuses_to_run_unarmed() -> None:
    config = AutoResetConfig(database=Path("unused.sqlite"), policy=RecoveryPolicy(armed=False))
    with pytest.raises(RuntimeError, match="--arm"):
        asyncio.run(probe_reset_recovery("someone@example.com", config))


def test_the_tof_gate_can_be_disabled() -> None:
    watchdog = Watchdog(RecoveryPolicy(require_clear_tof=False))
    occupied = {**LATCHED, "litterLevel": 120.0}
    watchdog.assess(observe(0.0, occupied))
    assert watchdog.assess(observe(1_000.0, occupied)).should_act


@pytest.mark.parametrize(
    ("policy", "message"),
    [
        (RecoveryPolicy(latch_grace=30.0), "latch grace"),
        (RecoveryPolicy(stall_grace=1.0), "stall grace"),
        (RecoveryPolicy(max_cycle_seconds=60.0), "max cycle seconds"),
        (RecoveryPolicy(quiet_period=0.0), "quiet period"),
        (RecoveryPolicy(cooldown=60.0), "cooldown"),
        (RecoveryPolicy(max_per_hour=0), "max per hour"),
        (RecoveryPolicy(max_consecutive_failures=0), "max consecutive failures"),
        (RecoveryPolicy(poll_interval=1.0), "poll interval"),
        (RecoveryPolicy(duration=0.0), "duration"),
    ],
)
def test_unsafe_policies_are_rejected(policy: RecoveryPolicy, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        policy.validate()


def test_default_policy_is_valid_and_unarmed() -> None:
    policy = RecoveryPolicy()
    policy.validate()
    assert not policy.armed


def test_interventions_are_recorded_without_the_serial_and_without_deduping(
    tmp_path: Path,
) -> None:
    """Identical decisions must both survive the events dedupe index."""
    database = tmp_path / "capture.sqlite"
    observation = observe(0.0, LATCHED)
    assessment = Assessment(stuck=True, reason=IDLE_LATCH, stuck_for=1_000.0, should_act=True)

    with EventStore(database) as store:
        for _ in range(2):
            _record(
                store,
                FakeRobot(),  # type: ignore[arg-type]
                "assessment",
                observation,
                assessment,
                POLICY,
            )
        assert store.counts() == {"intervention": 2}
        records = list(store.recent(2))

    payload = records[0]["payload"]
    assert payload["kind"] == "assessment"
    assert payload["armed"] is False
    assert payload["assessment"]["reason"] == IDLE_LATCH
    assert payload["observation"]["display_code"] == "DC_CAT_DETECT_30M"
    assert records[0]["robot_id"].startswith("lr4-")
    assert "SECRET" not in str(records)
