"""Command-line interface for the LR4 diagnostic recorder."""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

from .analysis import build_report, render, to_json
from .auth import (
    CredentialError,
    clear_credentials,
    resolve_username,
    store_password,
)
from .autoreset import AutoResetConfig, RecoveryPolicy, probe_reset_recovery, run_autoreset
from .capture import CaptureConfig, run_capture
from .store import EventStore

DEFAULT_DATABASE = Path("logs/lr4-diagnostics.sqlite")


def build_parser() -> argparse.ArgumentParser:
    """Construct the CLI parser."""
    parser = argparse.ArgumentParser(
        prog="lr4-diagnostics",
        description="Read-only local diagnostic capture for Whisker Litter-Robot 4.",
    )
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    auth = subparsers.add_parser("auth", help="Manage credentials in the OS keyring.")
    auth_subparsers = auth.add_subparsers(dest="auth_command", required=True)
    auth_store = auth_subparsers.add_parser("store", help="Store a Whisker password.")
    auth_store.add_argument("--username")
    auth_clear = auth_subparsers.add_parser("clear", help="Remove password and API tokens.")
    auth_clear.add_argument("--username")

    capture = subparsers.add_parser("capture", help="Start live diagnostic recording.")
    capture.add_argument("--username")
    capture.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    capture.add_argument("--diagnostics-interval", type=float, default=60.0)
    capture.add_argument("--activity-interval", type=float, default=60.0)
    capture.add_argument("--lifecycle-interval", type=float, default=300.0)
    capture.add_argument(
        "--state-refresh-interval",
        type=float,
        default=30.0,
        help="Seconds between direct read-only state snapshots.",
    )
    capture.add_argument(
        "--pet-interval",
        type=float,
        default=300.0,
        help="Seconds between SmartWeight pet-history refreshes.",
    )
    capture.add_argument(
        "--extended-interval",
        type=float,
        default=3600.0,
        help="Seconds between history, firmware, summary, and insight refreshes.",
    )
    capture.add_argument(
        "--history-limit",
        type=int,
        default=500,
        help=(
            "Rows requested per activity/lifecycle poll. Whisker serves about a "
            "7-day window; a small limit reaches back only ~1 day on an active "
            "unit and silently loses older history."
        ),
    )
    capture.add_argument(
        "--duration",
        type=float,
        help="Stop automatically after this many seconds; useful for smoke tests.",
    )

    autoreset = subparsers.add_parser(
        "autoreset",
        help="Watch for stuck cat-sensor states and recover the unit. WRITES TO THE DEVICE.",
        description=(
            "Detect the >30-minute scale latch and stalled clean cycles, then "
            "recover the unit with a short Reset press followed by a Cycle "
            "press. Detection-only unless --arm is passed."
        ),
    )
    autoreset.add_argument("--username")
    autoreset.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    autoreset.add_argument(
        "--arm",
        action="store_true",
        help=(
            "Actually dispatch commands. Without this the watchdog only logs "
            "and records what it would have done. A reset can start the globe "
            "turning, so read the safety notes before arming."
        ),
    )
    autoreset.add_argument(
        "--arm-rezero",
        action="store_true",
        help=(
            "Enable the proactive scale re-zero (double Reset from an idle "
            "unit when weekly maxWeight exceeds --rezero-weight-ceiling). "
            "Has no effect unless --arm is also passed. Separate from --arm "
            "because, unlike every other command this watchdog sends, the "
            "idle double-press has never been independently verified via the "
            "API -- see the autoreset module docstring."
        ),
    )
    autoreset.add_argument(
        "--rezero-weight-ceiling",
        type=float,
        default=RecoveryPolicy.rezero_weight_ceiling,
        help=(
            "Weekly maxWeight (lb) at or above which drift is assumed rather "
            "than a heavy cat (default: %(default)s)."
        ),
    )
    autoreset.add_argument(
        "--rezero-poll-interval",
        type=float,
        default=RecoveryPolicy.rezero_poll_interval,
        help="Seconds between weekly weight-summary checks.",
    )
    autoreset.add_argument(
        "--rezero-cooldown",
        type=float,
        default=RecoveryPolicy.rezero_cooldown,
        help="Minimum seconds between proactive re-zero attempts.",
    )
    autoreset.add_argument(
        "--latch-grace",
        type=float,
        default=RecoveryPolicy.latch_grace,
        help=(
            "Seconds to wait after the unit reports the >30-minute latch "
            "before intervening (default: %(default)s)."
        ),
    )
    autoreset.add_argument(
        "--stall-grace",
        type=float,
        default=RecoveryPolicy.stall_grace,
        help="Seconds a cycle may sit on one state before it counts as stalled.",
    )
    autoreset.add_argument(
        "--max-cycle",
        type=float,
        default=RecoveryPolicy.max_cycle_seconds,
        help=(
            "Ceiling on total cycle duration in seconds; catches a cycle that "
            "keeps retrying without ever finishing."
        ),
    )
    autoreset.add_argument(
        "--quiet-period",
        type=float,
        default=RecoveryPolicy.quiet_period,
        help="Seconds of activity-stream silence required before acting.",
    )
    autoreset.add_argument(
        "--cooldown",
        type=float,
        default=RecoveryPolicy.cooldown,
        help="Minimum seconds between recovery attempts.",
    )
    autoreset.add_argument(
        "--max-per-hour",
        type=int,
        default=RecoveryPolicy.max_per_hour,
        help="Hard cap on recovery attempts per rolling hour.",
    )
    autoreset.add_argument(
        "--max-consecutive-failures",
        type=int,
        default=RecoveryPolicy.max_consecutive_failures,
        help="Failed recoveries in a row before the watchdog stands down.",
    )
    autoreset.add_argument(
        "--recovery-hold",
        type=float,
        default=RecoveryPolicy.recovery_hold,
        help=(
            "Seconds the unit must stay healthy after a recovery before that "
            "recovery counts as successful; reaching home once is not enough."
        ),
    )
    autoreset.add_argument(
        "--tof-clear-floor",
        type=float,
        default=RecoveryPolicy.tof_clear_floor,
        help=(
            "Minimum top-centre ToF distance in mm before an idle latch may be "
            "reset; guards against resetting with an object in the globe."
        ),
    )
    autoreset.add_argument(
        "--skip-tof-check",
        action="store_true",
        help="Disable the ToF clearance check. Removes a safety gate.",
    )
    autoreset.add_argument(
        "--poll-interval",
        type=float,
        default=RecoveryPolicy.poll_interval,
        help="Seconds between read-only state polls.",
    )
    autoreset.add_argument(
        "--duration",
        type=float,
        help="Stop automatically after this many seconds; useful for smoke tests.",
    )

    probe = subparsers.add_parser(
        "probe-reset",
        help="Test whether a Reset alone brings a stalled globe home. WRITES TO THE DEVICE.",
        description=(
            "Supervised, one-shot experiment. Starts a clean cycle, interrupts "
            "it once the globe is away from home with a short Reset press, and "
            "records whether the globe returns home on its own or needs a Cycle "
            "press afterwards. THE GLOBE MUST BE EMPTY AND YOU MUST BE WATCHING "
            "THE UNIT. Refuses to run unless armed, the bonnet is on, the unit "
            "is idle at home, and the ToF floor is clear."
        ),
    )
    probe.add_argument("--username")
    probe.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    probe.add_argument(
        "--arm",
        action="store_true",
        help="Required. The probe rotates the globe deliberately.",
    )
    probe.add_argument(
        "--tof-clear-floor",
        type=float,
        default=RecoveryPolicy.tof_clear_floor,
        help="Minimum top-centre ToF distance in mm required before starting.",
    )

    summary = subparsers.add_parser("summary", help="Summarize a local capture database.")
    summary.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    summary.add_argument("--limit", type=int, default=10)

    analyze = subparsers.add_parser(
        "analyze",
        help="Reconstruct clean cycles and attribute false cat-detect aborts.",
    )
    analyze.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    analyze.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    return parser


#: Libraries that log full HTTP request and response bodies at DEBUG. The
#: Cognito auth exchange carries refresh tokens, access tokens, and an ID token
#: with the owner's name and email, so these must never reach a debug handler.
#: Pinned explicitly rather than left to inherit: a library is free to set its
#: own level, and this one is not a rule worth leaving to another package.
_WIRE_LOGGERS = ("boto3", "botocore", "urllib3", "pylitterbot", "aiohttp", "gql", "websockets")


def _configure_logging(verbose: bool) -> None:
    """Route logging so ``--verbose`` never turns into a credential dump.

    ``--verbose`` raises the level for *this package only*. The root logger
    stays at WARNING, so third-party debug logging is off by default instead of
    opted out of by name; the explicit pins above are the second layer.

    Our own records still reach the console: a child logger's records propagate
    to the root handler regardless of the root logger's level.
    """
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger(__package__).setLevel(logging.DEBUG if verbose else logging.INFO)
    for name in _WIRE_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


def _line_buffer(stream: object) -> None:
    """Stop operational announcements from sitting in a buffer.

    Python block-buffers stdout whenever it is not a TTY, so under ``> file``,
    a pipe, or a Lambda log driver the banners print *last* — flushed at exit —
    rather than first. That is backwards for the one line that says whether the
    watchdog is armed: "ARMED — commands will be sent" is what an operator
    reads before deciding whether to let the thing run, and it is worthless if
    it only appears once the run is over.

    Logging is unaffected either way; it goes to stderr, which is why the log
    lines showed up promptly while the banner did not.

    Takes the stream rather than reaching for `sys.stdout` so this is testable,
    and no-ops on anything that is not a real text stream — pytest's `capsys`
    swaps in a substitute that has no `reconfigure`.
    """
    if isinstance(stream, io.TextIOWrapper):
        stream.reconfigure(line_buffering=True)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command-line application."""
    parser = build_parser()
    args = parser.parse_args(argv)
    _line_buffer(sys.stdout)
    _configure_logging(args.verbose)

    try:
        if args.command == "auth":
            username = resolve_username(args.username)
            if args.auth_command == "store":
                store_password(username)
                print(f"Stored Whisker credentials for {username} in the OS keyring.")
            else:
                clear_credentials(username)
                print(f"Removed stored Whisker credentials for {username}.")
            return 0

        if args.command == "summary":
            return _summary(args.database, args.limit)

        if args.command == "analyze":
            return _analyze(args.database, args.json)

        if args.command == "autoreset":
            return _autoreset(args)
        if args.command == "probe-reset":
            return _probe_reset(args)

        username = resolve_username(args.username)
        config = CaptureConfig(
            database=args.database,
            diagnostics_interval=args.diagnostics_interval,
            activity_interval=args.activity_interval,
            lifecycle_interval=args.lifecycle_interval,
            state_refresh_interval=args.state_refresh_interval,
            pet_interval=args.pet_interval,
            extended_interval=args.extended_interval,
            duration=args.duration,
            history_limit=args.history_limit,
        )
        print(f"Recording redacted LR4 diagnostics to {config.database}. Press Ctrl-C to stop.")
        result = asyncio.run(run_capture(username, config))
        print(f"Capture stopped. Robots: {result.robots}; records: {result.counts}")
        return 0
    except KeyboardInterrupt:
        print("\nCapture stopped.")
        return 130
    except (CredentialError, RuntimeError, ValueError) as exc:
        parser.error(str(exc))
    return 2


def _probe_reset(args: argparse.Namespace) -> int:
    username = resolve_username(args.username)
    policy = RecoveryPolicy(tof_clear_floor=args.tof_clear_floor, armed=args.arm)
    config = AutoResetConfig(database=args.database, policy=policy)
    print(
        "This rotates the globe on purpose. Confirm the globe is empty and stay "
        "at the unit while it runs."
    )
    result = asyncio.run(probe_reset_recovery(username, config))
    print()
    print(result.summary())
    print(f"  reset alone returned home : {result.reset_alone_returned_home}")
    print(f"  first reset paused globe  : {result.paused_by_first_reset}")
    print(f"  second reset resumed it   : {result.resumed_by_second_reset}")
    print(f"  unit recovered            : {result.recovered}")
    print(f"  seconds to home           : {result.seconds_to_home}")
    return 0 if result.recovered else 1


def _autoreset(args: argparse.Namespace) -> int:
    username = resolve_username(args.username)
    policy = RecoveryPolicy(
        latch_grace=args.latch_grace,
        stall_grace=args.stall_grace,
        max_cycle_seconds=args.max_cycle,
        quiet_period=args.quiet_period,
        cooldown=args.cooldown,
        max_per_hour=args.max_per_hour,
        max_consecutive_failures=args.max_consecutive_failures,
        recovery_hold=args.recovery_hold,
        tof_clear_floor=args.tof_clear_floor,
        require_clear_tof=not args.skip_tof_check,
        poll_interval=args.poll_interval,
        rezero_weight_ceiling=args.rezero_weight_ceiling,
        rezero_poll_interval=args.rezero_poll_interval,
        rezero_cooldown=args.rezero_cooldown,
        armed=args.arm,
        rezero_armed=args.arm_rezero,
        duration=args.duration,
    )
    config = AutoResetConfig(database=args.database, policy=policy)
    mode = "ARMED — commands will be sent" if policy.armed else "detection only"
    if policy.armed and policy.rezero_armed:
        mode += "; proactive re-zero ARMED"
    print(f"Watching for stuck cat-sensor states ({mode}). Press Ctrl-C to stop.")
    result = asyncio.run(run_autoreset(username, config))
    print(
        f"Watchdog stopped. Robots: {result.robots}; "
        f"attempts: {result.attempts}; recoveries: {result.recoveries}"
    )
    return 0


def _analyze(database: Path, as_json: bool) -> int:
    if not database.exists():
        raise RuntimeError(f"Database does not exist: {database}")
    with EventStore(database) as store:
        backfilled = store.backfill_sensor_samples()
        if backfilled:
            print(f"Backfilled {backfilled} sensor samples from recorded events.\n")
        report = build_report(store.payloads("activity"), store.sensor_samples())
    print(to_json(report) if as_json else render(report))
    return 0


def _summary(database: Path, limit: int) -> int:
    if not database.exists():
        raise RuntimeError(f"Database does not exist: {database}")
    if limit < 1:
        raise ValueError("limit must be greater than zero")
    with EventStore(database) as store:
        print("Record counts:")
        print(json.dumps(store.counts(), indent=2, sort_keys=True))
        print("\nMost recent records:")
        for record in store.recent(limit):
            print(json.dumps(record, indent=2, sort_keys=True))
    return 0
