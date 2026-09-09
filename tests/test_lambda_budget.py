"""One invocation must not cost more monitoring than its own schedule slot.

EventBridge fires once a minute, and the DynamoDB lease means whatever one
invocation does, the ones behind it skip. Two ways that turns into lost
monitoring:

- **A wait with no ceiling.** Every wait in the routine path used to inherit a
  library default sized for a human at a terminal rather than for a cron job:
  aiohttp's 300-second request ceiling, botocore's 60s/60s with five attempts,
  and nothing capping the invocation as a whole. Observed at 175 seconds
  against a 1.35-second average -- three schedules of no monitoring.
- **A lease nobody releases.** The lock write is conditional, so a retry after
  a lost response fails a condition its own first attempt made false. Mistaking
  that for another invocation's lease strands this one's for the full 660s.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from lr4_diagnostics import lambda_handler
from lr4_diagnostics.autoreset import RecoveryPolicy
from lr4_diagnostics.lambda_handler import (
    _AWS_TIMEOUT,
    _HTTP_TIMEOUT,
    _ROUTINE_BUDGET_SECONDS,
    _run_once,
)

#: What EventBridge is configured for in `infra/main.tf`.
SCHEDULE_SECONDS = 60.0

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


class FakeTable:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict[str, Any]] = {}
        #: Set by a test to stand in for what the conditional lock write does.
        #: Only the LOCK write is routed through it.
        self.lock_write: Callable[[FakeTable, dict[str, Any]], None] | None = None

    def get_item(self, *, Key: dict[str, str], **_: Any) -> dict[str, Any]:  # noqa: N803
        item = self.items.get((Key["robot_id"], Key["recorded_at"]))
        return {} if item is None else {"Item": item}

    def put_item(self, *, Item: dict[str, Any], **_: Any) -> None:  # noqa: N803
        if self.lock_write is not None and Item["recorded_at"] == "LOCK":
            self.lock_write(self, Item)
            return
        self.items[(Item["robot_id"], Item["recorded_at"])] = Item

    def delete_item(self, *, Key: dict[str, str], **_: Any) -> None:  # noqa: N803
        self.items.pop((Key["robot_id"], Key["recorded_at"]), None)

    @property
    def lease_held(self) -> bool:
        return ("CONTROL", "LOCK") in self.items


class _ConditionalCheckFailedException(Exception):
    pass


class FakeDynamoDB:
    """Enough of `boto3.resource("dynamodb")` for one invocation."""

    def __init__(self, table: FakeTable) -> None:
        self._table = table
        self.config: Any = None
        exceptions = type(
            "Exceptions",
            (),
            {"ConditionalCheckFailedException": _ConditionalCheckFailedException},
        )
        client = type("Client", (), {"exceptions": exceptions()})
        self.meta = type("Meta", (), {"client": client()})()

    def Table(self, _name: str) -> FakeTable:  # noqa: N802 - AWS API spelling
        return self._table


class FakeSecrets:
    def __init__(self) -> None:
        self.config: Any = None
        self.updated: list[str] = []

    def get_secret_value(self, *, SecretId: str) -> dict[str, str]:  # noqa: N803
        del SecretId
        return {"SecretString": json.dumps({"username": "u", "password": "p"})}

    def update_secret(self, *, SecretId: str, SecretString: str) -> None:  # noqa: N803
        del SecretId
        self.updated.append(SecretString)


class FakeRobot:
    serial = "LR4C-TEST-0001"

    def __init__(self, state: dict[str, Any]) -> None:
        # Copied, not aliased: a test that swaps the unit into another state
        # mid-run would otherwise rewrite the shared constant it was built from.
        self._state = dict(state)
        self.refreshes = 0
        self.resets = 0

    async def refresh(self) -> None:
        self.refreshes += 1

    async def reset(self) -> None:
        self.resets += 1

    def to_dict(self) -> dict[str, Any]:
        return dict(self._state)


class FakeAccount:
    def __init__(self, robot: FakeRobot) -> None:
        self.robots = [robot]
        self.session = object()
        self.disconnected = False

    async def disconnect(self) -> None:
        self.disconnected = True


class FakeClientSession:
    """Records the timeout it was built with, and whether it was closed."""

    instances: list[FakeClientSession] = []

    def __init__(self, *, timeout: Any = None) -> None:
        self.timeout = timeout
        self.closed = False
        FakeClientSession.instances.append(self)

    async def close(self) -> None:
        self.closed = True


def _harness(
    monkeypatch: pytest.MonkeyPatch,
    *,
    state: dict[str, Any],
    armed: bool = False,
    rezero_armed: bool = False,
    activity: Callable[[], Awaitable[list[dict[str, Any]]]] | None = None,
    summary: Callable[[], Awaitable[list[dict[str, Any]]]] | None = None,
) -> tuple[FakeTable, FakeRobot, FakeSecrets]:
    """Wire `_run_once` to fakes, leaving every decision gate real."""
    table = FakeTable()
    dynamodb = FakeDynamoDB(table)
    secrets = FakeSecrets()
    robot = FakeRobot(state)
    account = FakeAccount(robot)

    def resource(_name: str, *, config: Any = None) -> FakeDynamoDB:
        dynamodb.config = config
        return dynamodb

    def client(_name: str, *, config: Any = None) -> FakeSecrets:
        secrets.config = config
        return secrets

    async def fetch_activity(_session: Any, _serial: str, **_: Any) -> list[dict[str, Any]]:
        return [] if activity is None else await activity()

    async def fetch_history(_session: Any, _serial: str, **_: Any) -> list[dict[str, Any]]:
        return []

    async def fetch_summary(_session: Any, _serial: str, **_: Any) -> list[dict[str, Any]]:
        return [] if summary is None else await summary()

    async def connect(*_args: Any, **_kwargs: Any) -> FakeAccount:
        return account

    monkeypatch.setenv("WATCHDOG_STATE_TABLE", "watchdog")
    monkeypatch.setenv("WHISKER_SECRET_ARN", "arn:aws:secretsmanager:test")
    monkeypatch.setenv("WATCHDOG_ARMED", "true" if armed else "false")
    monkeypatch.setenv("WATCHDOG_REZERO_ARMED", "true" if rezero_armed else "false")
    monkeypatch.setattr(lambda_handler.boto3, "resource", resource)
    monkeypatch.setattr(lambda_handler.boto3, "client", client)
    monkeypatch.setattr(lambda_handler, "LitterRobot4", FakeRobot)
    monkeypatch.setattr(lambda_handler, "fetch_activity", fetch_activity)
    monkeypatch.setattr(lambda_handler, "fetch_history_download", fetch_history)
    monkeypatch.setattr(lambda_handler, "fetch_summary", fetch_summary)
    monkeypatch.setattr(lambda_handler, "_connect", connect)
    monkeypatch.setattr(lambda_handler, "ClientSession", FakeClientSession)
    FakeClientSession.instances = []
    return table, robot, secrets


def _stuck_state(now: float) -> dict[str, Any]:
    """A latch old enough to be past every persistence and quiet-period gate."""
    return {
        "robot_id": lambda_handler.pseudonym(FakeRobot.serial),
        "recorded_at": "STATE",
        "watchdog": json.dumps(
            {
                "latched_since": now - 5_000,
                "bonnet_secure": True,
                "last_cat_activity": now - 5_000,
                "attempts": [],
                "consecutive_failures": 0,
            }
        ),
    }


def test_whisker_requests_cannot_outlive_a_schedule_slot() -> None:
    """aiohttp's default is 300s per request; the schedule fires every 60."""
    assert _HTTP_TIMEOUT.total is not None
    assert _HTTP_TIMEOUT.total < SCHEDULE_SECONDS


def test_aws_calls_cannot_outlive_a_schedule_slot() -> None:
    """These are synchronous, so `asyncio.timeout` cannot bound them."""
    # `Config` sets its options dynamically, so it has nothing to type against.
    config: Any = _AWS_TIMEOUT
    worst_case = config.retries["max_attempts"] * (config.connect_timeout + config.read_timeout)
    assert worst_case < SCHEDULE_SECONDS


def test_the_routine_budget_leaves_room_for_the_next_schedule() -> None:
    assert _ROUTINE_BUDGET_SECONDS < SCHEDULE_SECONDS


def test_a_healthy_check_releases_its_lease_and_closes_the_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    table, robot, _ = _harness(monkeypatch, state=HEALTHY)

    result = asyncio.run(_run_once())

    assert result == {"attempts": 0, "recoveries": 0}
    assert robot.refreshes == 1
    assert not table.lease_held, "the lease must not survive a completed invocation"
    session = FakeClientSession.instances[0]
    assert session.timeout is _HTTP_TIMEOUT, "pylitterbot must not fall back to its own default"
    assert session.closed, "`Session.close` skips a supplied session; closing it is ours to do"


def test_the_boto3_clients_are_built_with_a_bounded_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    table, _, secrets = _harness(monkeypatch, state=HEALTHY)

    asyncio.run(_run_once())

    assert secrets.config is _AWS_TIMEOUT
    assert not table.lease_held


def test_a_stalled_read_is_abandoned_instead_of_running_into_the_next_schedule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reported failure: one wait with no ceiling costs three schedules."""

    async def never_returns() -> list[dict[str, Any]]:
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")

    table, _, _ = _harness(monkeypatch, state=HEALTHY, activity=never_returns)
    monkeypatch.setattr(lambda_handler, "_ROUTINE_BUDGET_SECONDS", 0.05)

    with pytest.raises(TimeoutError):
        asyncio.run(_run_once())

    assert not table.lease_held, "an abandoned invocation must still hand the lease back"
    assert FakeClientSession.instances[0].closed


def test_the_budget_does_not_cut_a_recovery_short(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recovery is meant to be slow.

    `verify_timeout` alone is five minutes, and both the DynamoDB lease and the
    Lambda timeout are sized for it. A budget that killed a dispatched recovery
    would strand the globe mid-cycle -- worse than the overlap it exists to
    prevent.
    """
    now = lambda_handler.time()
    table, _, _ = _harness(monkeypatch, state=LATCHED, armed=True)
    table.put_item(Item=_stuck_state(now))
    monkeypatch.setattr(lambda_handler, "_ROUTINE_BUDGET_SECONDS", 0.05)
    recovered: list[bool] = []

    async def slow_recovery(*_args: Any, **_kwargs: Any) -> bool:
        await asyncio.sleep(0.3)  # six times the budget
        recovered.append(True)
        return True

    monkeypatch.setattr(lambda_handler, "_recover", slow_recovery)

    result = asyncio.run(_run_once())

    assert recovered == [True], "the dispatched recovery must be allowed to finish"
    assert result == {"attempts": 1, "recoveries": 1}
    assert not table.lease_held


def test_the_stuck_state_this_relies_on_really_does_warrant_acting() -> None:
    """Guard the fixture: a gate change that makes it un-actionable is silent."""
    now = lambda_handler.time()
    watchdog = lambda_handler.Watchdog(RecoveryPolicy(armed=True), now=lambda: now)
    watchdog.restore(json.loads(_stuck_state(now)["watchdog"]))

    assessment = watchdog.assess(lambda_handler.Observation.from_state(now, LATCHED))

    assert assessment.stuck and assessment.should_act


def test_a_retried_lease_write_is_recognised_as_this_invocations_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lock write is conditional, and botocore retries it.

    Attempt one lands and its response is lost; attempt two then fails a
    condition its own predecessor made false. Reading that as another
    invocation's lease strands one this invocation owns for the full 660
    seconds -- and because the skip returns before the `finally` that releases
    it, nothing but the TTL ever clears it.
    """
    table, robot, _ = _harness(monkeypatch, state=HEALTHY)

    def landed_but_reported_failure(current: FakeTable, item: dict[str, Any]) -> None:
        current.items[(item["robot_id"], item["recorded_at"])] = item
        raise _ConditionalCheckFailedException()

    table.lock_write = landed_but_reported_failure

    result = asyncio.run(_run_once())

    assert result == {"attempts": 0, "recoveries": 0}
    assert robot.refreshes == 1, "the check must run rather than skip its own slot"
    assert not table.lease_held, "and must still hand the lease back at the end"


def test_a_lease_held_by_another_invocation_is_left_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gate this rests on: a foreign `lock_id` still means stand down."""
    table, robot, _ = _harness(monkeypatch, state=HEALTHY)
    incumbent = {
        "robot_id": "CONTROL",
        "recorded_at": "LOCK",
        "lock_id": "some-other-invocation",
        "expires_at": int(lambda_handler.time()) + 660,
    }
    table.items[("CONTROL", "LOCK")] = incumbent

    def refuse(_current: FakeTable, _item: dict[str, Any]) -> None:
        raise _ConditionalCheckFailedException()

    table.lock_write = refuse

    result = asyncio.run(_run_once())

    assert result == {"attempts": 0, "recoveries": 0}
    assert robot.refreshes == 0, "two invocations must not read the unit at once"
    assert table.items[("CONTROL", "LOCK")] is incumbent, "nor release each other's lease"
