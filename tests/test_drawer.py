from __future__ import annotations

import io
import json
import logging
from typing import Any

import pytest

from lr4_diagnostics.drawer import (
    DrawerMonitor,
    DrawerPolicy,
    DrawerReading,
    log_drawer_full,
)
from lr4_diagnostics.lambda_handler import _drawer_policy

#: The shipped configuration, so the thresholds are exercised as they run.
POLICY = DrawerPolicy()


def _settle(monitor: DrawerMonitor, percent: float, *, samples: int) -> Any:
    verdict = None
    for _ in range(samples):
        verdict = monitor.observe(DrawerReading(percent=percent))
    return verdict


def test_a_single_high_sample_does_not_warn() -> None:
    """The DFI ToF swings several percent at rest; one spike is not a full drawer."""
    monitor = DrawerMonitor(POLICY)
    verdict = monitor.observe(DrawerReading(percent=92.0))
    assert not verdict.warned
    assert verdict.streak == 1


def test_a_sustained_level_warns_once_the_persistence_gate_is_met() -> None:
    monitor = DrawerMonitor(POLICY)
    verdict = _settle(monitor, 92.0, samples=POLICY.consecutive_samples)
    assert verdict is not None
    assert verdict.warned
    assert verdict.newly_warned


def test_the_warning_repeats_so_the_alarm_stays_in_alarm() -> None:
    """The alarm notifies on OK->ALARM; continued lines are what hold it there."""
    monitor = DrawerMonitor(POLICY)
    _settle(monitor, 92.0, samples=POLICY.consecutive_samples)
    repeat = monitor.observe(DrawerReading(percent=92.0))
    assert repeat.warned
    assert not repeat.newly_warned


def test_a_dip_inside_the_hysteresis_band_does_not_release_the_warning() -> None:
    """Releasing on the first dip would flap the alarm and re-send the mail."""
    monitor = DrawerMonitor(POLICY)
    _settle(monitor, 92.0, samples=POLICY.consecutive_samples)
    assert monitor.observe(DrawerReading(percent=80.0)).warned
    assert monitor.observe(DrawerReading(percent=70.0)).warned
    assert monitor.observe(DrawerReading(percent=61.0)).warned


def test_emptying_the_drawer_releases_the_warning_and_re_arms_it() -> None:
    monitor = DrawerMonitor(POLICY)
    _settle(monitor, 92.0, samples=POLICY.consecutive_samples)
    assert not monitor.observe(DrawerReading(percent=5.0)).warned

    # And the gate has to be met again rather than the old streak carrying over.
    assert not monitor.observe(DrawerReading(percent=92.0)).warned
    assert _settle(monitor, 92.0, samples=POLICY.consecutive_samples - 1).warned


def test_a_dip_inside_the_band_restarts_the_streak_before_a_warning() -> None:
    """Sub-threshold readings must not accumulate toward a warning across a dip."""
    monitor = DrawerMonitor(POLICY)
    _settle(monitor, 92.0, samples=POLICY.consecutive_samples - 1)
    assert monitor.observe(DrawerReading(percent=70.0)).streak == 0
    assert not monitor.observe(DrawerReading(percent=92.0)).warned


def test_the_firmware_flag_warns_immediately() -> None:
    """`isDFIFull` is a boolean, not a noisy distance, and it is what the app pushes on."""
    monitor = DrawerMonitor(POLICY)
    verdict = monitor.observe(DrawerReading(percent=None, drawer_full=True))
    assert verdict.warned
    assert verdict.newly_warned


def test_mid_cycle_and_missing_readings_neither_build_nor_clear() -> None:
    """The DFI sensor is re-read during a cycle, so those samples are not levels."""
    monitor = DrawerMonitor(POLICY)
    held = POLICY.consecutive_samples - 1
    _settle(monitor, 92.0, samples=held)
    assert monitor.observe(DrawerReading(percent=10.0, at_rest=False)).streak == held
    assert monitor.observe(DrawerReading(percent=None)).streak == held
    assert monitor.observe(DrawerReading(percent=92.0)).warned


def test_a_mid_cycle_reading_cannot_clear_a_standing_warning() -> None:
    monitor = DrawerMonitor(POLICY)
    _settle(monitor, 92.0, samples=POLICY.consecutive_samples)
    assert monitor.observe(DrawerReading(percent=2.0, at_rest=False)).warned


def test_state_survives_a_round_trip_through_json() -> None:
    """A scheduled runtime persists this between invocations as a JSON string."""
    monitor = DrawerMonitor(POLICY)
    _settle(monitor, 92.0, samples=POLICY.consecutive_samples - 1)

    restored = DrawerMonitor(POLICY)
    restored.restore(json.loads(json.dumps(monitor.snapshot())))
    assert restored.observe(DrawerReading(percent=92.0)).warned


def test_restore_ignores_junk_rather_than_trusting_it() -> None:
    monitor = DrawerMonitor(POLICY)
    monitor.restore({"streak": -4, "warned": "yes"})
    assert not monitor.warned
    assert monitor.observe(DrawerReading(percent=92.0)).streak == 1


def test_reading_is_built_from_the_raw_state_payload() -> None:
    reading = DrawerReading.from_state({"DFILevelPercent": 91, "isDFIFull": False}, at_rest=True)
    assert reading.percent == 91.0
    assert not reading.drawer_full
    assert reading.at_rest


@pytest.mark.parametrize(
    "policy",
    [
        DrawerPolicy(warn_percent=0),
        DrawerPolicy(warn_percent=101),
        DrawerPolicy(warn_percent=50, clear_percent=50),
        DrawerPolicy(warn_percent=50, clear_percent=60),
        DrawerPolicy(clear_percent=-1),
        DrawerPolicy(consecutive_samples=0),
    ],
)
def test_validate_rejects_thresholds_that_would_be_noisy_or_unreachable(
    policy: DrawerPolicy,
) -> None:
    with pytest.raises(ValueError):
        policy.validate()


def test_shipped_policy_validates() -> None:
    POLICY.validate()


def test_policy_reads_overrides_from_the_environment(monkeypatch: Any) -> None:
    monkeypatch.setenv("DRAWER_WARN_PERCENT", "75")
    monkeypatch.setenv("DRAWER_CLEAR_PERCENT", "40")
    monkeypatch.setenv("DRAWER_CONSECUTIVE_SAMPLES", "3")
    policy = _drawer_policy()
    assert (policy.warn_percent, policy.clear_percent, policy.consecutive_samples) == (
        75.0,
        40.0,
        3,
    )


def test_policy_refuses_an_unparseable_override(monkeypatch: Any) -> None:
    """A typo must not silently disable the warning by parsing to something absurd."""
    monkeypatch.setenv("DRAWER_WARN_PERCENT", "eighty-five")
    with pytest.raises(RuntimeError):
        _drawer_policy()


def test_policy_falls_back_to_the_defaults_when_unset(monkeypatch: Any) -> None:
    monkeypatch.delenv("DRAWER_WARN_PERCENT", raising=False)
    monkeypatch.delenv("DRAWER_CLEAR_PERCENT", raising=False)
    monkeypatch.delenv("DRAWER_CONSECUTIVE_SAMPLES", raising=False)
    assert _drawer_policy() == POLICY


def test_drawer_line_reaches_a_root_handler_left_at_warning() -> None:
    """Same trap as the stuck line: the Lambda root logger sits at WARNING.

    Asserting through a real handler rather than `caplog` is the point --
    `caplog.at_level` forces the level, hiding exactly this failure.
    """
    buffer = io.StringIO()
    handler = logging.StreamHandler(buffer)
    root = logging.getLogger()
    original_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.WARNING)
    try:
        monitor = DrawerMonitor(POLICY)
        verdict = _settle(monitor, 92.0, samples=POLICY.consecutive_samples)
        assert verdict is not None
        log_drawer_full(verdict, POLICY)
    finally:
        root.removeHandler(handler)
        root.setLevel(original_level)

    assert "WATCHDOG_DRAWER_FULL" in buffer.getvalue()
