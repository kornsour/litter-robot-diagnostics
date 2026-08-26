from __future__ import annotations

from decimal import Decimal
from typing import Any

from lr4_diagnostics.lambda_handler import (
    _CaptureCursor,
    _DynamoCaptureStore,
    _history_start,
    _needs_history_backfill,
)


class FakeDynamoTable:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict[str, Any]] = {}

    def get_item(self, *, Key: dict[str, str]) -> dict[str, Any]:  # noqa: N803 - AWS API spelling
        item = self.items.get((Key["robot_id"], Key["recorded_at"]))
        return {} if item is None else {"Item": item}

    def put_item(self, *, Item: dict[str, Any]) -> None:  # noqa: N803 - AWS API spelling
        self.items[(Item["robot_id"], Item["recorded_at"])] = Item


def test_capture_retries_overlap_without_creating_duplicate_activity_items() -> None:
    table = FakeDynamoTable()
    store = _DynamoCaptureStore(table, "lr4-test")
    initial = store.cursor()
    rows = [
        {
            "timestamp": "2026-07-30T00:00:00Z",
            "value": "robotStatusCatDetect",
            "serial": "LR4C-SECRET",
        },
        {
            "timestamp": "2026-07-30T00:01:00Z",
            "value": "robotCycleStatusDump",
        },
    ]

    high_water = store.add_activity_rows(rows, initial, observed_at="2026-07-30T00:01:01+00:00")
    store.save_cursor(
        high_water_epoch=high_water[0],
        high_water_at=high_water[1],
        collected_at=1_785_369_700.0,
        history_initialized=True,
    )

    reloaded = store.cursor()
    assert reloaded.high_water_at == "2026-07-30T00:01:00Z"
    assert reloaded.history_initialized
    repeated = store.add_activity_rows(rows, reloaded, observed_at="2026-07-30T00:02:01+00:00")

    activity_items = [item for item in table.items.values() if item.get("source") == "activity"]
    assert len(activity_items) == 2
    assert repeated == high_water
    assert activity_items[0]["payload"]["serial"].startswith("lr4-")
    assert "LR4C-SECRET" not in str(activity_items)


def test_capture_skips_activity_older_than_the_overlap_window() -> None:
    table = FakeDynamoTable()
    store = _DynamoCaptureStore(table, "lr4-test")
    cursor = _CaptureCursor(
        high_water_epoch=1_785_369_660.0,
        high_water_at="2026-07-30T00:01:00Z",
        collected_at=1_785_369_700.0,
        history_initialized=True,
    )

    high_water = store.add_activity_rows(
        [{"timestamp": "2026-07-29T23:50:00Z", "value": "old"}],
        cursor,
        observed_at="2026-07-30T00:02:01+00:00",
    )

    assert high_water == (cursor.high_water_epoch, cursor.high_water_at)
    assert not table.items


def test_capture_cursor_requests_backfill_on_first_run_or_schedule_gap() -> None:
    empty = _CaptureCursor()
    recent = _CaptureCursor(
        high_water_epoch=1_785_369_660.0,
        high_water_at="2026-07-30T00:01:00Z",
        collected_at=1_785_369_700.0,
        history_initialized=True,
    )
    missed = _CaptureCursor(
        high_water_epoch=1_785_369_660.0,
        high_water_at="2026-07-30T00:01:00Z",
        collected_at=1_785_369_600.0,
        history_initialized=True,
    )

    assert _needs_history_backfill(empty, 1_785_369_700.0)
    assert not _needs_history_backfill(recent, 1_785_369_760.0)
    assert _needs_history_backfill(missed, 1_785_369_900.0)
    assert _history_start(empty, 1_785_369_700.0).startswith("2026-06-25")
    assert _history_start(recent, 1_785_369_760.0).startswith("2026-07-29T23:56:00")


def test_cursor_reads_dynamodb_decimal_numbers() -> None:
    table = FakeDynamoTable()
    table.put_item(
        Item={
            "robot_id": "lr4-test",
            "recorded_at": "CAPTURE_CURSOR",
            "activity_high_water_epoch": Decimal("1785369660"),
            "activity_high_water_at": "2026-07-30T00:01:00Z",
            "collected_at": Decimal("1785369700"),
            "history_initialized": True,
        }
    )

    cursor = _DynamoCaptureStore(table, "lr4-test").cursor()

    assert cursor.high_water_epoch == 1_785_369_660.0
    assert cursor.collected_at == 1_785_369_700.0


def test_stuck_line_reaches_a_root_handler_left_at_warning() -> None:
    """The stuck alarm is a CloudWatch metric filter over the emitted line.

    The Lambda runtime attaches its handler to the root logger and leaves the
    level at WARNING, so importing the handler module has to raise the package
    logger itself.  Asserting through a real handler rather than `caplog` is
    the point: `caplog.at_level` forces the level, which is why this went
    unnoticed in production while the existing `log_stuck` test passed.
    """
    import io
    import logging

    from lr4_diagnostics.autoreset import Assessment, RecoveryPolicy, log_stuck

    buffer = io.StringIO()
    handler = logging.StreamHandler(buffer)
    root = logging.getLogger()
    original_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.WARNING)
    try:
        log_stuck(
            Assessment(stuck=True, reason="idle-latch", stuck_for=900.0, should_act=True),
            RecoveryPolicy(armed=False),
        )
    finally:
        root.removeHandler(handler)
        root.setLevel(original_level)

    assert "WATCHDOG_STUCK" in buffer.getvalue()


class FakeAccount:
    """Stands in for `pylitterbot.Account` around the connect/fallback path."""

    def __init__(self, fail: Exception | None = None) -> None:
        self.token: Any = None
        self.websession: Any = None
        self._fail = fail
        self.connect_kwargs: dict[str, Any] | None = None
        self.disconnected = False

    async def connect(self, **kwargs: Any) -> None:
        self.connect_kwargs = kwargs
        if self._fail is not None:
            raise self._fail

    async def disconnect(self) -> None:
        self.disconnected = True


def _patch_accounts(monkeypatch: Any, accounts: list[FakeAccount]) -> list[FakeAccount]:
    """Hand out `accounts` in order, recording what each was built with."""
    made: list[FakeAccount] = []

    def factory(token: Any, save_token: Any, websession: Any = None) -> FakeAccount:
        account = accounts[len(made)]
        account.token = token
        account.websession = websession
        made.append(account)
        return account

    monkeypatch.setattr("lr4_diagnostics.lambda_handler._account", factory)
    return made


def test_connect_prefers_the_stored_token() -> None:
    """A password login re-mints credentials; it must not happen once a minute."""
    import asyncio

    import pytest

    from lr4_diagnostics.lambda_handler import _connect

    monkeypatch = pytest.MonkeyPatch()
    good = FakeAccount()
    made = _patch_accounts(monkeypatch, [good])
    try:
        result = asyncio.run(
            _connect({"username": "u", "password": "p", "token": {"id_token": "x"}}, lambda _: None)
        )
    finally:
        monkeypatch.undo()

    assert result is good
    assert len(made) == 1
    assert good.connect_kwargs == {"load_robots": True, "subscribe_for_updates": False}
    assert not good.disconnected


def test_connect_falls_back_to_a_password_login_when_the_token_signature_expired() -> None:
    """The observed production failure: id_token expired, invocation lost.

    `Account.connect` takes the refresh branch whenever a refresh token is
    present and never falls back on its own, so the retry has to be against a
    *tokenless* account or it just fails the same way again.
    """
    import asyncio

    import pytest
    from pycognito.exceptions import TokenVerificationException

    from lr4_diagnostics.lambda_handler import _connect

    monkeypatch = pytest.MonkeyPatch()
    expired = FakeAccount(fail=TokenVerificationException("Signature has expired"))
    fresh = FakeAccount()
    made = _patch_accounts(monkeypatch, [expired, fresh])
    try:
        result = asyncio.run(
            _connect({"username": "u", "password": "p", "token": {"id_token": "x"}}, lambda _: None)
        )
    finally:
        monkeypatch.undo()

    assert result is fresh
    assert expired.disconnected, "the failed session must be closed, not leaked"
    assert made[0].token == {"id_token": "x"}
    assert made[1].token is None, "a retry carrying the token repeats the same failure"
    assert fresh.connect_kwargs == {
        "username": "u",
        "password": "p",
        "load_robots": True,
        "subscribe_for_updates": False,
    }


def test_connect_uses_the_password_when_no_token_is_stored() -> None:
    import asyncio

    import pytest

    from lr4_diagnostics.lambda_handler import _connect

    monkeypatch = pytest.MonkeyPatch()
    fresh = FakeAccount()
    made = _patch_accounts(monkeypatch, [fresh])
    try:
        result = asyncio.run(_connect({"username": "u", "password": "p"}, lambda _: None))
    finally:
        monkeypatch.undo()

    assert result is fresh
    assert len(made) == 1
    assert made[0].token is None


def test_token_failure_logs_the_type_but_never_the_credential() -> None:
    """Whisker tokens must not reach CloudWatch; only the exception type may."""
    import asyncio
    import io
    import logging

    import pytest
    from pycognito.exceptions import TokenVerificationException

    from lr4_diagnostics.lambda_handler import _connect

    secret = "hunter2-should-never-appear"
    monkeypatch = pytest.MonkeyPatch()
    _patch_accounts(
        monkeypatch, [FakeAccount(fail=TokenVerificationException(secret)), FakeAccount()]
    )
    buffer = io.StringIO()
    handler = logging.StreamHandler(buffer)
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        asyncio.run(
            _connect(
                {"username": "u", "password": secret, "token": {"id_token": secret}}, lambda _: None
            )
        )
    finally:
        root.removeHandler(handler)
        monkeypatch.undo()

    output = buffer.getvalue()
    assert "TokenVerificationException" in output
    assert secret not in output


def test_connect_closes_the_session_when_the_password_login_also_fails() -> None:
    """A failed login must not leak its session into the next warm invocation.

    `_run_once` binds its `account` from what `_connect` returns, so an
    account that never gets returned is invisible to the outer `finally`.
    A wrong password in the secret fails once a minute, so the leak would
    accumulate rather than stay a one-off.
    """
    import asyncio

    import pytest

    from lr4_diagnostics.lambda_handler import _connect

    monkeypatch = pytest.MonkeyPatch()
    doomed = FakeAccount(fail=RuntimeError("bad password"))
    _patch_accounts(monkeypatch, [doomed])
    try:
        with pytest.raises(RuntimeError):
            asyncio.run(_connect({"username": "u", "password": "p"}, lambda _: None))
    finally:
        monkeypatch.undo()

    assert doomed.disconnected, "the failed login's session must be closed before re-raising"
