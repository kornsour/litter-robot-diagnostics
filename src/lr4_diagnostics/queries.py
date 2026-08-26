"""Read-only Whisker LR4 GraphQL queries."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

LR4_ENDPOINT = "https://lr4.iothings.site/graphql"
PET_PROFILE_ENDPOINT = "https://pet-profile.iothings.site/graphql/"


class Session(Protocol):
    """Subset of the pylitterbot session used by diagnostic queries."""

    async def post(self, path: str, **kwargs: Any) -> Any:
        """POST JSON and return the decoded response."""
        ...


class GraphQLQueryError(RuntimeError):
    """Raised for a rejected or malformed read-only GraphQL query."""


@dataclass(frozen=True)
class GraphQLWarning:
    """Sanitized field-level warning returned alongside authorized data."""

    message: str
    path: tuple[str | int, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Return a persistence-safe representation without response metadata."""
        value: dict[str, Any] = {"message": self.message}
        if self.path:
            value["path"] = list(self.path)
        return value


@dataclass(frozen=True)
class GraphQLResult:
    """One GraphQL value plus non-fatal field-level warnings."""

    value: Any
    warnings: tuple[GraphQLWarning, ...] = ()


UNIT_DIAGNOSTICS_QUERY = """
query UnitDiagnostics($serial: String!) {
  getUnitDiagnosticsBySerial(serial: $serial) {
    unitId serial userId RTCChipId
    mbHardware mbBom mbSuite mbRevision mbRevisionId mbDeviceId
    mbHardwareHex mbBomHex mbSuiteHex mbRevisionHex mbRevisionIdHex mbDeviceIdHex
    lbHardware lbBom lbSuite lbRevision lbRevisionId lbDeviceId
    lbHardwareHex lbBomHex lbSuiteHex lbRevisionHex lbRevisionIdHex lbDeviceIdHex
    isScaleReady DFIToFDistanceMax DFIToFDistanceMin systemPowerSensor
    ambientSensor bonnetSensor hallSensor globeMotorAmperes hopperMotorAmperes
    drawerSensor
    ToFSensorDistances {
      ToFSensorDistanceLeft ToFSensorDistanceMiddle ToFSensorDistanceRight
    }
    ToFSensorSlopes {
      ToFSensorSlopeLeft ToFSensorSlopeMiddle ToFSensorSlopeRight
    }
    globeRotationSpeed
    cycleTimerPending cycleTimerIdle cycleTimerDump cycleTimerDFI cycleTimerLevel
    cycleTimerHome cycleTimerEmpty cycleTimerEmptyHome cycleTimerAbort
    cycleTimerCatRelease cycleTimerCatRelDFI cycleTimerCatRelLevel
    cycleTimerFindDump cycleTimerComplete cycleTimerChangeFilter cycleTimerUnknown
    avgMotorAmpsDump avgMotorAmpsDFI avgMotorAmpsLevel avgMotorAmpsHome
    avgMotorAmpsUnknown motorFaultCycleTime motorFaultVoltage motorFaultAmperage
    motorFaultSlope motorFaultSpeed globeSpeedDumpToHall revisionId deviceId
    prmDisplayIntensityHigh prmDisplayIntensityLow
    prmAmbientLightSensorLimitsHigh prmAmbientLightSensorLimitsLow
  }
}
"""

ACTIVITY_QUERY = """
query Activity($serial: String!, $limit: Int) {
  getLitterRobot4Activity(serial: $serial, limit: $limit, consumer: "app") {
    serial measure timestamp value actionValue originalHex valueString stateString
    consumer commandSource
  }
}
"""

LIFECYCLE_QUERY = """
query Lifecycle($serial: String!, $limit: Int) {
  getLitterRobot4Lifecycle(serial: $serial, limit: $limit) {
    serial measure timestamp value sessionId clientInitiatedDisconnect
    disconnectReason traceId versionNumber principalIdentifier clientId
  }
}
"""

HISTORY_DOWNLOAD_QUERY = """
query HistoryDownload($serial: String!, $start: String!, $limit: Int) {
  robot(serial: $serial) {
    historyDownload(startDate: $start, limit: $limit) {
      timestamp value actionValue
    }
  }
}
"""

FIRMWARE_QUERY = """
query Firmware($serial: String!) {
  litterRobot4CompareFirmwareVersion(serial: $serial) {
    isEspFirmwareUpdateNeeded
    isPicFirmwareUpdateNeeded
    isLaserboardFirmwareUpdateNeeded
    latestFirmware {
      espFirmwareVersion picFirmwareVersion laserBoardFirmwareVersion
    }
    robotFirmware {
      espFirmware picFirmwareVersion laserBoardFirmwareVersion
    }
  }
}
"""

SUMMARY_QUERY = """
query Summary($serial: String!) {
  getLitterRobot4Summary(serial: $serial) {
    weekStart weekEnd numberOfCycles maxWeight minWeight numberOfCatDetections
  }
}
"""

INSIGHTS_QUERY = """
query Insights($serial: String!, $start: String!) {
  getLitterRobot4Insights(serial: $serial, startTimestamp: $start) {
    totalCycles averageCycles totalCatDetections
    cycleHistory { date numberOfCycles }
  }
}
"""

PET_PROFILES_QUERY = """
query PetProfiles($userId: String!) {
  getPetsByUser(userId: $userId) {
    petId
    weightHistoryErrorType
  }
}
"""

PET_WEIGHT_HISTORY_QUERY = """
query PetWeightHistory($petId: String!, $limit: Int) {
  getWeightHistoryByPetId(petId: $petId, limit: $limit) {
    petId timestamp robotSerial weight status isReassigned
  }
}
"""


async def fetch_unit_diagnostics(session: Session, serial: str) -> dict[str, Any] | None:
    """Fetch the latest detailed unit diagnostics, when owner access is allowed."""
    return (await fetch_unit_diagnostics_result(session, serial)).value


async def fetch_unit_diagnostics_result(session: Session, serial: str) -> GraphQLResult:
    """Fetch diagnostics while retaining sanitized field-level denials."""
    result = await _execute_result(
        session,
        UNIT_DIAGNOSTICS_QUERY,
        {"serial": serial},
        "getUnitDiagnosticsBySerial",
        allow_partial=True,
    )
    value = result.value
    if value is None:
        return result
    if not isinstance(value, dict):
        raise GraphQLQueryError("Unit diagnostics returned an unexpected shape.")
    return result


async def fetch_activity(
    session: Session, serial: str, *, limit: int = 100
) -> list[dict[str, Any]]:
    """Fetch raw activity rows without pylitterbot's lossy status conversion."""
    value = await _execute(
        session,
        ACTIVITY_QUERY,
        {"serial": serial, "limit": limit},
        "getLitterRobot4Activity",
    )
    return _dict_list(value, "Activity")


async def fetch_lifecycle(
    session: Session, serial: str, *, limit: int = 100
) -> list[dict[str, Any]]:
    """Fetch raw cloud-connectivity lifecycle rows."""
    value = await _execute(
        session,
        LIFECYCLE_QUERY,
        {"serial": serial, "limit": limit},
        "getLitterRobot4Lifecycle",
    )
    return _dict_list(value, "Lifecycle")


async def fetch_history_download(
    session: Session,
    serial: str,
    *,
    start_date: str,
    limit: int = 1000,
) -> list[dict[str, Any]]:
    """Fetch Whisker's longer, curated activity-history backfill."""
    robot = await _execute(
        session,
        HISTORY_DOWNLOAD_QUERY,
        {"serial": serial, "start": start_date, "limit": limit},
        "robot",
    )
    if robot is None:
        return []
    if not isinstance(robot, dict):
        raise GraphQLQueryError("History download returned an unexpected shape.")
    return _dict_list(robot.get("historyDownload"), "History download")


async def fetch_firmware_comparison(session: Session, serial: str) -> dict[str, Any] | None:
    """Fetch installed and currently offered firmware versions."""
    value = await _execute(
        session,
        FIRMWARE_QUERY,
        {"serial": serial},
        "litterRobot4CompareFirmwareVersion",
    )
    if value is None:
        return None
    if not isinstance(value, dict):
        raise GraphQLQueryError("Firmware comparison returned an unexpected shape.")
    return value


async def fetch_summary(session: Session, serial: str) -> list[dict[str, Any]]:
    """Fetch weekly cycle, cat-detection, and weight summaries."""
    value = await _execute(
        session,
        SUMMARY_QUERY,
        {"serial": serial},
        "getLitterRobot4Summary",
    )
    return _dict_list(value, "Summary")


async def fetch_insights(
    session: Session, serial: str, *, start_date: str
) -> dict[str, Any] | None:
    """Fetch cycle and cat-detection insight totals."""
    value = await _execute(
        session,
        INSIGHTS_QUERY,
        {"serial": serial, "start": start_date},
        "getLitterRobot4Insights",
    )
    if value is None:
        return None
    if not isinstance(value, dict):
        raise GraphQLQueryError("Insights returned an unexpected shape.")
    return value


async def fetch_pet_profiles(session: Session, user_id: str) -> list[dict[str, Any]]:
    """Fetch the owner's pet identifiers needed for SmartWeight history."""
    value = await _execute(
        session,
        PET_PROFILES_QUERY,
        {"userId": user_id},
        "getPetsByUser",
        endpoint=PET_PROFILE_ENDPOINT,
    )
    return _dict_list(value, "Pet profiles")


async def fetch_pet_weight_history(
    session: Session, pet_id: str, *, limit: int = 1000
) -> list[dict[str, Any]]:
    """Fetch detailed owner-authorized SmartWeight visit records."""
    value = await _execute(
        session,
        PET_WEIGHT_HISTORY_QUERY,
        {"petId": pet_id, "limit": limit},
        "getWeightHistoryByPetId",
        endpoint=PET_PROFILE_ENDPOINT,
    )
    return _dict_list(value, "Pet weight history")


async def _execute(
    session: Session,
    query: str,
    variables: dict[str, Any],
    result_field: str,
    *,
    allow_partial: bool = False,
    endpoint: str = LR4_ENDPOINT,
) -> Any:
    return (
        await _execute_result(
            session,
            query,
            variables,
            result_field,
            allow_partial=allow_partial,
            endpoint=endpoint,
        )
    ).value


async def _execute_result(
    session: Session,
    query: str,
    variables: dict[str, Any],
    result_field: str,
    *,
    allow_partial: bool = False,
    endpoint: str = LR4_ENDPOINT,
) -> GraphQLResult:
    result = await session.post(
        endpoint,
        json={"query": query, "variables": variables},
    )
    if not isinstance(result, dict):
        raise GraphQLQueryError("Whisker returned an unexpected response.")
    errors = result.get("errors")
    data = result.get("data")
    warnings = _graphql_warnings(errors)
    if errors and not (
        allow_partial and isinstance(data, dict) and data.get(result_field) is not None
    ):
        messages = [warning.message for warning in warnings]
        raise GraphQLQueryError("; ".join(messages) or "GraphQL query rejected.")
    if not isinstance(data, dict):
        raise GraphQLQueryError("Whisker returned no GraphQL data.")
    return GraphQLResult(value=data.get(result_field), warnings=warnings)


def _graphql_warnings(errors: object) -> tuple[GraphQLWarning, ...]:
    if not isinstance(errors, list):
        return ()
    warnings: list[GraphQLWarning] = []
    for item in errors:
        if not isinstance(item, dict):
            continue
        raw_path = item.get("path")
        path = (
            tuple(part for part in raw_path if isinstance(part, str | int))
            if isinstance(raw_path, list)
            else ()
        )
        warnings.append(
            GraphQLWarning(
                message=str(item.get("message", "GraphQL query warning")),
                path=path,
            )
        )
    return tuple(warnings)


def _dict_list(value: Any, label: str) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise GraphQLQueryError(f"{label} returned an unexpected shape.")
    return value
