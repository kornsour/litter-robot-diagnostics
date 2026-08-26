"""Tests for payload-to-sensor-column extraction."""

from __future__ import annotations

from lr4_diagnostics.sensors import (
    diagnostics_sample_fields,
    flag,
    number,
    state_sample_fields,
)


def test_number_rejects_booleans() -> None:
    """`True` is an int in Python; treating it as 1.0 would corrupt readings."""
    assert number(True) is None
    assert number(False) is None
    assert number(451) == 451.0
    assert number(2.7) == 2.7
    assert number("451") is None
    assert number(None) is None


def test_flag_only_accepts_booleans() -> None:
    assert flag(True) == 1
    assert flag(False) == 0
    assert flag(1) is None
    assert flag(None) is None


def test_state_sample_fields_extract_the_middle_tof_proxy() -> None:
    fields = state_sample_fields(
        {
            "litterLevel": 452,
            "litterLevelPercentage": 0.9,
            "weightSensor": 2.7,
            "catWeight": 10.81,
            "DFILevelPercent": 19,
            "isLaserDirty": False,
            "robotStatus": "ROBOT_CLEAN",
            "robotCycleState": "CYCLE_STATE_CAT_DETECT",
        }
    )
    assert fields["litter_level_mm"] == 452.0
    assert fields["weight_sensor"] == 2.7
    assert fields["is_laser_dirty"] == 0
    assert fields["cycle_state"] == "CYCLE_STATE_CAT_DETECT"


def test_state_sample_fields_tolerate_missing_keys() -> None:
    fields = state_sample_fields({})
    assert set(fields) == {
        "robot_status",
        "cycle_state",
        "cycle_status",
        "litter_level_mm",
        "litter_level_pct",
        "dfi_level_mm",
        "dfi_level_pct",
        "weight_sensor",
        "cat_weight",
        "is_laser_dirty",
    }
    assert all(value is None for value in fields.values())


def test_diagnostics_sample_fields_survive_withheld_tof_distances() -> None:
    """Whisker nulls the nested distances via field-level authorization."""
    fields = diagnostics_sample_fields(
        {
            "ToFSensorDistances": {
                "ToFSensorDistanceLeft": None,
                "ToFSensorDistanceMiddle": None,
                "ToFSensorDistanceRight": None,
            },
            "ToFSensorSlopes": {
                "ToFSensorSlopeLeft": 0,
                "ToFSensorSlopeMiddle": 2,
                "ToFSensorSlopeRight": 0,
            },
        }
    )
    assert fields["tof_left"] is None
    assert fields["tof_slope_middle"] == 2.0


def test_diagnostics_sample_fields_tolerate_absent_nesting() -> None:
    fields = diagnostics_sample_fields({"ToFSensorDistances": None})
    assert all(value is None for value in fields.values())
