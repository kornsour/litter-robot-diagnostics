import asyncio
import os
from pathlib import Path
from typing import Any

import pytest

from lr4_diagnostics import capture
from lr4_diagnostics.capture import (
    CaptureConfig,
    _capture_diagnostics,
    _capture_extended,
    _capture_pet_weights,
    _isolated_botocore_configuration,
)
from lr4_diagnostics.queries import GraphQLResult, GraphQLWarning
from lr4_diagnostics.store import EventStore


class FakeAccount:
    user_id = "owner-secret"
    session = object()


class FakeRobot:
    serial = "LR4-SECRET"


def test_botocore_configuration_is_isolated_and_restored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AWS_CONFIG_FILE", "/original/config")
    monkeypatch.delenv("AWS_SHARED_CREDENTIALS_FILE", raising=False)
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "false")

    with _isolated_botocore_configuration():
        assert os.environ["AWS_CONFIG_FILE"] == os.devnull
        assert os.environ["AWS_SHARED_CREDENTIALS_FILE"] == os.devnull
        assert os.environ["AWS_EC2_METADATA_DISABLED"] == "true"

    assert os.environ["AWS_CONFIG_FILE"] == "/original/config"
    assert "AWS_SHARED_CREDENTIALS_FILE" not in os.environ
    assert os.environ["AWS_EC2_METADATA_DISABLED"] == "false"


def test_history_limit_defaults_to_the_full_server_window() -> None:
    """100 rows reaches back only ~1 day; Whisker holds about seven."""
    config = CaptureConfig(database=Path("unused.sqlite"))
    assert config.history_limit == 500
    config.validate()


def test_history_limit_must_be_positive() -> None:
    config = CaptureConfig(database=Path("unused.sqlite"), history_limit=0)
    with pytest.raises(ValueError, match="history limit"):
        config.validate()


def test_extended_intervals_reject_aggressive_polling() -> None:
    with pytest.raises(ValueError, match="pet interval"):
        CaptureConfig(database=Path("unused.sqlite"), pet_interval=30).validate()
    with pytest.raises(ValueError, match="extended interval"):
        CaptureConfig(
            database=Path("unused.sqlite"),
            extended_interval=60,
        ).validate()


def test_diagnostics_persists_partial_data_and_access_warnings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_diagnostics(session: object, serial: str) -> GraphQLResult:
        assert session is FakeAccount.session
        assert serial == FakeRobot.serial
        return GraphQLResult(
            value={
                "isScaleReady": True,
                "ToFSensorDistances": {"ToFSensorDistanceLeft": None},
            },
            warnings=(
                GraphQLWarning(
                    "Not Authorized to access ToFSensorDistanceLeft",
                    (
                        "getUnitDiagnosticsBySerial",
                        "ToFSensorDistances",
                        "ToFSensorDistanceLeft",
                    ),
                ),
            ),
        )

    monkeypatch.setattr(capture, "fetch_unit_diagnostics_result", fake_diagnostics)
    with EventStore(tmp_path / "capture.sqlite") as store:
        asyncio.run(
            _capture_diagnostics(
                FakeAccount(),  # type: ignore[arg-type]
                FakeRobot(),  # type: ignore[arg-type]
                store,
                set(),
            )
        )

        assert store.counts() == {"api_warning": 1, "diagnostics": 1}
        [warning] = list(store.payloads("api_warning"))
        assert warning["warnings"][0]["path"][-1] == "ToFSensorDistanceLeft"


def test_pet_weight_capture_redacts_pet_and_robot_identifiers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_profiles(session: object, user_id: str) -> list[dict[str, Any]]:
        assert session is FakeAccount.session
        assert user_id == FakeAccount.user_id
        return [{"petId": "pet-secret", "weightHistoryErrorType": None}]

    async def fake_history(session: object, pet_id: str, *, limit: int) -> list[dict[str, Any]]:
        assert session is FakeAccount.session
        assert pet_id == "pet-secret"
        assert limit == 1000
        return [
            {
                "petId": pet_id,
                "robotSerial": FakeRobot.serial,
                "timestamp": "2026-07-25T12:00:00",
                "weight": 10.2,
                "status": None,
                "isReassigned": False,
            }
        ]

    monkeypatch.setattr(capture, "fetch_pet_profiles", fake_profiles)
    monkeypatch.setattr(capture, "fetch_pet_weight_history", fake_history)
    with EventStore(tmp_path / "capture.sqlite") as store:
        asyncio.run(
            _capture_pet_weights(
                FakeAccount(),  # type: ignore[arg-type]
                store,
                set(),
                limit=1000,
            )
        )

        [record] = list(store.recent())
        assert record["source"] == "pet_weight"
        assert record["robot_id"].startswith("lr4-")
        assert record["payload"]["petId"].startswith("pet-")
        assert record["payload"]["robotSerial"].startswith("lr4-")
        assert "secret" not in str(record).lower()


def test_extended_capture_keeps_each_source_independent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_history(
        session: object,
        serial: str,
        *,
        start_date: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        del session, serial, start_date, limit
        return [
            {
                "timestamp": "2026-07-01T12:00:00-04:00",
                "value": "CatDetectStuckWeight",
            }
        ]

    async def fake_firmware(session: object, serial: str) -> dict[str, Any]:
        del session, serial
        return {"isEspFirmwareUpdateNeeded": False}

    async def fake_summary(session: object, serial: str) -> list[dict[str, Any]]:
        del session, serial
        return [{"weekEnd": "2026-07-27", "numberOfCycles": 12}]

    async def fake_insights(
        session: object,
        serial: str,
        *,
        start_date: str,
    ) -> dict[str, Any]:
        del session, serial, start_date
        return {"totalCycles": 12, "totalCatDetections": 8}

    monkeypatch.setattr(capture, "fetch_history_download", fake_history)
    monkeypatch.setattr(capture, "fetch_firmware_comparison", fake_firmware)
    monkeypatch.setattr(capture, "fetch_summary", fake_summary)
    monkeypatch.setattr(capture, "fetch_insights", fake_insights)
    with EventStore(tmp_path / "capture.sqlite") as store:
        asyncio.run(
            _capture_extended(
                FakeAccount(),  # type: ignore[arg-type]
                FakeRobot(),  # type: ignore[arg-type]
                store,
                set(),
                limit=1000,
            )
        )

        assert store.counts() == {
            "firmware": 1,
            "history": 1,
            "insights": 1,
            "summary": 1,
        }
