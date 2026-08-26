"""Reconstruct clean cycles and attribute false cat-detect aborts to a sensor.

The LR4 reports two distinct "cat detected" signals, and conflating them hides
the fault:

* ``robotStatusCatDetect`` fires while the unit is idle and is driven by the
  scale (load cells). It starts the clean-cycle wait timer.
* ``robotCycleStateCatDetect`` fires *during* a cycle and aborts it. While the
  globe is rotating the scale is unusable, so this interlock is driven by the
  three-sensor ToF laser curtain on the laser board.

A cat re-entering mid-cycle produces aborts scattered across the cycle. A
failing laser curtain produces aborts clustered at the same point in the globe
rotation, because the beam is broken at a fixed rotation angle. The offset
distribution computed here separates those two cases.
"""

from __future__ import annotations

import json
import statistics
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import takewhile
from typing import Any

# Activity `value` markers, mapped by pylitterbot to LitterBoxStatus.
IDLE_CAT_DETECT = "robotStatusCatDetect"
CYCLE_ABORT = "robotCycleStateCatDetect"
CYCLE_START = "robotCycleStatusDump"
CYCLE_END = "robotCycleStatusIdle"
STUCK_LASER = "catDetectStuckLaser"

# Taking the bonnet off is the physical signature of a cleaning or reseating
# session, so it doubles as a maintenance marker to split before/after on.
MAINTENANCE = "bonnetRemovedYes"

# Aborts at or below this offset happened while the globe was still leaving
# home, which is far too early for a cat to have walked back in.
EARLY_ABORT_SECONDS = 60.0

# Below this many cycles on either side, a before/after abort-rate change is
# noise. Refusing to compare beats reporting a fix that has not been shown.
MIN_CYCLES_FOR_COMPARISON = 5

# A cat visit starts the clean-cycle wait timer, which defaults to 7 minutes.
# Well past that with no cycle means the unit still believes the cat is in the
# box: the at-rest cat-detect has latched. This is a separate failure from a
# mid-cycle abort and implicates the idle detection path, not the rotation.
STALL_THRESHOLD_MINUTES = 45.0

# pylitterbot documents `litterLevel` as a millimetre distance to the top-centre
# ToF sensor: ~441 full, ~451 nominal, ~461 low, ~471 very low.
LITTER_LEVEL_FULL_MM = 441.0
LITTER_LEVEL_NOMINAL_MM = 451.0
LITTER_LEVEL_LOW_MM = 461.0


@dataclass
class Cycle:
    """One clean cycle, from dump start to the return to idle."""

    start: datetime
    prior_visit: datetime | None = None
    aborts: list[datetime] = field(default_factory=list)
    end: datetime | None = None

    @property
    def duration_seconds(self) -> float | None:
        """Wall-clock length of the cycle, if it completed within the capture."""
        return None if self.end is None else (self.end - self.start).total_seconds()

    @property
    def wait_seconds(self) -> float | None:
        """Delay between the triggering cat visit and the cycle starting."""
        if self.prior_visit is None:
            return None
        return (self.start - self.prior_visit).total_seconds()

    @property
    def abort_offsets(self) -> list[float]:
        """Seconds from dump start to each mid-cycle abort."""
        return [(moment - self.start).total_seconds() for moment in self.aborts]


@dataclass
class Report:
    """Everything the analysis derived from one capture database."""

    cycles: list[Cycle]
    idle_visits: list[datetime]
    stuck_laser_events: list[datetime]
    samples: list[dict[str, Any]]
    maintenance_events: list[datetime] = field(default_factory=list)

    @property
    def last_maintenance(self) -> datetime | None:
        """When the bonnet last came off, if it did during the capture."""
        return max(self.maintenance_events) if self.maintenance_events else None

    @property
    def latest_event(self) -> datetime | None:
        """Newest observation in the capture, used as the reference for stalls.

        Sensor samples are stamped with this recorder's own clock, so they are
        included deliberately: when the unit has latched, the stalled visit is
        the newest *activity* row and comparing it against itself would report
        a zero-length stall, hiding the very failure being looked for.
        """
        moments = [*self.idle_visits, *self.maintenance_events]
        moments.extend(cycle.start for cycle in self.cycles)
        moments.extend(cycle.end for cycle in self.cycles if cycle.end is not None)
        moments.extend(
            moment
            for moment in (parse_timestamp(row.get("sampled_at")) for row in self.samples)
            if moment is not None
        )
        return max(moments) if moments else None

    @property
    def aborted(self) -> list[Cycle]:
        """Cycles that suffered at least one mid-cycle abort."""
        return [cycle for cycle in self.cycles if cycle.aborts]

    @property
    def total_aborts(self) -> int:
        """Count of mid-cycle aborts across the capture."""
        return sum(len(cycle.aborts) for cycle in self.cycles)

    @property
    def first_offsets(self) -> list[float]:
        """First abort offset for each aborted cycle."""
        return [cycle.abort_offsets[0] for cycle in self.aborted]

    @property
    def inter_abort_gaps(self) -> list[float]:
        """Seconds between consecutive aborts inside the same cycle."""
        gaps: list[float] = []
        for cycle in self.cycles:
            offsets = cycle.abort_offsets
            gaps.extend(second - first for first, second in zip(offsets, offsets[1:], strict=False))
        return gaps


@dataclass
class StalledVisit:
    """A cat visit the unit never followed with a clean cycle."""

    at: datetime
    delay_minutes: float
    resolved: bool


def parse_timestamp(value: object) -> datetime | None:
    """Parse a Whisker timestamp into an aware UTC datetime."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    # Activity rows carry nanosecond precision, which fromisoformat rejects.
    # Only the leading digit run is the fraction; anything after it (a UTC
    # offset such as "+00:00") must be preserved verbatim.
    if "." in text:
        head, _, tail = text.partition(".")
        fraction = "".join(takewhile(str.isdigit, tail))
        rest = tail[len(fraction) :]
        text = f"{head}.{fraction[:6] or '0'}{rest}"
    try:
        parsed = datetime.fromisoformat(text.replace(" ", "T"))
    except ValueError:
        return None
    # Whisker emits naive activity/lifecycle timestamps in UTC.
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def build_report(
    activity: Iterable[dict[str, Any]], samples: Iterable[dict[str, Any]] = ()
) -> Report:
    """Reconstruct cycles, aborts, and idle visits from raw activity rows."""
    events: list[tuple[datetime, str]] = []
    for row in activity:
        moment = parse_timestamp(row.get("timestamp"))
        value = row.get("value")
        if moment is not None and isinstance(value, str):
            events.append((moment, value))
    events.sort(key=lambda item: item[0])

    cycles: list[Cycle] = []
    idle_visits: list[datetime] = []
    stuck: list[datetime] = []
    maintenance: list[datetime] = []
    current: Cycle | None = None
    last_visit: datetime | None = None

    for moment, value in events:
        if value == IDLE_CAT_DETECT:
            idle_visits.append(moment)
            last_visit = moment
        elif value == MAINTENANCE:
            maintenance.append(moment)
        elif value == STUCK_LASER:
            stuck.append(moment)
        elif value == CYCLE_START:
            current = Cycle(start=moment, prior_visit=last_visit)
            cycles.append(current)
        elif value == CYCLE_ABORT and current is not None:
            current.aborts.append(moment)
        elif value == CYCLE_END and current is not None:
            current.end = moment
            current = None

    ordered = sorted(
        (row for row in samples if parse_timestamp(row.get("sampled_at")) is not None),
        key=lambda row: parse_timestamp(row["sampled_at"]) or datetime.min.replace(tzinfo=UTC),
    )
    return Report(
        cycles=cycles,
        idle_visits=idle_visits,
        stuck_laser_events=stuck,
        samples=ordered,
        maintenance_events=maintenance,
    )


def compare_around_maintenance(report: Report) -> dict[str, Any]:
    """Contrast abort rates before and after the last bonnet removal.

    Returns a `verdict` of "no-maintenance", "insufficient-data", "improved",
    "unchanged", or "worse". Anything short of enough cycles on both sides is
    reported as insufficient rather than guessed at.
    """
    moment = report.last_maintenance
    if moment is None:
        return {"verdict": "no-maintenance"}

    before = [cycle for cycle in report.cycles if cycle.start < moment]
    after = [cycle for cycle in report.cycles if cycle.start >= moment]
    result: dict[str, Any] = {
        "maintenance_at": moment.isoformat(),
        "cycles_before": len(before),
        "cycles_after": len(after),
        "aborts_before": sum(len(cycle.aborts) for cycle in before),
        "aborts_after": sum(len(cycle.aborts) for cycle in after),
    }
    if len(after) < MIN_CYCLES_FOR_COMPARISON or len(before) < MIN_CYCLES_FOR_COMPARISON:
        result["verdict"] = "insufficient-data"
        result["needed_cycles_after"] = max(0, MIN_CYCLES_FOR_COMPARISON - len(after))
        return result

    rate_before = result["aborts_before"] / len(before)
    rate_after = result["aborts_after"] / len(after)
    result["aborts_per_cycle_before"] = rate_before
    result["aborts_per_cycle_after"] = rate_after
    if rate_after <= rate_before * 0.5:
        result["verdict"] = "improved"
    elif rate_after >= rate_before * 1.5:
        result["verdict"] = "worse"
    else:
        result["verdict"] = "unchanged"
    return result


def stalled_visits(
    report: Report, threshold_minutes: float = STALL_THRESHOLD_MINUTES
) -> list[StalledVisit]:
    """Find cat visits that never produced a cycle within the threshold.

    An unresolved stall is measured against the newest event in the capture,
    so its delay is a lower bound rather than the final figure.
    """
    reference = report.latest_event
    starts = sorted(cycle.start for cycle in report.cycles)
    stalls: list[StalledVisit] = []
    for visit in report.idle_visits:
        following = [start for start in starts if start > visit]
        if following:
            delay = (following[0] - visit).total_seconds() / 60
            resolved = True
        elif reference is not None:
            delay = (reference - visit).total_seconds() / 60
            resolved = False
        else:
            continue
        if delay > threshold_minutes:
            stalls.append(StalledVisit(at=visit, delay_minutes=delay, resolved=resolved))
    return stalls


def _series(samples: Sequence[dict[str, Any]], key: str) -> list[float]:
    return [float(row[key]) for row in samples if isinstance(row.get(key), int | float)]


def _varies_with_status(samples: Sequence[dict[str, Any]], key: str) -> bool:
    """Whether a field ever changes, including across differing robot states.

    A field that holds one value even while `robot_status` moves through
    cat-detect is a tare or calibration constant, not a live reading. Treating
    its stillness as "stable therefore healthy" would manufacture a clean bill
    of health for a sensor that was never actually being observed.
    """
    values = {row[key] for row in samples if isinstance(row.get(key), int | float)}
    if len(values) > 1:
        return True
    statuses = {
        row.get("robot_status")
        for row in samples
        if isinstance(row.get(key), int | float) and row.get("robot_status")
    }
    # One value seen across several distinct states means it never responded.
    return len(statuses) < 2


def _at_rest(samples: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Samples taken while the robot was not running a cycle."""
    return [row for row in samples if row.get("robot_status") != "ROBOT_CLEAN"]


def _spread(values: Sequence[float]) -> dict[str, float] | None:
    if not values:
        return None
    return {
        "n": len(values),
        "min": min(values),
        "max": max(values),
        "median": statistics.median(values),
        "stdev": statistics.pstdev(values) if len(values) > 1 else 0.0,
    }


def sensor_findings(report: Report) -> dict[str, Any]:
    """Summarize what each sensor was doing across the capture."""
    samples = report.samples
    # `litterLevel` is frozen while the robot is cleaning: the firmware holds
    # the last at-rest reading rather than streaming the ToF distance, and
    # pylitterbot's calculate_litter_level mirrors that. Mid-cycle values are
    # therefore stale, so only at-rest samples say anything about the sensor.
    middle = _series(_at_rest(samples), "litter_level_mm")
    findings: dict[str, Any] = {
        "middle_tof_via_litter_level_mm": _spread(middle),
        "dfi_tof_percent": _spread(_series(samples, "dfi_level_pct")),
        "scale_weight_sensor": _spread(_series(samples, "weight_sensor")),
        "direct_tof_left": _spread(_series(samples, "tof_left")),
        "direct_tof_middle": _spread(_series(samples, "tof_middle")),
        "direct_tof_right": _spread(_series(samples, "tof_right")),
        "laser_dirty_flag_set": any(row.get("is_laser_dirty") for row in samples),
        "stuck_laser_faults": len(report.stuck_laser_events),
        "weight_sensor_is_live": _varies_with_status(samples, "weight_sensor"),
        "stalled_visits": len(stalled_visits(report)),
    }
    if middle:
        findings["litter_depth_verdict"] = _litter_verdict(statistics.median(middle))
    return findings


def _litter_verdict(median_mm: float) -> str:
    if median_mm <= LITTER_LEVEL_FULL_MM:
        return "at or above the full line - overfilled litter can break the laser plane"
    if median_mm <= LITTER_LEVEL_NOMINAL_MM:
        return "between nominal and full - normal, do not add litter"
    if median_mm <= LITTER_LEVEL_LOW_MM:
        return "between nominal and low - normal"
    return "below the low line - top up litter"


def attribute_fault(report: Report) -> list[str]:
    """Rank explanations for the observed faults, strongest evidence first."""
    verdicts: list[str] = []

    stalls = stalled_visits(report)
    if stalls:
        unresolved = [stall for stall in stalls if not stall.resolved]
        worst = max(stall.delay_minutes for stall in stalls)
        message = (
            f"IDLE LATCH: {len(stalls)}/{len(report.idle_visits)} cat visits did not start a "
            f"cycle within {STALL_THRESHOLD_MINUTES:.0f} min (worst {worst / 60:.1f}h). The unit "
            "still believed a cat was in the box. This is the AT-REST detection path, a "
            "separate failure from a mid-cycle abort."
        )
        if unresolved:
            message += (
                f" {len(unresolved)} is still unresolved as of the newest event "
                f"(>= {max(s.delay_minutes for s in unresolved) / 60:.1f}h and counting)."
            )
        verdicts.append(message)

    offsets = report.first_offsets
    if not offsets:
        verdicts.append("No mid-cycle aborts recorded.")
        return verdicts

    early = [value for value in offsets if value <= EARLY_ABORT_SECONDS]
    share = len(early) / len(offsets)
    median_offset = statistics.median(offsets)

    if share >= 0.7:
        verdicts.append(
            f"LASER CURTAIN (ToF): {len(early)}/{len(offsets)} aborted cycles trip within "
            f"{EARLY_ABORT_SECONDS:.0f}s of dump start (median {median_offset:.0f}s). "
            "Aborts locked to the same point in the globe rotation indicate a beam break, "
            "not a cat."
        )
    else:
        verdicts.append(
            f"INCONCLUSIVE timing: only {len(early)}/{len(offsets)} aborts are early "
            f"(median {median_offset:.0f}s). Spread-out aborts are consistent with a real cat."
        )

    findings = sensor_findings(report)
    scale = findings["scale_weight_sensor"]
    if scale and not findings["weight_sensor_is_live"]:
        verdicts.append(
            f"SCALE UNASSESSABLE: weightSensor never left {scale['median']:.2f} across "
            f"{scale['n']} samples spanning several robot states, including cat-detect. "
            "That makes it a tare/calibration constant, not a live load reading, so it "
            "can neither incriminate nor clear the scale."
        )
    elif scale and scale["stdev"] < 0.5:
        verdicts.append(
            f"SCALE steady: weightSensor varies but stays tight (stdev {scale['stdev']:.3f} "
            f"over {scale['n']} samples). Load-cell drift would wander more than this."
        )

    middle = findings["middle_tof_via_litter_level_mm"]
    if middle and middle["stdev"] < 3.0:
        verdicts.append(
            f"MIDDLE ToF healthy AT REST ONLY: top-centre distance is steady "
            f"({middle['min']:.0f}-{middle['max']:.0f}mm, stdev {middle['stdev']:.2f}) "
            f"across {middle['n']} idle samples. litterLevel is frozen during a "
            "cycle, so this says nothing about the centre sensor while the globe "
            "turns and cannot on its own shift blame to the left/right sensors."
        )
    elif middle:
        verdicts.append(
            f"MIDDLE ToF unstable: top-centre distance swings "
            f"{middle['min']:.0f}-{middle['max']:.0f}mm (stdev {middle['stdev']:.2f}). "
            "The centre sensor is a strong suspect."
        )

    dfi = findings["dfi_tof_percent"]
    if dfi and dfi["max"] - dfi["min"] >= 8:
        verdicts.append(
            f"ToF family noisy: drawer sensor ranges {dfi['min']:.0f}-{dfi['max']:.0f}% "
            "non-monotonically. A second optical sensor misbehaving points at a shared "
            "cause such as litter dust."
        )

    if not findings["laser_dirty_flag_set"] and not findings["stuck_laser_faults"]:
        verdicts.append(
            "Firmware self-checks pass at rest (isLaserDirty clear, no catDetectStuckLaser). "
            "The fault is intermittent and appears only under rotation."
        )

    if findings.get("direct_tof_left") is None:
        verdicts.append(
            "BLOCKED: ToFSensorDistanceLeft/Middle/Right are withheld by Whisker's "
            "field-level GraphQL authorization, so per-sensor attribution stays indirect."
        )
    return verdicts


def render(report: Report) -> str:
    """Render the human-readable analysis."""
    lines: list[str] = []
    cycles = report.cycles
    if not cycles:
        return "No clean cycles found in this capture."

    clean = [cycle for cycle in cycles if not cycle.aborts]
    lines.append("== Clean cycles ==")
    lines.append(f"{'start (UTC)':<21}{'wait':>8}{'aborts':>8}{'duration':>10}  abort offsets (s)")
    lines.append("-" * 78)
    for cycle in cycles:
        wait = cycle.wait_seconds
        duration = cycle.duration_seconds
        offsets = ", ".join(f"{value:.0f}" for value in cycle.abort_offsets)
        lines.append(
            f"{cycle.start.strftime('%Y-%m-%d %H:%M:%S'):<21}"
            f"{(f'{wait / 60:.1f}m' if wait is not None else '-'):>8}"
            f"{len(cycle.aborts):>8}"
            f"{(f'{duration / 60:.1f}m' if duration is not None else '-'):>10}"
            f"  [{offsets}]"
        )

    lines.append("")
    lines.append("== Totals ==")
    lines.append(f"cycles: {len(cycles)}   aborts: {report.total_aborts}")
    lines.append(f"abort-free cycles: {len(clean)}/{len(cycles)}")
    lines.append(f"idle cat visits (scale-triggered): {len(report.idle_visits)}")

    durations = [c.duration_seconds for c in cycles if c.duration_seconds is not None]
    clean_durations = [c.duration_seconds for c in clean if c.duration_seconds is not None]
    if durations:
        lines.append(f"median cycle duration: {statistics.median(durations) / 60:.1f}m")
    if clean_durations:
        lines.append(f"median abort-free duration: {statistics.median(clean_durations) / 60:.1f}m")

    if report.first_offsets:
        offsets = sorted(report.first_offsets)
        lines.append("")
        lines.append("== Abort timing ==")
        lines.append("first-abort offsets (s): " + ", ".join(f"{v:.0f}" for v in offsets))
        lines.append(f"median first-abort offset: {statistics.median(offsets):.0f}s")
        gaps = report.inter_abort_gaps
        if gaps:
            lines.append(f"median gap between retries: {statistics.median(gaps):.0f}s")

    stalls = stalled_visits(report)
    if stalls:
        lines.append("")
        lines.append("== Stalled cat visits (at-rest detection latched) ==")
        for stall in stalls:
            state = "resolved after" if stall.resolved else "STILL STUCK, >="
            lines.append(
                f"{stall.at.strftime('%Y-%m-%d %H:%M:%S')}  {state} {stall.delay_minutes / 60:.1f}h"
            )
        lines.append(f"{len(stalls)}/{len(report.idle_visits)} visits stalled")

    comparison = compare_around_maintenance(report)
    if comparison["verdict"] != "no-maintenance":
        lines.append("")
        lines.append("== Maintenance (bonnet removal) ==")
        lines.append(f"last bonnet removal: {comparison['maintenance_at']}")
        lines.append(
            f"before: {comparison['cycles_before']} cycles / "
            f"{comparison['aborts_before']} aborts     "
            f"after: {comparison['cycles_after']} cycles / "
            f"{comparison['aborts_after']} aborts"
        )
        if comparison["verdict"] == "insufficient-data":
            lines.append(
                f"VERDICT: insufficient data - need {comparison['needed_cycles_after']} "
                "more post-maintenance cycles before any change is meaningful."
            )
        else:
            lines.append(
                f"VERDICT: {comparison['verdict']} "
                f"({comparison['aborts_per_cycle_before']:.2f} -> "
                f"{comparison['aborts_per_cycle_after']:.2f} aborts/cycle)"
            )

    lines.append("")
    lines.append("== Sensor findings ==")
    for name, value in sensor_findings(report).items():
        lines.append(f"{name}: {_format_finding(value)}")

    lines.append("")
    lines.append("== Attribution ==")
    for index, verdict in enumerate(attribute_fault(report), start=1):
        lines.append(f"{index}. {verdict}")
    return "\n".join(lines)


def _format_finding(value: object) -> str:
    if value is None:
        return "no data"
    if isinstance(value, dict):
        return (
            f"n={value['n']} min={value['min']:.2f} max={value['max']:.2f} "
            f"median={value['median']:.2f} stdev={value['stdev']:.3f}"
        )
    return str(value)


def to_json(report: Report) -> str:
    """Render the analysis as JSON for downstream tooling."""
    payload = {
        "cycles": [
            {
                "start": cycle.start.isoformat(),
                "wait_seconds": cycle.wait_seconds,
                "duration_seconds": cycle.duration_seconds,
                "abort_offsets": cycle.abort_offsets,
            }
            for cycle in report.cycles
        ],
        "totals": {
            "cycles": len(report.cycles),
            "aborts": report.total_aborts,
            "abort_free_cycles": len(report.cycles) - len(report.aborted),
            "idle_cat_visits": len(report.idle_visits),
        },
        "sensor_findings": sensor_findings(report),
        "stalled_visits": [
            {
                "at": stall.at.isoformat(),
                "delay_minutes": stall.delay_minutes,
                "resolved": stall.resolved,
            }
            for stall in stalled_visits(report)
        ],
        "maintenance": compare_around_maintenance(report),
        "attribution": attribute_fault(report),
    }
    return json.dumps(payload, indent=2, sort_keys=True, default=str)
