from __future__ import annotations

import io
import json
import logging
from typing import Any

import pytest

from lr4_diagnostics.autoreset import RecoveryPolicy, Watchdog
from lr4_diagnostics.drawer import DrawerMonitor, DrawerPolicy
from lr4_diagnostics.faults import (
    FaultMonitor,
    FaultPolicy,
    FaultReading,
    FaultVerdict,
    log_motor_fault,
)

# `lambda_handler` also lifts the package logger to INFO on import, which is what
# lets the marker line reach the runtime's root handler.
from lr4_diagnostics.lambda_handler import _fault_policy, _save_state

#: The shipped configuration, so the thresholds are exercised as they run.
POLICY = FaultPolicy()


class FakeDynamoTable:
    """The one method `_save_state` needs. Local rather than shared with the
    other suites: `tests` is not a package, so importing across test modules
    only works when the repository root happens to be on `sys.path`."""

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict[str, Any]] = {}

    def put_item(self, *, Item: dict[str, Any]) -> None:  # noqa: N803 - AWS API spelling
        self.items[(Item["robot_id"], Item["recorded_at"])] = Item


GLOBE = "globeMotorFaultStatus"
PINCH = "pinchStatus"


def _settle(monitor: FaultMonitor, values: dict[str, str], *, samples: int) -> FaultVerdict:
    verdict = FaultVerdict()
    for _ in range(samples):
        verdict = monitor.observe(FaultReading(values=values))
    return verdict


def test_a_single_fault_sample_does_not_alarm() -> None:
    """One garbled or partial payload must not send mail."""
    monitor = FaultMonitor(POLICY)
    verdict = monitor.observe(FaultReading(values={GLOBE: "FAULT_TIMEOUT"}))
    assert not verdict.faulted
    assert verdict.streaks[GLOBE] == 1


def test_a_sustained_fault_alarms_once_the_persistence_gate_is_met() -> None:
    monitor = FaultMonitor(POLICY)
    verdict = _settle(monitor, {GLOBE: "FAULT_TIMEOUT"}, samples=POLICY.consecutive_samples)
    assert verdict.faulted
    assert verdict.active == {GLOBE: "FAULT_TIMEOUT"}
    assert verdict.newly_active == {GLOBE: "FAULT_TIMEOUT"}


def test_the_alarm_repeats_so_it_stays_in_alarm() -> None:
    """The alarm notifies on OK->ALARM; continued lines are what hold it there."""
    monitor = FaultMonitor(POLICY)
    _settle(monitor, {GLOBE: "FAULT_TIMEOUT"}, samples=POLICY.consecutive_samples)
    repeat = monitor.observe(FaultReading(values={GLOBE: "FAULT_TIMEOUT"}))
    assert repeat.faulted
    assert not repeat.newly_active


def test_a_clear_reading_releases_the_latch() -> None:
    """The firmware clearing the flag is the only thing that ends an episode."""
    monitor = FaultMonitor(POLICY)
    _settle(monitor, {GLOBE: "FAULT_TIMEOUT"}, samples=POLICY.consecutive_samples)
    released = monitor.observe(FaultReading(values={GLOBE: "FAULT_CLEAR"}))
    assert not released.faulted
    assert GLOBE not in released.streaks


def test_an_absent_field_holds_the_latch_rather_than_releasing_it() -> None:
    """Silence is not recovery: a payload that omits the field proves nothing."""
    monitor = FaultMonitor(POLICY)
    _settle(monitor, {GLOBE: "FAULT_TIMEOUT"}, samples=POLICY.consecutive_samples)
    held = monitor.observe(FaultReading(values={}))
    assert held.active == {GLOBE: "FAULT_TIMEOUT"}


def test_the_pinch_field_uses_the_unprefixed_clear_spelling() -> None:
    """`pinchStatus` reports CLEAR, not FAULT_CLEAR; both must count as clear."""
    monitor = FaultMonitor(POLICY)
    verdict = _settle(monitor, {PINCH: "CLEAR"}, samples=POLICY.consecutive_samples)
    assert not verdict.faulted


def test_an_unrecognised_value_counts_as_a_fault() -> None:
    """A new fault code should alarm rather than be guessed clear."""
    monitor = FaultMonitor(POLICY)
    verdict = _settle(monitor, {GLOBE: "FAULT_SOMETHING_NEW"}, samples=POLICY.consecutive_samples)
    assert verdict.active == {GLOBE: "FAULT_SOMETHING_NEW"}


def test_fields_latch_independently() -> None:
    """A pinch fault and a motor fault mean different things and different remedies."""
    monitor = FaultMonitor(POLICY)
    _settle(monitor, {GLOBE: "FAULT_TIMEOUT", PINCH: "CLEAR"}, samples=POLICY.consecutive_samples)
    verdict = _settle(
        monitor,
        {GLOBE: "FAULT_CLEAR", PINCH: "PINCH_DETECT"},
        samples=POLICY.consecutive_samples,
    )
    assert verdict.active == {PINCH: "PINCH_DETECT"}


def test_a_changed_fault_code_restarts_the_persistence_count() -> None:
    """A different code is a different fault, not a continuation of the old one."""
    monitor = FaultMonitor(POLICY)
    _settle(monitor, {GLOBE: "FAULT_TIMEOUT"}, samples=POLICY.consecutive_samples)
    changed = monitor.observe(FaultReading(values={GLOBE: "FAULT_OVERCURRENT"}))
    assert changed.active == {GLOBE: "FAULT_TIMEOUT"}
    assert changed.streaks[GLOBE] == 1


def test_from_state_ignores_absent_and_non_string_fields() -> None:
    """`retractMotorFaultStatus` is null on this unit; nulls are unknown, not faults."""
    reading = FaultReading.from_state(
        {GLOBE: "FAULT_TIMEOUT", "retractMotorFaultStatus": None, PINCH: "CLEAR"}
    )
    assert reading.values == {GLOBE: "FAULT_TIMEOUT", PINCH: "CLEAR"}
    assert reading.faults() == {GLOBE: "FAULT_TIMEOUT"}


def test_snapshot_survives_a_round_trip() -> None:
    """The scheduled runtime carries this between one-minute invocations."""
    monitor = FaultMonitor(POLICY)
    _settle(monitor, {GLOBE: "FAULT_TIMEOUT"}, samples=POLICY.consecutive_samples)
    resumed = FaultMonitor(POLICY)
    resumed.restore(monitor.snapshot())
    assert resumed.active == {GLOBE: "FAULT_TIMEOUT"}
    repeat = resumed.observe(FaultReading(values={GLOBE: "FAULT_TIMEOUT"}))
    assert repeat.faulted
    assert not repeat.newly_active


def test_restore_rejects_a_malformed_state() -> None:
    monitor = FaultMonitor(POLICY)
    monitor.restore({"streaks": "nonsense", "active": [1, 2]})
    assert monitor.active == {}


def test_fault_line_reaches_a_root_handler_left_at_warning() -> None:
    """Same trap as the stuck and drawer lines: the Lambda root logger is at WARNING.

    Asserting through a real handler rather than `caplog` is the point --
    `caplog.at_level` forces the level, hiding exactly this failure. Importing
    `lambda_handler` is what lifts the package logger to INFO in the real
    runtime, so the import is load-bearing here, not incidental.
    """
    buffer = io.StringIO()
    handler = logging.StreamHandler(buffer)
    root = logging.getLogger()
    original_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.WARNING)
    try:
        log_motor_fault(FaultVerdict(active={GLOBE: "FAULT_TIMEOUT"}))
    finally:
        root.removeHandler(handler)
        root.setLevel(original_level)

    assert "WATCHDOG_MOTOR_FAULT" in buffer.getvalue()
    assert f"{GLOBE}=FAULT_TIMEOUT" in buffer.getvalue()


def test_the_default_policy_validates() -> None:
    POLICY.validate()


def test_fault_policy_falls_back_to_the_default_when_unset(monkeypatch: Any) -> None:
    monkeypatch.delenv("FAULT_CONSECUTIVE_SAMPLES", raising=False)
    assert _fault_policy() == POLICY


def test_fault_policy_reads_an_override(monkeypatch: Any) -> None:
    monkeypatch.setenv("FAULT_CONSECUTIVE_SAMPLES", "7")
    assert _fault_policy().consecutive_samples == 7


def test_fault_policy_refuses_an_unparseable_override(monkeypatch: Any) -> None:
    """A typo must not silently disable the alarm by parsing to something absurd."""
    monkeypatch.setenv("FAULT_CONSECUTIVE_SAMPLES", "three")
    with pytest.raises(RuntimeError):
        _fault_policy()


def test_save_state_persists_the_fault_latch_across_invocations() -> None:
    """Without this the latch resets every minute and re-sends mail on every check."""
    table = FakeDynamoTable()
    monitor = FaultMonitor(POLICY)
    _settle(monitor, {GLOBE: "FAULT_TIMEOUT"}, samples=POLICY.consecutive_samples)
    _save_state(
        table,
        "lr4-test",
        Watchdog(RecoveryPolicy()),
        DrawerMonitor(DrawerPolicy()),
        monitor,
    )

    stored = table.items[("lr4-test", "STATE")]
    resumed = FaultMonitor(POLICY)
    resumed.restore(json.loads(stored["faults"]))
    assert resumed.active == {GLOBE: "FAULT_TIMEOUT"}
    assert not resumed.observe(FaultReading(values={GLOBE: "FAULT_TIMEOUT"})).newly_active
