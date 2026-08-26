"""Extract ToF, scale, and cycle scalars from raw LR4 payloads.

Shared by live capture and by backfill of already-recorded events so both
produce identical `sensor_samples` rows.
"""

from __future__ import annotations

from typing import Any


def number(value: object) -> float | None:
    """Coerce a JSON scalar to float, rejecting bools and non-numerics."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def flag(value: object) -> int | None:
    """Coerce a JSON boolean to 0/1, leaving anything else unknown."""
    return int(value) if isinstance(value, bool) else None


def state_sample_fields(payload: dict[str, Any]) -> dict[str, Any]:
    """Map a state push onto sensor columns.

    `litterLevel` is the millimetre distance reported by the top-centre ToF
    sensor, so it doubles as a live readout of the middle laser-curtain sensor
    even while Whisker withholds `ToFSensorDistanceMiddle`.
    """
    return {
        "robot_status": _text(payload.get("robotStatus")),
        "cycle_state": _text(payload.get("robotCycleState")),
        "cycle_status": _text(payload.get("robotCycleStatus")),
        "litter_level_mm": number(payload.get("litterLevel")),
        "litter_level_pct": number(payload.get("litterLevelPercentage")),
        "dfi_level_mm": number(payload.get("DFILevelMM")),
        "dfi_level_pct": number(payload.get("DFILevelPercent")),
        "weight_sensor": number(payload.get("weightSensor")),
        "cat_weight": number(payload.get("catWeight")),
        "is_laser_dirty": flag(payload.get("isLaserDirty")),
    }


def diagnostics_sample_fields(payload: dict[str, Any]) -> dict[str, Any]:
    """Map a unit-diagnostics payload onto the direct laser-curtain columns."""
    distances = payload.get("ToFSensorDistances")
    slopes = payload.get("ToFSensorSlopes")
    distances = distances if isinstance(distances, dict) else {}
    slopes = slopes if isinstance(slopes, dict) else {}
    return {
        "tof_left": number(distances.get("ToFSensorDistanceLeft")),
        "tof_middle": number(distances.get("ToFSensorDistanceMiddle")),
        "tof_right": number(distances.get("ToFSensorDistanceRight")),
        "tof_slope_left": number(slopes.get("ToFSensorSlopeLeft")),
        "tof_slope_middle": number(slopes.get("ToFSensorSlopeMiddle")),
        "tof_slope_right": number(slopes.get("ToFSensorSlopeRight")),
    }


def _text(value: object) -> str | None:
    return None if value is None else str(value)
