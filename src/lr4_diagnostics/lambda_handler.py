"""One scheduled, fail-closed watchdog check for AWS Lambda.

The Lambda deployment deliberately does not use the long-lived websocket
subscriptions from the container sidecar.  EventBridge invokes this handler
once a minute; DynamoDB holds the state machine between invocations and the
Whisker activity history supplies the quiet-period gate.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from functools import partial
from hashlib import sha256
from time import time
from typing import Any
from uuid import uuid4

import boto3
from aiohttp import ClientSession, ClientTimeout
from botocore.config import Config
from botocore.exceptions import ClientError
from pycognito.exceptions import TokenVerificationException
from pylitterbot import Account, LitterRobot4
from pylitterbot.exceptions import LitterRobotException

from .analysis import parse_timestamp
from .autoreset import (
    CAT_ACTIVITY_VALUES,
    InterventionStore,
    Observation,
    RecoveryPolicy,
    Watchdog,
    _latest_weight_sample,
    _record,
    _record_drift,
    _recover,
    _rezero_scale,
    log_scale_drift,
    log_stuck,
)
from .drawer import DrawerMonitor, DrawerPolicy, DrawerReading, log_drawer_full
from .faults import FaultMonitor, FaultPolicy, FaultReading, log_motor_fault
from .queries import (
    GraphQLQueryError,
    fetch_activity,
    fetch_history_download,
    fetch_summary,
)
from .redact import pseudonym, redact_payload

_LOGGER = logging.getLogger(__name__)

# `cli.py` raises this for the CLI entry point; Lambda is the other one, and it
# arrives with the root logger still at WARNING. `log_stuck` writes
# WATCHDOG_STUCK at INFO, and the CloudWatch metric filter behind the stuck
# alarm can only match a line that was actually emitted -- without this the
# alarm stays OK through every stuck unit. Ancestor logger levels do not gate
# propagation, so setting the package logger is enough to reach the runtime's
# root handler.
logging.getLogger(__package__).setLevel(logging.INFO)

_CAPTURE_RETENTION_SECONDS = 7_776_000  # 90 days
_CAPTURE_OVERLAP_SECONDS = 300
_CAPTURE_GAP_SECONDS = 180
_INITIAL_HISTORY_DAYS = 35

#: Per-request ceiling for Whisker HTTP calls.
#:
#: pylitterbot builds its own `aiohttp.ClientSession` when none is supplied,
#: and aiohttp's default is `total=300` -- five times the schedule interval,
#: for a call whose median is well under a second. One stalled request could
#: therefore outlive four invocations on its own. `Account` accepts a session,
#: so supply one with a budget that fits a once-a-minute cadence.
_HTTP_TIMEOUT = ClientTimeout(total=20, connect=10, sock_read=10)

#: Per-call ceiling for DynamoDB and Secrets Manager.
#:
#: botocore's defaults are 60s connect, 60s read and legacy retry mode (five
#: attempts), so one unreachable endpoint is minutes of blocking. These calls
#: are synchronous, which also puts them out of reach of `asyncio.timeout`:
#: nothing else in this handler can bound them, so they have to bound
#: themselves. Every write here is keyed to be idempotent, so a retry after a
#: timeout re-writes the same item rather than duplicating it.
_AWS_TIMEOUT = Config(
    connect_timeout=3,
    read_timeout=5,
    retries={"mode": "standard", "max_attempts": 3},
)

#: Wall-clock ceiling for the routine part of one invocation.
#:
#: EventBridge fires once a minute. A run that outlives its own slot keeps the
#: lease while the invocations queued behind it skip, so the unit goes
#: unmonitored for as long as the stall lasts -- observed once at 175 seconds
#: against a 1.35-second average, which cost three schedules. Giving up and
#: failing closed hands monitoring back to the next schedule in one minute
#: instead of three.
#:
#: Recovery is deliberately excluded; see `_run_once`.
_ROUTINE_BUDGET_SECONDS = 45.0


def handler(_event: dict[str, object], _context: object) -> dict[str, int]:
    """Run one check.  Errors fail closed: a later schedule will retry."""
    return asyncio.run(_run_once())


async def _run_once() -> dict[str, int]:
    table_name = _required_env("WATCHDOG_STATE_TABLE")
    secret_arn = _required_env("WHISKER_SECRET_ARN")
    armed = os.getenv("WATCHDOG_ARMED", "false").lower() == "true"
    # A second, independent gate, and deliberately not derived from
    # `WATCHDOG_ARMED`. The stuck-state recovery presses were measured against
    # this unit; the proactive re-zero's idle double-press is inferred from
    # Whisker's physical-button documentation and has never been confirmed
    # through the API. `WATCHDOG_ARMED` is already true in the deployment, so
    # sharing its flag would have armed an unverified command path the moment
    # this shipped.
    rezero_armed = os.getenv("WATCHDOG_REZERO_ARMED", "false").lower() == "true"
    dynamodb: Any = boto3.resource("dynamodb", config=_AWS_TIMEOUT)
    table = dynamodb.Table(table_name)
    secrets = boto3.client("secretsmanager", config=_AWS_TIMEOUT)
    credentials = _load_credentials(secrets, secret_arn)
    token_changed = False

    def save_token(token: dict[str, Any] | None) -> None:
        nonlocal token_changed
        credentials["token"] = token
        token_changed = True

    policy = RecoveryPolicy(armed=armed, rezero_armed=rezero_armed)
    policy.validate()
    drawer_policy = _drawer_policy()
    drawer_policy.validate()
    fault_policy = _fault_policy()
    fault_policy.validate()
    lock_id = str(uuid4())
    now = int(time())
    try:
        table.put_item(
            Item={
                "robot_id": "CONTROL",
                "recorded_at": "LOCK",
                "lock_id": lock_id,
                "expires_at": now + 660,
            },
            ConditionExpression="attribute_not_exists(robot_id) OR expires_at < :now",
            ExpressionAttributeValues={":now": now},
        )
    except dynamodb.meta.client.exceptions.ConditionalCheckFailedException:
        if not _holds_lease(table, lock_id):
            _LOGGER.info("A previous watchdog invocation still holds the lease; skipping.")
            return {"attempts": 0, "recoveries": 0}
        # The write landed and its response was lost, so the retry failed a
        # condition its own first attempt had made false. Reading that as
        # somebody else's lease would strand one this invocation owns for the
        # full 660 seconds -- and the release below is only reachable past
        # this point, so the skip is what makes it permanent.
        _LOGGER.info("Reclaimed the lease this invocation had already taken.")
    attempts = 0
    recoveries = 0
    account: Account | None = None
    websession = ClientSession(timeout=_HTTP_TIMEOUT)
    try:
        # `asyncio.timeout` only fires at an `await`, so it bounds the Whisker
        # calls and nothing else; the synchronous DynamoDB and Secrets Manager
        # calls are bounded by `_AWS_TIMEOUT` instead. Between them every wait
        # in the routine path has a ceiling.
        async with asyncio.timeout(_ROUTINE_BUDGET_SECONDS) as routine:
            account = await _connect(credentials, save_token, websession)
            robots = [robot for robot in account.robots if isinstance(robot, LitterRobot4)]
            if not robots:
                raise RuntimeError("No Litter-Robot 4 was found on this Whisker account.")
            for robot in robots:
                now = time()
                robot_id = pseudonym(robot.serial)
                watchdog = Watchdog(policy, now=time)
                drawer = DrawerMonitor(drawer_policy)
                faults = FaultMonitor(fault_policy)
                state = table.get_item(Key={"robot_id": robot_id, "recorded_at": "STATE"}).get(
                    "Item"
                )
                if state and isinstance(state.get("watchdog"), str):
                    watchdog.restore(json.loads(state["watchdog"]))
                if state and isinstance(state.get("drawer"), str):
                    drawer.restore(json.loads(state["drawer"]))
                if state and isinstance(state.get("faults"), str):
                    faults.restore(json.loads(state["faults"]))
                summary_checked_at = (
                    float(state["last_summary_check"])
                    if state and isinstance(state.get("last_summary_check"), Decimal)
                    else None
                )

                # Without a trustworthy activity response we cannot prove the quiet
                # period; fail closed rather than risk a reset around a cat.
                activity = await fetch_activity(account.session, robot.serial, limit=100)
                _note_recent_cat_activity(watchdog, activity, now, policy.quiet_period)
                capture = _DynamoCaptureStore(table, robot_id)
                cursor = capture.cursor()
                history_initialized = cursor.history_initialized
                if _needs_history_backfill(cursor, now):
                    try:
                        history = await fetch_history_download(
                            account.session,
                            robot.serial,
                            start_date=_history_start(cursor, now),
                            limit=1000,
                        )
                    except GraphQLQueryError as exc:
                        # The history backfill improves completeness, but a denied
                        # or unavailable read must not interrupt safety monitoring.
                        _LOGGER.warning("History backfill unavailable: %s", exc)
                    else:
                        capture.add_rows("history", history, observed_at=_iso_at(now))
                        history_initialized = True
                high_water = capture.add_activity_rows(activity, cursor, observed_at=_iso_at(now))
                await robot.refresh()
                payload = copy.deepcopy(robot.to_dict())
                observation = Observation.from_state(now, payload)
                capture.add_state(payload, observed_at=_iso_at(now))
                capture.save_cursor(
                    high_water_epoch=high_water[0],
                    high_water_at=high_water[1],
                    collected_at=now,
                    history_initialized=history_initialized,
                )
                # Evaluated before the stuck assessment because `_recover` can sit in
                # the verification poll for `verify_timeout`; a drawer warning is
                # pure and instant and should not queue behind it.
                drawer_verdict = drawer.observe(
                    DrawerReading.from_state(payload, at_rest=not observation.is_cycling)
                )
                if drawer_verdict.warned:
                    log_drawer_full(drawer_verdict, drawer_policy)

                # Same reasoning, and the same place in the order: pure, instant, and
                # the one signal `assess` cannot produce. A unit parked at home with a
                # latched motor fault is `is_healthy` as far as the watchdog is
                # concerned, so without this the fault is invisible to every alarm.
                fault_verdict = faults.observe(FaultReading.from_state(payload))
                if fault_verdict.faulted:
                    log_motor_fault(fault_verdict)

                assessment = watchdog.assess(observation)
                store = _DynamoInterventionStore(table, robot_id)
                if assessment.stuck:
                    _record(store, robot, "assessment", observation, assessment, policy)
                    log_stuck(assessment, policy)
                    if assessment.should_act:
                        # Recovery drives the globe and then watches it park,
                        # which is deliberately slower than one schedule slot:
                        # `verify_timeout` alone is five minutes, and both the
                        # lease and the Lambda timeout are sized for it.
                        # Suspending the budget keeps it a bound on stalls
                        # rather than a cap on the one wait meant to be long,
                        # and puts the whole dispatch block out of reach of a
                        # cancellation that could record an attempt that never
                        # reached the device.
                        routine.reschedule(None)
                        attempts += 1
                        # Persisted before dispatch, not after. `_recover` can sit
                        # in the verification poll for `verify_timeout`; if the
                        # invocation is killed in there, a command has reached the
                        # device and the only record of it would be lost, leaving
                        # the next invocation with no cooldown and no attempt count.
                        watchdog.note_attempt(dispatched=policy.armed, at=time())
                        _save_state(table, robot_id, watchdog, drawer, faults, summary_checked_at)
                        returned_home = await _recover(
                            robot, observation, assessment, store, policy
                        )
                        # Re-armed rather than left off: any robot after this
                        # one still gets a bounded routine pass.
                        routine.reschedule(
                            asyncio.get_running_loop().time() + _ROUTINE_BUDGET_SECONDS
                        )
                        if policy.armed:
                            if returned_home:
                                recoveries += 1
                            else:
                                watchdog.note_recovery_failed()
                        if watchdog.escalated:
                            _LOGGER.error(
                                "WATCHDOG_ESCALATED failures=%d; standing down until "
                                "a human intervenes at the unit.",
                                watchdog.consecutive_failures,
                            )
                # Skipped in an invocation that already drove the globe. Every
                # drift gate would refuse anyway -- `_blocked_by_drift` wants an
                # idle, healthy unit -- and the hourly cadence means nothing is
                # lost by waiting for the next one.
                if not (assessment.stuck and assessment.should_act) and _summary_due(
                    summary_checked_at, now, policy
                ):
                    summary_checked_at = now
                    rezero_attempts, rezero_recoveries = await _maybe_rezero(
                        account,
                        robot,
                        watchdog,
                        store,
                        policy,
                        routine,
                        now,
                        partial(
                            _save_state,
                            table,
                            robot_id,
                            watchdog,
                            drawer,
                            faults,
                            summary_checked_at,
                        ),
                    )
                    attempts += rezero_attempts
                    recoveries += rezero_recoveries
                _save_state(table, robot_id, watchdog, drawer, faults, summary_checked_at)
    except TimeoutError:
        # Either ceiling lands here: aiohttp raises a `TimeoutError` subclass
        # for a single stalled request, and the budget raises one for the pass
        # as a whole. Fail closed and say so -- the Errors alarm covers a
        # sustained stall, and this line is what tells the next reader that a
        # wait ran away rather than something genuinely breaking.
        _LOGGER.error(
            "WATCHDOG_TIMEOUT a routine read ran past its ceiling "
            "(invocation budget %.0fs); abandoning this invocation so the "
            "next schedule gets a clean run.",
            _ROUTINE_BUDGET_SECONDS,
        )
        raise
    finally:
        if account is not None:
            await account.disconnect()
        # `Session.close` deliberately leaves a caller-supplied session alone,
        # so closing this one is ours to do; a warm container would otherwise
        # accumulate a connector per invocation.
        await websession.close()
        if token_changed:
            secrets.update_secret(SecretId=secret_arn, SecretString=json.dumps(credentials))
        table.delete_item(
            Key={"robot_id": "CONTROL", "recorded_at": "LOCK"},
            ConditionExpression="lock_id = :lock_id",
            ExpressionAttributeValues={":lock_id": lock_id},
        )
    return {"attempts": attempts, "recoveries": recoveries}


def _holds_lease(table: Any, lock_id: str) -> bool:
    """Return whether the CONTROL lock already carries this invocation's id.

    `lock_id` is a fresh uuid4 per invocation, so a match can only be this
    invocation's own write coming back -- which is what distinguishes a retried
    conditional write from another invocation genuinely holding the lease.

    The read is consistent on purpose: an eventually-consistent one may not see
    the write being asked about, which is exactly the case this exists for.
    """
    item = table.get_item(
        Key={"robot_id": "CONTROL", "recorded_at": "LOCK"},
        ConsistentRead=True,
    ).get("Item")
    return isinstance(item, dict) and item.get("lock_id") == lock_id


def _account(
    token: Any,
    save_token: Callable[[dict[str, Any] | None], None],
    websession: ClientSession | None,
) -> Account:
    return Account(
        token=token,
        websession=websession,
        token_update_callback=save_token,
        robot_types=[LitterRobot4],
    )


async def _connect(
    credentials: dict[str, Any],
    save_token: Callable[[dict[str, Any] | None], None],
    websession: ClientSession | None = None,
) -> Account:
    """Connect to Whisker, re-authenticating when the stored token is unusable.

    The stored token is tried first: a password login is the heavier call and
    re-mints credentials, so it is not something to do once a minute.

    But the token path cannot recover itself. ``Account.connect`` takes the
    refresh branch whenever a refresh token is present and never falls back to
    a password on its own, and ``Session.get_user`` lets
    ``TokenVerificationException`` escape while catching only ``ClientError``
    and ``ParamValidationError``. So an ``id_token`` whose signature has
    expired takes down the whole invocation -- observed in production roughly
    every 13 hours between 2026-08-01 and 2026-08-03, each occurrence costing
    a minute of monitoring until the following schedule happened to succeed.

    Retrying once against a *tokenless* account forces the password branch,
    which re-mints the token through ``token_update_callback`` so later
    invocations go back to the cheap path.
    """
    if credentials.get("token"):
        account = _account(credentials["token"], save_token, websession)
        try:
            await account.connect(load_robots=True, subscribe_for_updates=False)
            return account
        except (TokenVerificationException, LitterRobotException, ClientError) as exc:
            # Only the exception *type*: these carry token material in their
            # messages, and nothing here may reach the log with a credential
            # in it.
            _LOGGER.warning(
                "Stored Whisker token unusable (%s); re-authenticating with the password.",
                type(exc).__name__,
            )
            await account.disconnect()

    account = _account(None, save_token, websession)
    try:
        await account.connect(
            username=str(credentials["username"]),
            password=str(credentials["password"]),
            load_robots=True,
            subscribe_for_updates=False,
        )
    except BaseException:
        # `_run_once` only binds its `account` from what this returns, so its
        # `finally` cannot unsubscribe one that never got returned. A wrong
        # password in the secret fails once a minute, so the leak would
        # accumulate rather than stay a one-off. (When `_run_once` supplies the
        # websession this leaves it open on purpose -- `Session.close` skips a
        # caller-supplied session, which is also what lets the token-to-password
        # retry above reuse it.)
        await account.disconnect()
        raise
    return account


def _save_state(
    table: Any,
    robot_id: str,
    watchdog: Watchdog,
    drawer: DrawerMonitor,
    faults: FaultMonitor,
    last_summary_check: float | None,
) -> None:
    """Persist the safety timers and monitor state that gate the next invocation."""
    item: dict[str, Any] = {
        "robot_id": robot_id,
        "recorded_at": "STATE",
        "watchdog": json.dumps(watchdog.snapshot(), separators=(",", ":")),
        # Shares the item so one write covers all three: these streaks and
        # latches are as meaningless per-invocation as the watchdog timers
        # are, and separate items could diverge from them.
        "drawer": json.dumps(drawer.snapshot(), separators=(",", ":")),
        "faults": json.dumps(faults.snapshot(), separators=(",", ":")),
        "updated_at": datetime.now(UTC).isoformat(),
    }
    if last_summary_check is not None:
        # `put_item` replaces the whole item, so every caller has to carry this
        # through -- dropping it on one of the mid-loop saves would re-open the
        # weekly poll on the very next invocation.
        item["last_summary_check"] = Decimal(str(round(last_summary_check, 3)))
    table.put_item(Item=item)


def _summary_due(last_check: float | None, now: float, policy: RecoveryPolicy) -> bool:
    """Whether the weekly weight summary is worth re-reading yet.

    The schedule fires once a minute against an aggregate that changes at most
    once a week. Without this gate the handler would spend 1,440 GraphQL reads
    a day watching a number that moves 52 times a year.
    """
    return last_check is None or now - last_check >= policy.rezero_poll_interval


async def _maybe_rezero(
    account: Account,
    robot: LitterRobot4,
    watchdog: Watchdog,
    store: InterventionStore,
    policy: RecoveryPolicy,
    routine: asyncio.Timeout,
    now: float,
    save_state: Callable[[], None],
) -> tuple[int, int]:
    """Run one proactive scale-drift check, returning (attempts, recoveries).

    The scheduled counterpart of the `rezero_poll_interval` branch in
    `autoreset._watch`. Every decision gate stays in `Watchdog.assess_drift`
    and every command stays in `_rezero_scale`; this only supplies the weekly
    read and the cross-invocation bookkeeping that the long-lived loop keeps
    in memory.
    """
    try:
        rows = await fetch_summary(account.session, robot.serial)
    except GraphQLQueryError as exc:
        # Same reasoning as the history backfill: the weekly summary is an
        # improvement to a slow-moving mitigation, not a safety signal, so a
        # denied or unavailable read must not interrupt monitoring.
        _LOGGER.warning("Weight summary unavailable: %s", exc)
        return 0, 0
    sample = _latest_weight_sample(rows, now)
    if sample is None:
        return 0, 0

    assessment = watchdog.assess_drift(sample)
    if not assessment.stuck:
        return 0, 0
    _record_drift(store, robot, "drift_assessment", sample, assessment, policy)
    log_scale_drift(assessment, sample, policy)
    if not assessment.should_act:
        return 0, 0

    # `_rezero_scale` waits `settle` between the two presses, which alone is
    # most of the routine budget. Suspending it keeps that budget a bound on
    # stalled reads rather than a cap on a wait that is meant to be long --
    # the same reasoning as the recovery dispatch in `_run_once`.
    routine.reschedule(None)
    # Persisted before dispatch, for the same reason as `note_attempt`: if the
    # invocation dies between here and the second press, a command has still
    # reached the device, and losing the record would leave the next
    # invocation with no re-zero cooldown and no failure count.
    watchdog.note_rezero_attempt(sample.week_start, at=now)
    save_state()
    succeeded = await _rezero_scale(robot, sample, assessment, store, policy)
    routine.reschedule(asyncio.get_running_loop().time() + _ROUTINE_BUDGET_SECONDS)

    if not (policy.armed and policy.rezero_armed):
        # Nothing was dispatched, so there is no outcome to judge; recording a
        # failure here would walk an unarmed deployment into escalation.
        return 1, 0
    if succeeded:
        watchdog.note_rezero_succeeded()
    else:
        watchdog.note_rezero_failed()
    if watchdog.rezero_escalated:
        _LOGGER.error(
            "WATCHDOG_REZERO_ESCALATED failures=%d; standing down on proactive "
            "re-zero until a human intervenes at the unit.",
            watchdog.rezero_consecutive_failures,
        )
    return 1, (1 if succeeded else 0)


def _drawer_policy() -> DrawerPolicy:
    """Build the drawer thresholds from the environment, defaults intact."""
    return DrawerPolicy(
        warn_percent=_float_env("DRAWER_WARN_PERCENT", DrawerPolicy.warn_percent),
        clear_percent=_float_env("DRAWER_CLEAR_PERCENT", DrawerPolicy.clear_percent),
        consecutive_samples=int(
            _float_env("DRAWER_CONSECUTIVE_SAMPLES", DrawerPolicy.consecutive_samples)
        ),
    )


def _fault_policy() -> FaultPolicy:
    """Build the fault persistence gate from the environment, defaults intact."""
    return FaultPolicy(
        consecutive_samples=int(
            _float_env("FAULT_CONSECUTIVE_SAMPLES", FaultPolicy.consecutive_samples)
        ),
    )


def _float_env(name: str, default: float) -> float:
    value = os.getenv(name)
    if not value:
        return default
    try:
        return float(value)
    except ValueError as exc:
        # A typo here would otherwise silently disable the warning by parsing
        # to something absurd, so refuse rather than guess.
        raise RuntimeError(f"{name} must be a number, got {value!r}") from exc


class _DynamoInterventionStore:
    """Small EventStore-compatible sink for redacted intervention records."""

    def __init__(self, table: Any, robot_id: str) -> None:
        self._table = table
        self._robot_id = robot_id

    def add(
        self,
        *,
        observed_at: str,
        source: str,
        robot_id: str,
        payload: Any,
        source_timestamp: str | None = None,
    ) -> bool:
        del robot_id, source_timestamp
        self._table.put_item(
            Item={
                "robot_id": self._robot_id,
                "recorded_at": f"EVENT#{observed_at}",
                "source": source,
                "payload": _dynamodb_value(payload),
                "expires_at": int(time()) + _CAPTURE_RETENTION_SECONDS,
            }
        )
        return True


class _DynamoCaptureStore:
    """Idempotent, redacted diagnostic capture alongside watchdog state."""

    def __init__(self, table: Any, robot_id: str) -> None:
        self._table = table
        self._robot_id = robot_id

    def cursor(self) -> _CaptureCursor:
        item = self._table.get_item(
            Key={"robot_id": self._robot_id, "recorded_at": "CAPTURE_CURSOR"}
        ).get("Item")
        if not isinstance(item, dict):
            return _CaptureCursor()
        epoch = item.get("activity_high_water_epoch")
        collected_at = item.get("collected_at")
        return _CaptureCursor(
            high_water_epoch=float(epoch) if isinstance(epoch, (int, float, Decimal)) else None,
            high_water_at=(
                item["activity_high_water_at"]
                if isinstance(item.get("activity_high_water_at"), str)
                else None
            ),
            collected_at=float(collected_at)
            if isinstance(collected_at, (int, float, Decimal))
            else None,
            history_initialized=item.get("history_initialized") is True,
        )

    def add_activity_rows(
        self,
        rows: list[dict[str, Any]],
        cursor: _CaptureCursor,
        *,
        observed_at: str,
    ) -> tuple[float | None, str | None]:
        """Store new activity plus a small overlap for delayed cloud records."""
        high_water_epoch = cursor.high_water_epoch
        high_water_at = cursor.high_water_at
        floor = (
            cursor.high_water_epoch - _CAPTURE_OVERLAP_SECONDS
            if cursor.high_water_epoch is not None
            else None
        )
        for row in rows:
            timestamp, epoch = _source_time(row)
            if floor is not None and epoch is not None and epoch < floor:
                continue
            self._add_row(
                "activity",
                row,
                observed_at=observed_at,
                source_timestamp=timestamp,
                key_timestamp=timestamp,
            )
            if epoch is not None and (high_water_epoch is None or epoch > high_water_epoch):
                high_water_epoch = epoch
                high_water_at = timestamp
        return high_water_epoch, high_water_at

    def add_rows(self, source: str, rows: list[dict[str, Any]], *, observed_at: str) -> None:
        """Store a bounded backfill using stable keys so retries are harmless."""
        for row in rows:
            timestamp, _ = _source_time(row)
            self._add_row(
                source,
                row,
                observed_at=observed_at,
                source_timestamp=timestamp,
                key_timestamp=timestamp,
            )

    def add_state(self, payload: dict[str, Any], *, observed_at: str) -> None:
        """Append a state snapshot even when the values have not changed."""
        self._add_row(
            "state",
            payload,
            observed_at=observed_at,
            source_timestamp=_optional_text(payload.get("lastSeen")),
            key_timestamp=observed_at,
        )

    def save_cursor(
        self,
        *,
        high_water_epoch: float | None,
        high_water_at: str | None,
        collected_at: float,
        history_initialized: bool,
    ) -> None:
        item: dict[str, Any] = {
            "robot_id": self._robot_id,
            "recorded_at": "CAPTURE_CURSOR",
            "collected_at": Decimal(str(collected_at)),
            "history_initialized": history_initialized,
            "updated_at": _iso_at(collected_at),
        }
        if high_water_epoch is not None:
            item["activity_high_water_epoch"] = Decimal(str(high_water_epoch))
        if high_water_at is not None:
            item["activity_high_water_at"] = high_water_at
        self._table.put_item(Item=item)

    def _add_row(
        self,
        source: str,
        payload: dict[str, Any],
        *,
        observed_at: str,
        source_timestamp: str | None,
        key_timestamp: str | None,
    ) -> None:
        redacted = redact_payload(payload)
        encoded = json.dumps(redacted, sort_keys=True, separators=(",", ":"), default=str)
        payload_hash = sha256(encoded.encode("utf-8")).hexdigest()
        # The source time and payload hash make retries overwrite precisely the
        # same item.  An unavailable source time still has a stable hash key.
        timestamp_key = key_timestamp or "unknown"
        self._table.put_item(
            Item={
                "robot_id": self._robot_id,
                "recorded_at": f"CAPTURE#{timestamp_key}#{source}#{payload_hash}",
                "source": source,
                "observed_at": observed_at,
                "source_timestamp": source_timestamp,
                "payload_hash": payload_hash,
                "payload": _dynamodb_value(redacted),
                "expires_at": int(time()) + _CAPTURE_RETENTION_SECONDS,
            }
        )


class _CaptureCursor:
    def __init__(
        self,
        *,
        high_water_epoch: float | None = None,
        high_water_at: str | None = None,
        collected_at: float | None = None,
        history_initialized: bool = False,
    ) -> None:
        self.high_water_epoch = high_water_epoch
        self.high_water_at = high_water_at
        self.collected_at = collected_at
        self.history_initialized = history_initialized


def _needs_history_backfill(cursor: _CaptureCursor, now: float) -> bool:
    """Backfill first use and a missed schedule without doing it every minute."""
    return (
        not cursor.history_initialized
        or cursor.collected_at is None
        or now - cursor.collected_at > _CAPTURE_GAP_SECONDS
    )


def _history_start(cursor: _CaptureCursor, now: float) -> str:
    if not cursor.history_initialized or cursor.high_water_epoch is None:
        start = datetime.fromtimestamp(now, UTC) - timedelta(days=_INITIAL_HISTORY_DAYS)
    else:
        start = datetime.fromtimestamp(cursor.high_water_epoch - _CAPTURE_OVERLAP_SECONDS, UTC)
    return start.isoformat()


def _source_time(row: dict[str, Any]) -> tuple[str | None, float | None]:
    timestamp = _optional_text(row.get("timestamp"))
    parsed = parse_timestamp(timestamp)
    return timestamp, None if parsed is None else parsed.timestamp()


def _optional_text(value: object) -> str | None:
    return None if value is None else str(value)


def _iso_at(value: float) -> str:
    return datetime.fromtimestamp(value, UTC).isoformat()


def _note_recent_cat_activity(
    watchdog: Watchdog, rows: list[dict[str, Any]], now: float, quiet_period: float
) -> None:
    for row in rows:
        if str(row.get("value")) not in CAT_ACTIVITY_VALUES:
            continue
        timestamp = parse_timestamp(row.get("timestamp"))
        if timestamp is None:
            # An unparseable cat marker must not open the gate.
            watchdog.note_cat_activity(at=now)
        else:
            occurred = timestamp.timestamp()
            if now - quiet_period <= occurred <= now + 60:
                watchdog.note_cat_activity(at=occurred)


def _load_credentials(client: Any, secret_arn: str) -> dict[str, Any]:
    value = client.get_secret_value(SecretId=secret_arn).get("SecretString")
    if not isinstance(value, str):
        raise RuntimeError("Whisker secret has no string value.")
    credentials = json.loads(value)
    if (
        not isinstance(credentials, dict)
        or not credentials.get("username")
        or not credentials.get("password")
    ):
        raise RuntimeError("Whisker secret must contain non-empty username and password values.")
    return credentials


def _dynamodb_value(value: Any) -> Any:
    """Convert JSON-compatible floats to DynamoDB's required Decimal values."""
    return json.loads(json.dumps(value, default=str), parse_float=Decimal)


def _required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required {name} environment variable.")
    return value
