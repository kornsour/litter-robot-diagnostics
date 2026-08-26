"""Tests for cycle reconstruction and false cat-detect attribution."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from lr4_diagnostics.analysis import (
    attribute_fault,
    build_report,
    compare_around_maintenance,
    parse_timestamp,
    render,
    sensor_findings,
    stalled_visits,
    to_json,
)


def activity(timestamp: str, value: str) -> dict[str, Any]:
    """Build a minimal activity row."""
    return {"timestamp": timestamp, "value": value, "measure": "action"}


def test_parse_timestamp_handles_nanosecond_activity_rows() -> None:
    parsed = parse_timestamp("2026-07-25 23:17:22.000000000")
    assert parsed == datetime(2026, 7, 25, 23, 17, 22, tzinfo=UTC)


def test_parse_timestamp_handles_zulu_state_timestamps() -> None:
    parsed = parse_timestamp("2026-07-24T23:52:24.802Z")
    assert parsed == datetime(2026, 7, 24, 23, 52, 24, 802000, tzinfo=UTC)


def test_parse_timestamp_rejects_junk() -> None:
    assert parse_timestamp(None) is None
    assert parse_timestamp("") is None
    assert parse_timestamp("not-a-time") is None


def test_build_report_separates_idle_visits_from_mid_cycle_aborts() -> None:
    rows = [
        activity("2026-07-25 10:00:00.000000000", "robotStatusCatDetect"),
        activity("2026-07-25 10:07:00.000000000", "robotCycleStatusDump"),
        activity("2026-07-25 10:07:15.000000000", "robotCycleStateCatDetect"),
        activity("2026-07-25 10:07:45.000000000", "robotCycleStateCatDetect"),
        activity("2026-07-25 10:10:00.000000000", "robotCycleStatusIdle"),
    ]
    report = build_report(rows)

    assert len(report.cycles) == 1
    assert len(report.idle_visits) == 1
    cycle = report.cycles[0]
    assert cycle.abort_offsets == [15.0, 45.0]
    assert cycle.wait_seconds == 420.0
    assert cycle.duration_seconds == 180.0
    assert report.total_aborts == 2
    assert report.inter_abort_gaps == [30.0]


def test_build_report_orders_out_of_sequence_rows() -> None:
    """Whisker returns activity newest-first; ordering must not depend on input order."""
    rows = [
        activity("2026-07-25 10:10:00.000000000", "robotCycleStatusIdle"),
        activity("2026-07-25 10:07:15.000000000", "robotCycleStateCatDetect"),
        activity("2026-07-25 10:07:00.000000000", "robotCycleStatusDump"),
    ]
    report = build_report(rows)
    assert report.cycles[0].abort_offsets == [15.0]


def test_aborts_before_any_cycle_are_ignored() -> None:
    rows = [
        activity("2026-07-25 09:00:00.000000000", "robotCycleStateCatDetect"),
        activity("2026-07-25 10:07:00.000000000", "robotCycleStatusDump"),
    ]
    report = build_report(rows)
    assert report.total_aborts == 0


def test_unfinished_cycle_has_no_duration() -> None:
    rows = [activity("2026-07-25 10:07:00.000000000", "robotCycleStatusDump")]
    report = build_report(rows)
    assert report.cycles[0].duration_seconds is None


def early_abort_rows(count: int) -> list[dict[str, Any]]:
    """Build `count` cycles that each abort ~15s after the dump starts."""
    rows: list[dict[str, Any]] = []
    for index in range(count):
        rows.append(activity(f"2026-07-25 {index:02d}:00:00.000000000", "robotCycleStatusDump"))
        rows.append(activity(f"2026-07-25 {index:02d}:00:15.000000000", "robotCycleStateCatDetect"))
        rows.append(activity(f"2026-07-25 {index:02d}:05:00.000000000", "robotCycleStatusIdle"))
    return rows


def test_clustered_early_aborts_are_attributed_to_the_laser_curtain() -> None:
    report = build_report(early_abort_rows(5))
    verdict = to_json(report)
    assert "LASER CURTAIN" in verdict
    assert "INCONCLUSIVE" not in verdict


def test_scattered_aborts_are_not_blamed_on_the_laser_curtain() -> None:
    rows: list[dict[str, Any]] = []
    for index in range(5):
        rows.append(activity(f"2026-07-25 {index:02d}:00:00.000000000", "robotCycleStatusDump"))
        # Aborts land ten minutes in, which a real cat could plausibly cause.
        rows.append(activity(f"2026-07-25 {index:02d}:10:00.000000000", "robotCycleStateCatDetect"))
        rows.append(activity(f"2026-07-25 {index:02d}:20:00.000000000", "robotCycleStatusIdle"))
    verdict = to_json(build_report(rows))
    assert "INCONCLUSIVE" in verdict
    assert "LASER CURTAIN" not in verdict


def test_scale_is_never_declared_exonerated() -> None:
    """Superseded verdict: weightSensor proved to be a tare constant, not a load."""
    samples = [
        {"sampled_at": f"2026-07-25T0{index}:00:00+00:00", "weight_sensor": 2.7}
        for index in range(5)
    ]
    assert "exonerated" not in to_json(build_report(early_abort_rows(3), samples))


def test_steady_middle_tof_is_only_claimed_for_the_at_rest_case() -> None:
    """litterLevel is frozen mid-cycle, so it cannot clear the centre sensor."""
    samples = [
        {"sampled_at": f"2026-07-25T0{index}:00:00+00:00", "litter_level_mm": 451.0 + index % 2}
        for index in range(5)
    ]
    verdicts = to_json(build_report(early_abort_rows(3), samples))
    assert "AT REST ONLY" in verdicts
    assert "LEFT or RIGHT" not in verdicts


def test_mid_cycle_litter_level_is_excluded_from_the_middle_tof_spread() -> None:
    """Held mid-cycle values would otherwise fake a steady sensor."""
    samples = [
        {
            "sampled_at": "2026-07-25T01:00:00+00:00",
            "litter_level_mm": 451.0,
            "robot_status": "ROBOT_IDLE",
        },
        *[
            {
                "sampled_at": f"2026-07-25T02:00:0{index}+00:00",
                "litter_level_mm": 453.0,
                "robot_status": "ROBOT_CLEAN",
            }
            for index in range(5)
        ],
    ]
    findings = sensor_findings(build_report([], samples))
    assert findings["middle_tof_via_litter_level_mm"]["n"] == 1


def test_swinging_middle_tof_incriminates_the_centre_sensor() -> None:
    samples = [
        {"sampled_at": f"2026-07-25T0{index}:00:00+00:00", "litter_level_mm": 440.0 + index * 9}
        for index in range(5)
    ]
    assert "MIDDLE ToF unstable" in to_json(build_report(early_abort_rows(3), samples))


def test_litter_depth_verdict_tracks_the_firmware_scale() -> None:
    def verdict_for(distance_mm: float) -> str:
        samples = [{"sampled_at": "2026-07-25T00:00:00+00:00", "litter_level_mm": distance_mm}]
        return sensor_findings(build_report([], samples))["litter_depth_verdict"]

    assert "overfilled" in verdict_for(438.0)
    assert "do not add litter" in verdict_for(448.0)
    assert "normal" in verdict_for(455.0)
    assert "top up litter" in verdict_for(470.0)


def test_stuck_laser_faults_are_counted() -> None:
    rows = [activity("2026-07-25 10:00:00.000000000", "catDetectStuckLaser")]
    assert sensor_findings(build_report(rows))["stuck_laser_faults"] == 1


def test_no_maintenance_marker_yields_no_comparison() -> None:
    assert compare_around_maintenance(build_report(early_abort_rows(3))) == {
        "verdict": "no-maintenance"
    }


def test_maintenance_without_enough_after_cycles_refuses_to_judge() -> None:
    """The live case: bonnet reseated, but no cycles have run since."""
    rows = early_abort_rows(6)
    rows.append(activity("2026-07-25 23:00:00.000000000", "bonnetRemovedYes"))
    comparison = compare_around_maintenance(build_report(rows))

    assert comparison["verdict"] == "insufficient-data"
    assert comparison["cycles_before"] == 6
    assert comparison["cycles_after"] == 0
    assert comparison["needed_cycles_after"] == 5


def maintenance_rows(before: int, after: int, aborts_after: int) -> list[dict[str, Any]]:
    """Cycles that always abort, a bonnet removal, then cycles that mostly do not."""
    rows = early_abort_rows(before)
    rows.append(activity(f"2026-07-25 {before:02d}:30:00.000000000", "bonnetRemovedYes"))
    for index in range(after):
        hour = before + 1 + index
        rows.append(activity(f"2026-07-25 {hour:02d}:00:00.000000000", "robotCycleStatusDump"))
        if index < aborts_after:
            rows.append(
                activity(f"2026-07-25 {hour:02d}:00:15.000000000", "robotCycleStateCatDetect")
            )
        rows.append(activity(f"2026-07-25 {hour:02d}:05:00.000000000", "robotCycleStatusIdle"))
    return rows


def test_maintenance_that_clears_the_aborts_reads_as_improved() -> None:
    comparison = compare_around_maintenance(build_report(maintenance_rows(6, 6, 0)))
    assert comparison["verdict"] == "improved"
    assert comparison["aborts_per_cycle_before"] == 1.0
    assert comparison["aborts_per_cycle_after"] == 0.0


def test_maintenance_that_changes_nothing_reads_as_unchanged() -> None:
    comparison = compare_around_maintenance(build_report(maintenance_rows(6, 6, 6)))
    assert comparison["verdict"] == "unchanged"


def test_visit_followed_promptly_by_a_cycle_is_not_a_stall() -> None:
    rows = [
        activity("2026-07-25 10:00:00.000000000", "robotStatusCatDetect"),
        activity("2026-07-25 10:07:00.000000000", "robotCycleStatusDump"),
        activity("2026-07-25 10:10:00.000000000", "robotCycleStatusIdle"),
    ]
    assert stalled_visits(build_report(rows)) == []


def test_visit_with_a_late_cycle_is_a_resolved_stall() -> None:
    rows = [
        activity("2026-07-25 10:00:00.000000000", "robotStatusCatDetect"),
        activity("2026-07-25 13:00:00.000000000", "robotCycleStatusDump"),
        activity("2026-07-25 13:10:00.000000000", "robotCycleStatusIdle"),
    ]
    [stall] = stalled_visits(build_report(rows))
    assert stall.resolved is True
    assert stall.delay_minutes == 180.0


def test_visit_with_no_cycle_at_all_is_an_unresolved_stall() -> None:
    """The live failure: unit latched on cat-detect and never cycled.

    The stalled visit is the newest activity row, so the stall is only
    visible relative to a later sensor sample from this recorder's clock.
    """
    rows = [
        activity("2026-07-25 08:00:00.000000000", "robotCycleStatusDump"),
        activity("2026-07-25 08:03:00.000000000", "robotCycleStatusIdle"),
        activity("2026-07-25 10:00:00.000000000", "robotStatusCatDetect"),
    ]
    samples = [{"sampled_at": "2026-07-25T16:00:00+00:00", "robot_status": "ROBOT_IDLE"}]
    report = build_report(rows, samples)

    [stall] = stalled_visits(report)
    assert stall.resolved is False
    assert stall.delay_minutes == 360.0
    assert "IDLE LATCH" in " ".join(attribute_fault(report))


def test_ongoing_stall_is_invisible_without_a_later_observation() -> None:
    """Guards the regression: activity alone cannot date an open-ended stall."""
    rows = [activity("2026-07-25 10:00:00.000000000", "robotStatusCatDetect")]
    assert stalled_visits(build_report(rows)) == []


def test_constant_weight_sensor_is_not_treated_as_a_clean_bill_of_health() -> None:
    """A tare constant must not read as 'scale healthy'."""
    samples = [
        {
            "sampled_at": f"2026-07-25T0{index}:00:00+00:00",
            "weight_sensor": 2.7,
            "robot_status": status,
        }
        for index, status in enumerate(["ROBOT_IDLE", "ROBOT_CAT_DETECT", "ROBOT_CLEAN"])
    ]
    report = build_report(early_abort_rows(3), samples)
    assert sensor_findings(report)["weight_sensor_is_live"] is False
    verdicts = " ".join(attribute_fault(report))
    assert "SCALE UNASSESSABLE" in verdicts
    assert "exonerated" not in verdicts


def test_varying_weight_sensor_is_assessed_normally() -> None:
    samples = [
        {
            "sampled_at": f"2026-07-25T0{index}:00:00+00:00",
            "weight_sensor": 2.7 + index * 0.1,
            "robot_status": "ROBOT_IDLE",
        }
        for index in range(4)
    ]
    report = build_report(early_abort_rows(3), samples)
    assert sensor_findings(report)["weight_sensor_is_live"] is True
    assert "SCALE steady" in " ".join(attribute_fault(report))


def test_report_without_cycles_renders_a_clear_message() -> None:
    assert render(build_report([])) == "No clean cycles found in this capture."


def test_json_output_is_valid_and_carries_totals() -> None:
    payload = json.loads(to_json(build_report(early_abort_rows(3))))
    assert payload["totals"] == {
        "cycles": 3,
        "aborts": 3,
        "abort_free_cycles": 0,
        "idle_cat_visits": 0,
    }
    assert len(payload["cycles"]) == 3
