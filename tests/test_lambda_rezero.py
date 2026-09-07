"""The proactive scale re-zero, running under the scheduled Lambda runtime.

`autoreset._watch` keeps the re-zero's pacing in memory across a long-lived
loop.  A once-a-minute Lambda has no memory between invocations, so the three
things that loop got for free have to be rebuilt and are what these tests are
about:

- **The arming gate.** `WATCHDOG_ARMED` is already true in the deployment. The
  re-zero's idle double-press is inferred from Whisker's physical-button
  documentation rather than measured against this unit, and a Reset can rotate
  the globe, so it must take a second switch of its own -- one that shipping
  this code does not flip.
- **The poll gate.** The schedule fires 1,440 times a day against a weekly
  aggregate. Without a persisted timestamp the handler would re-read it every
  minute, and `assess_drift` would be offered the same number over and over.
- **The ordering gate.** An invocation that has just driven the globe is not a
  safe place to drive it again.
"""

from __future__ import annotations

import asyncio
from time import time
from typing import Any

import pytest
from test_lambda_budget import HEALTHY, LATCHED, FakeRobot, _harness, _stuck_state

from lr4_diagnostics import autoreset, lambda_handler
from lr4_diagnostics.autoreset import RecoveryPolicy
from lr4_diagnostics.lambda_handler import _run_once, _summary_due

#: A weekly maxWeight past what this household's cats can physically produce.
#: 21.02 lb is the real relapse reading from the week of 2026-08-22; two cats
#: at once cap near 19.49 lb. See docs/hypothesis.md.
DRIFTED: list[dict[str, Any]] = [
    {"weekStart": "2026-09-06", "weekEnd": "2026-09-12", "maxWeight": 21.02}
]
#: The same week reading normally -- inside the range both cats can produce.
NORMAL: list[dict[str, Any]] = [
    {"weekStart": "2026-09-06", "weekEnd": "2026-09-12", "maxWeight": 11.23}
]


@pytest.fixture(autouse=True)
def _no_settle(monkeypatch: pytest.MonkeyPatch) -> None:
    """Collapse the pause between the two presses.

    `settle` is 20 seconds of real time and is not what any of these tests are
    about. The presses themselves stay real.
    """

    async def sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(autoreset.asyncio, "sleep", sleep)


def _summary(rows: list[dict[str, Any]]) -> Any:
    calls: list[int] = []

    async def fetch() -> list[dict[str, Any]]:
        calls.append(1)
        return rows

    fetch.calls = calls  # type: ignore[attr-defined]
    return fetch


def test_a_drifting_scale_is_reported_but_not_touched_when_only_armed(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The safety property of this whole change.

    `WATCHDOG_ARMED` is true in the live deployment. If the re-zero rode on it,
    merging this would have started sending an unverified command to a machine
    that rotates -- without anyone deciding to.
    """
    _table, robot, _secrets = _harness(
        monkeypatch, state=HEALTHY, armed=True, summary=_summary(DRIFTED)
    )
    with caplog.at_level("INFO"):
        asyncio.run(_run_once())

    assert robot.resets == 0
    assert "WATCHDOG_SCALE_DRIFT" in caplog.text
    # Detection has to survive the unarmed default, or a detection-only run
    # tells the owner nothing about the drift it declined to act on.
    assert "armed=False" in caplog.text


def test_a_drifting_scale_is_re_zeroed_once_both_gates_are_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two Reset presses, which is the recalibration Whisker documents."""
    _table, robot, _secrets = _harness(
        monkeypatch,
        state=HEALTHY,
        armed=True,
        rezero_armed=True,
        summary=_summary(DRIFTED),
    )
    result = asyncio.run(_run_once())

    assert robot.resets == 2
    assert result == {"attempts": 1, "recoveries": 1}


def test_the_rezero_gate_does_nothing_on_its_own(monkeypatch: pytest.MonkeyPatch) -> None:
    """`rezero_armed` is an additional gate, not an alternative to `armed`."""
    _table, robot, _secrets = _harness(
        monkeypatch,
        state=HEALTHY,
        armed=False,
        rezero_armed=True,
        summary=_summary(DRIFTED),
    )
    asyncio.run(_run_once())

    assert robot.resets == 0


def test_a_normal_week_is_neither_reported_nor_acted_on(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The ceiling is what separates drift from a heavy cat."""
    _table, robot, _secrets = _harness(
        monkeypatch,
        state=HEALTHY,
        armed=True,
        rezero_armed=True,
        summary=_summary(NORMAL),
    )
    with caplog.at_level("INFO"):
        asyncio.run(_run_once())

    assert robot.resets == 0
    assert "WATCHDOG_SCALE_DRIFT" not in caplog.text


def test_the_weekly_summary_is_not_re_read_on_every_schedule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """1,440 invocations a day against a number that moves 52 times a year.

    The second invocation must find the persisted timestamp and skip the read.
    """
    summary = _summary(NORMAL)
    _table, _robot, _secrets = _harness(monkeypatch, state=HEALTHY, armed=True, summary=summary)
    asyncio.run(_run_once())
    asyncio.run(_run_once())

    assert len(summary.calls) == 1


def test_the_poll_timestamp_survives_the_save_made_before_a_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`put_item` replaces the whole item, so every save has to carry it.

    A recovery saves state mid-invocation, *before* dispatching, precisely so
    that an invocation killed in the verification poll still leaves a record.
    That is the only write where dropping the timestamp is not masked by the
    save at the end of the pass -- so this kills the recovery to reach it.
    """
    summary = _summary(NORMAL)

    async def recover(*_args: Any, **_kwargs: Any) -> bool:
        raise RuntimeError("killed in the verification poll")

    monkeypatch.setattr(lambda_handler, "_recover", recover)
    table, robot, _secrets = _harness(monkeypatch, state=HEALTHY, armed=True, summary=summary)
    asyncio.run(_run_once())
    assert len(summary.calls) == 1
    robot_id = lambda_handler.pseudonym(FakeRobot.serial)
    saved_check = table.items[(robot_id, "STATE")]["last_summary_check"]

    # Latch the unit and age its timers, so the next invocation genuinely takes
    # the recovery path -- a healthy payload would clear the latch on the way
    # in and never reach the save under test.
    robot._state.clear()
    robot._state.update(LATCHED)
    stuck = _stuck_state(time())
    stuck["last_summary_check"] = saved_check
    table.items[(robot_id, "STATE")] = stuck

    with pytest.raises(RuntimeError):
        asyncio.run(_run_once())

    assert table.items[(robot_id, "STATE")]["last_summary_check"] == saved_check


def test_an_invocation_that_drove_the_globe_does_not_also_re_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One invocation, at most one reason to move the globe."""
    summary = _summary(DRIFTED)

    async def recover(*_args: Any, **_kwargs: Any) -> bool:
        return True

    monkeypatch.setattr(lambda_handler, "_recover", recover)
    table, _robot, _secrets = _harness(
        monkeypatch,
        state=LATCHED,
        armed=True,
        rezero_armed=True,
        summary=summary,
    )
    robot_id = lambda_handler.pseudonym(FakeRobot.serial)
    table.items[(robot_id, "STATE")] = _stuck_state(time())

    asyncio.run(_run_once())

    assert summary.calls == []


def test_summary_due_paces_itself_off_the_poll_interval() -> None:
    policy = RecoveryPolicy()
    assert _summary_due(None, 1_000.0, policy) is True
    assert _summary_due(1_000.0, 1_000.0 + policy.rezero_poll_interval - 1, policy) is False
    assert _summary_due(1_000.0, 1_000.0 + policy.rezero_poll_interval, policy) is True
