"""Live LR4 state and periodic diagnostic capture."""

from __future__ import annotations

import asyncio
import copy
import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import monotonic
from typing import Any

from pylitterbot import Account, LitterRobot4
from pylitterbot.event import EVENT_UPDATE

from .auth import load_token, resolve_password, save_token
from .queries import (
    GraphQLQueryError,
    fetch_activity,
    fetch_firmware_comparison,
    fetch_history_download,
    fetch_insights,
    fetch_lifecycle,
    fetch_pet_profiles,
    fetch_pet_weight_history,
    fetch_summary,
    fetch_unit_diagnostics_result,
)
from .redact import pseudonym
from .sensors import diagnostics_sample_fields, state_sample_fields
from .store import EventStore
from .subscriptions import stream_activity

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class CaptureConfig:
    """Runtime settings for a capture session."""

    database: Path
    diagnostics_interval: float = 60.0
    activity_interval: float = 60.0
    lifecycle_interval: float = 300.0
    state_refresh_interval: float = 30.0
    pet_interval: float = 300.0
    extended_interval: float = 3600.0
    duration: float | None = None
    # Whisker serves roughly a 7-day activity window, but each request returns
    # at most `history_limit` rows. A limit of 100 reaches back only about a
    # day on an active unit, so infrequent captures would silently lose
    # history that the server still holds. Ask for the whole window instead.
    history_limit: int = 500

    def validate(self) -> None:
        """Reject intervals likely to overload the unofficial API."""
        for label, value in (
            ("diagnostics interval", self.diagnostics_interval),
            ("activity interval", self.activity_interval),
            ("lifecycle interval", self.lifecycle_interval),
            ("state refresh interval", self.state_refresh_interval),
        ):
            if value < 10:
                raise ValueError(f"{label} must be at least 10 seconds")
        if self.pet_interval < 60:
            raise ValueError("pet interval must be at least 60 seconds")
        if self.extended_interval < 300:
            raise ValueError("extended interval must be at least 300 seconds")
        if self.duration is not None and self.duration <= 0:
            raise ValueError("duration must be greater than zero")
        if self.history_limit < 1:
            raise ValueError("history limit must be greater than zero")


@dataclass(frozen=True)
class CaptureResult:
    """Summary returned when a capture session stops."""

    robots: int
    counts: dict[str, int]


async def run_capture(username: str, config: CaptureConfig) -> CaptureResult:
    """Capture owner-authorized LR4 diagnostics until cancelled or duration elapses."""
    config.validate()
    token = load_token(username)
    password = None if token else resolve_password(username)
    account = Account(
        token=token,
        token_update_callback=lambda value: save_token(username, value),
        robot_types=[LitterRobot4],
    )
    updates: asyncio.Queue[tuple[LitterRobot4, dict[str, Any]]] = asyncio.Queue()
    activity_updates: asyncio.Queue[tuple[LitterRobot4, dict[str, Any]]] = asyncio.Queue()
    unsubscribers: list[Any] = []
    activity_tasks: list[asyncio.Task[None]] = []

    with _isolated_botocore_configuration(), EventStore(config.database) as store:
        try:
            await account.connect(
                username=username if token is None else None,
                password=password,
                load_robots=True,
                subscribe_for_updates=True,
            )
            robots = [robot for robot in account.robots if isinstance(robot, LitterRobot4)]
            if not robots:
                raise RuntimeError("No Litter-Robot 4 was found on this Whisker account.")

            for robot in robots:
                _store_state(store, robot, robot.to_dict())

                def on_update(current: LitterRobot4 = robot) -> None:
                    updates.put_nowait((current, copy.deepcopy(current.to_dict())))

                unsubscribers.append(robot.on(EVENT_UPDATE, on_update))
                activity_tasks.append(
                    asyncio.create_task(
                        stream_activity(
                            account,
                            robot.serial,
                            lambda row, current=robot: activity_updates.put_nowait(
                                (current, copy.deepcopy(row))
                            ),
                        )
                    )
                )

            await _capture_loop(
                account,
                robots,
                updates,
                activity_updates,
                store,
                config,
            )
            return CaptureResult(robots=len(robots), counts=store.counts())
        finally:
            for task in activity_tasks:
                task.cancel()
            if activity_tasks:
                await asyncio.gather(*activity_tasks, return_exceptions=True)
            for unsubscribe in unsubscribers:
                unsubscribe()
            await account.disconnect()


@contextmanager
def _isolated_botocore_configuration() -> Iterator[None]:
    """Keep Cognito login independent from unrelated local AWS profiles."""
    overrides = {
        "AWS_CONFIG_FILE": os.devnull,
        "AWS_SHARED_CREDENTIALS_FILE": os.devnull,
        "AWS_EC2_METADATA_DISABLED": "true",
    }
    previous = {key: os.environ.get(key) for key in overrides}
    os.environ.update(overrides)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


async def _capture_loop(
    account: Account,
    robots: list[LitterRobot4],
    updates: asyncio.Queue[tuple[LitterRobot4, dict[str, Any]]],
    activity_updates: asyncio.Queue[tuple[LitterRobot4, dict[str, Any]]],
    store: EventStore,
    config: CaptureConfig,
) -> None:
    started = monotonic()
    next_diagnostics = started
    next_activity = started
    next_lifecycle = started
    next_state_refresh = started
    next_pet = started
    next_extended = started
    reported_errors: set[str] = set()

    while True:
        now = monotonic()
        if config.duration is not None and now - started >= config.duration:
            return

        next_due = min(
            next_diagnostics,
            next_activity,
            next_lifecycle,
            next_state_refresh,
            next_pet,
            next_extended,
        )
        timeout = max(0.0, min(next_due - now, 1.0))
        if config.duration is not None:
            timeout = min(timeout, max(0.0, config.duration - (now - started)))

        try:
            robot, payload = await asyncio.wait_for(updates.get(), timeout=timeout)
        except TimeoutError:
            pass
        else:
            _store_state(store, robot, payload)

        while not activity_updates.empty():
            robot, row = activity_updates.get_nowait()
            _store_activity_row(store, robot, row)

        now = monotonic()
        if now >= next_state_refresh:
            for robot in robots:
                await _capture_state_refresh(robot, store, reported_errors)
            next_state_refresh = now + config.state_refresh_interval
        if now >= next_diagnostics:
            for robot in robots:
                await _capture_diagnostics(account, robot, store, reported_errors)
            next_diagnostics = now + config.diagnostics_interval
        if now >= next_activity:
            for robot in robots:
                await _capture_rows(
                    source="activity",
                    account=account,
                    robot=robot,
                    store=store,
                    reported_errors=reported_errors,
                    limit=config.history_limit,
                )
            next_activity = now + config.activity_interval
        if now >= next_lifecycle:
            for robot in robots:
                await _capture_rows(
                    source="lifecycle",
                    account=account,
                    robot=robot,
                    store=store,
                    reported_errors=reported_errors,
                    limit=config.history_limit,
                )
            next_lifecycle = now + config.lifecycle_interval
        if now >= next_pet:
            await _capture_pet_weights(
                account,
                store,
                reported_errors,
                limit=max(config.history_limit, 1000),
            )
            next_pet = now + config.pet_interval
        if now >= next_extended:
            for robot in robots:
                await _capture_extended(
                    account,
                    robot,
                    store,
                    reported_errors,
                    limit=max(config.history_limit, 1000),
                )
            next_extended = now + config.extended_interval


async def _capture_state_refresh(
    robot: LitterRobot4,
    store: EventStore,
    reported_errors: set[str],
) -> None:
    try:
        await robot.refresh()
    except Exception as exc:
        _report_once("state-refresh", exc, reported_errors)
        return
    _store_state(store, robot, copy.deepcopy(robot.to_dict()))


async def _capture_diagnostics(
    account: Account,
    robot: LitterRobot4,
    store: EventStore,
    reported_errors: set[str],
) -> None:
    try:
        result = await fetch_unit_diagnostics_result(account.session, robot.serial)
    except GraphQLQueryError as exc:
        _report_once("diagnostics", exc, reported_errors)
        return
    payload = result.value
    if result.warnings:
        store.add(
            observed_at=_now(),
            source="api_warning",
            robot_id=pseudonym(robot.serial),
            payload={
                "query": "getUnitDiagnosticsBySerial",
                "warnings": [warning.to_dict() for warning in result.warnings],
            },
        )
    if payload is not None:
        observed_at = _now()
        robot_id = pseudonym(robot.serial)
        store.add(
            observed_at=observed_at,
            source="diagnostics",
            robot_id=robot_id,
            payload=payload,
        )
        store.add_sensor_sample(
            sampled_at=observed_at,
            robot_id=robot_id,
            source="diagnostics",
            **diagnostics_sample_fields(payload),
        )


async def _capture_rows(
    *,
    source: str,
    account: Account,
    robot: LitterRobot4,
    store: EventStore,
    reported_errors: set[str],
    limit: int,
) -> None:
    try:
        if source == "activity":
            rows = await fetch_activity(account.session, robot.serial, limit=limit)
        else:
            rows = await fetch_lifecycle(account.session, robot.serial, limit=limit)
    except GraphQLQueryError as exc:
        _report_once(source, exc, reported_errors)
        return
    for row in rows:
        if source == "activity":
            _store_activity_row(store, robot, row)
        else:
            store.add(
                observed_at=_now(),
                source=source,
                robot_id=pseudonym(robot.serial),
                source_timestamp=_optional_text(row.get("timestamp")),
                payload=row,
            )


async def _capture_pet_weights(
    account: Account,
    store: EventStore,
    reported_errors: set[str],
    *,
    limit: int,
) -> None:
    if account.user_id is None:
        _report_once(
            "pet-weight",
            RuntimeError("Whisker did not provide an owner identifier."),
            reported_errors,
        )
        return
    try:
        profiles = await fetch_pet_profiles(account.session, account.user_id)
    except GraphQLQueryError as exc:
        _report_once("pet-profile", exc, reported_errors)
        return

    for profile in profiles:
        pet_id = profile.get("petId")
        if not isinstance(pet_id, str):
            continue
        if error_type := profile.get("weightHistoryErrorType"):
            store.add(
                observed_at=_now(),
                source="api_warning",
                robot_id="lr4-unknown",
                payload={
                    "query": "getPetsByUser",
                    "petId": pet_id,
                    "message": str(error_type),
                },
            )
        try:
            rows = await fetch_pet_weight_history(
                account.session,
                pet_id,
                limit=limit,
            )
        except GraphQLQueryError as exc:
            _report_once("pet-weight", exc, reported_errors)
            continue
        for row in rows:
            serial = row.get("robotSerial")
            store.add(
                observed_at=_now(),
                source="pet_weight",
                robot_id=(
                    pseudonym(serial) if isinstance(serial, str) and serial else "lr4-unknown"
                ),
                source_timestamp=_optional_text(row.get("timestamp")),
                payload=row,
            )


async def _capture_extended(
    account: Account,
    robot: LitterRobot4,
    store: EventStore,
    reported_errors: set[str],
    *,
    limit: int,
) -> None:
    start_date = (datetime.now(UTC) - timedelta(days=35)).isoformat()
    robot_id = pseudonym(robot.serial)

    try:
        history = await fetch_history_download(
            account.session,
            robot.serial,
            start_date=start_date,
            limit=limit,
        )
    except GraphQLQueryError as exc:
        _report_once("history", exc, reported_errors)
    else:
        for row in history:
            store.add(
                observed_at=_now(),
                source="history",
                robot_id=robot_id,
                source_timestamp=_optional_text(row.get("timestamp")),
                payload=row,
            )

    try:
        firmware = await fetch_firmware_comparison(
            account.session,
            robot.serial,
        )
    except GraphQLQueryError as exc:
        _report_once("firmware", exc, reported_errors)
    else:
        if firmware is not None:
            store.add(
                observed_at=_now(),
                source="firmware",
                robot_id=robot_id,
                payload=firmware,
            )

    try:
        summary = await fetch_summary(account.session, robot.serial)
    except GraphQLQueryError as exc:
        _report_once("summary", exc, reported_errors)
    else:
        for row in summary:
            store.add(
                observed_at=_now(),
                source="summary",
                robot_id=robot_id,
                source_timestamp=_optional_text(row.get("weekEnd")),
                payload=row,
            )

    try:
        insights = await fetch_insights(
            account.session,
            robot.serial,
            start_date=start_date,
        )
    except GraphQLQueryError as exc:
        _report_once("insights", exc, reported_errors)
    else:
        if insights is not None:
            store.add(
                observed_at=_now(),
                source="insights",
                robot_id=robot_id,
                payload=insights,
            )


def _store_activity_row(
    store: EventStore,
    robot: LitterRobot4,
    row: dict[str, Any],
) -> None:
    store.add(
        observed_at=_now(),
        source="activity",
        robot_id=pseudonym(robot.serial),
        source_timestamp=_optional_text(row.get("timestamp")),
        payload=row,
    )


def _store_state(store: EventStore, robot: LitterRobot4, payload: dict[str, Any]) -> None:
    observed_at = _now()
    robot_id = pseudonym(robot.serial)
    store.add(
        observed_at=observed_at,
        source="state",
        robot_id=robot_id,
        source_timestamp=_optional_text(payload.get("lastSeen")),
        payload=payload,
    )
    store.add_sensor_sample(
        sampled_at=observed_at,
        robot_id=robot_id,
        source="state",
        **state_sample_fields(payload),
    )


def _report_once(source: str, error: Exception, reported: set[str]) -> None:
    fingerprint = f"{source}:{error}"
    if fingerprint in reported:
        return
    reported.add(fingerprint)
    _LOGGER.warning("%s capture unavailable: %s", source, error)


def _optional_text(value: object) -> str | None:
    return None if value is None else str(value)


def _now() -> str:
    return datetime.now(UTC).isoformat()
