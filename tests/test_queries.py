import asyncio
from typing import Any

import pytest

from lr4_diagnostics.queries import (
    GraphQLQueryError,
    fetch_activity,
    fetch_firmware_comparison,
    fetch_history_download,
    fetch_pet_profiles,
    fetch_pet_weight_history,
    fetch_unit_diagnostics,
    fetch_unit_diagnostics_result,
)


class FakeSession:
    def __init__(self, response: Any) -> None:
        self.response = response
        self.paths: list[str] = []
        self.requests: list[dict[str, Any]] = []

    async def post(self, path: str, **kwargs: Any) -> Any:
        self.paths.append(path)
        self.requests.append(kwargs)
        return self.response


def test_fetch_unit_diagnostics_is_read_only() -> None:
    session = FakeSession(
        {
            "data": {
                "getUnitDiagnosticsBySerial": {
                    "isScaleReady": True,
                    "globeMotorAmperes": 12,
                }
            }
        }
    )

    result = asyncio.run(fetch_unit_diagnostics(session, "LR4-TEST"))

    assert result == {"isScaleReady": True, "globeMotorAmperes": 12}
    query = session.requests[0]["json"]["query"]
    assert "query UnitDiagnostics" in query
    assert "mutation" not in query.lower()


def test_fetch_unit_diagnostics_keeps_partial_authorized_data() -> None:
    session = FakeSession(
        {
            "data": {
                "getUnitDiagnosticsBySerial": {
                    "isScaleReady": False,
                    "globeMotorAmperes": 17,
                    "ToFSensorDistances": {
                        "ToFSensorDistanceLeft": None,
                        "ToFSensorDistanceMiddle": None,
                        "ToFSensorDistanceRight": None,
                    },
                }
            },
            "errors": [
                {
                    "message": "Not Authorized to access ToFSensorDistanceLeft",
                    "path": [
                        "getUnitDiagnosticsBySerial",
                        "ToFSensorDistances",
                        "ToFSensorDistanceLeft",
                    ],
                }
            ],
        }
    )

    result = asyncio.run(fetch_unit_diagnostics(session, "LR4-TEST"))

    assert result is not None
    assert result["isScaleReady"] is False
    assert result["globeMotorAmperes"] == 17
    assert result["ToFSensorDistances"]["ToFSensorDistanceLeft"] is None


def test_fetch_unit_diagnostics_retains_sanitized_warning_path() -> None:
    session = FakeSession(
        {
            "data": {
                "getUnitDiagnosticsBySerial": {
                    "isScaleReady": True,
                    "ToFSensorDistances": {"ToFSensorDistanceLeft": None},
                }
            },
            "errors": [
                {
                    "message": "Not Authorized to access ToFSensorDistanceLeft",
                    "path": [
                        "getUnitDiagnosticsBySerial",
                        "ToFSensorDistances",
                        "ToFSensorDistanceLeft",
                    ],
                    "extensions": {"authorization": "must-not-be-persisted"},
                }
            ],
        }
    )

    result = asyncio.run(fetch_unit_diagnostics_result(session, "LR4-TEST"))

    assert result.value["isScaleReady"] is True
    assert [warning.to_dict() for warning in result.warnings] == [
        {
            "message": "Not Authorized to access ToFSensorDistanceLeft",
            "path": [
                "getUnitDiagnosticsBySerial",
                "ToFSensorDistances",
                "ToFSensorDistanceLeft",
            ],
        }
    ]
    assert "must-not-be-persisted" not in str(result.warnings)


def test_fetch_activity_preserves_raw_fields() -> None:
    row = {
        "timestamp": "2026-07-25T12:00:00Z",
        "value": "robotCycleStateCatDetect",
        "originalHex": "0x1234",
        "stateString": "CYCLE_STATE_CAT_DETECT",
    }
    session = FakeSession({"data": {"getLitterRobot4Activity": [row]}})

    assert asyncio.run(fetch_activity(session, "LR4-TEST")) == [row]


def test_fetch_history_download_unwraps_nested_rows() -> None:
    row = {
        "timestamp": "2026-07-01T12:00:00-04:00",
        "value": "CatDetectStuckWeight",
    }
    session = FakeSession({"data": {"robot": {"historyDownload": [row]}}})

    result = asyncio.run(
        fetch_history_download(
            session,
            "LR4-TEST",
            start_date="2026-06-25T00:00:00+00:00",
        )
    )

    assert result == [row]
    query = session.requests[0]["json"]["query"]
    assert "historyDownload" in query
    assert "mutation" not in query.lower()


def test_fetch_firmware_comparison_is_read_only() -> None:
    firmware = {
        "isEspFirmwareUpdateNeeded": False,
        "robotFirmware": {"espFirmware": "1.1.84"},
    }
    session = FakeSession({"data": {"litterRobot4CompareFirmwareVersion": firmware}})

    assert asyncio.run(fetch_firmware_comparison(session, "LR4-TEST")) == firmware
    assert "mutation" not in session.requests[0]["json"]["query"].lower()


def test_pet_queries_use_profile_endpoint_and_preserve_visit_fields() -> None:
    profile_session = FakeSession({"data": {"getPetsByUser": [{"petId": "pet-secret"}]}})
    profiles = asyncio.run(fetch_pet_profiles(profile_session, "user-secret"))
    assert profiles == [{"petId": "pet-secret"}]
    assert "pet-profile.iothings.site" in profile_session.paths[0]

    visit = {
        "petId": "pet-secret",
        "timestamp": "2026-07-25T12:00:00",
        "robotSerial": "LR4-TEST",
        "weight": 10.2,
        "status": None,
        "isReassigned": False,
    }
    history_session = FakeSession({"data": {"getWeightHistoryByPetId": [visit]}})
    assert asyncio.run(fetch_pet_weight_history(history_session, "pet-secret")) == [visit]
    query = history_session.requests[0]["json"]["query"]
    assert "robotSerial" in query
    assert "isReassigned" in query
    assert "mutation" not in query.lower()


def test_graphql_errors_are_reported_without_response_dump() -> None:
    session = FakeSession(
        {
            "errors": [
                {
                    "message": "Not authorized",
                    "extensions": {"authorization": "secret-token"},
                }
            ]
        }
    )

    with pytest.raises(GraphQLQueryError, match="Not authorized") as captured:
        asyncio.run(fetch_unit_diagnostics(session, "LR4-TEST"))

    assert "secret-token" not in str(captured.value)
