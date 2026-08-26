"""Automatic recovery from stuck cat-sensor states.

**This module writes to the device.** Everything else in this package is
read-only; this is the one deliberate exception, and it stays opt-in. Nothing
here runs unless the ``autoreset`` subcommand is invoked, and no command is
dispatched unless ``--arm`` is passed as well.

The recovery it performs mirrors what the owner does by hand at the unit, using
``shortResetPress`` and ``cleanCycle``. Which presses, and how many, depends on
where the globe actually is — measured 2026-07-30 by ``probe-reset``:

===================  ==========================  =======================
State                ``shortResetPress``         ``cleanCycle``
===================  ==========================  =======================
Idle at home         recalibrates (double)       starts a cycle
Cycle running        pauses it where it stands   --
Cycle paused         resumes it, runs to home    ignored entirely
===================  ==========================  =======================

Reset is a pause/resume **toggle** mid-cycle, not a "return home" command, so
clearing a stalled cycle takes two presses: one to break the stall into a
pause, one to resume it. ``next_recovery_command`` holds that decision table
and ``_recover`` observes between every press rather than firing a fixed
sequence — a third Reset would re-pause the cycle the second just resumed.

Safety rationale, since a reset can start the globe turning:

* The trigger is a sensor already known to lie on this unit, so the watchdog can
  never *prove* the globe is empty. It instead requires the stuck state to have
  persisted far longer than any plausible cat visit, and requires the activity
  stream to have been silent for a quiet period before acting.
* The idle-latch path additionally checks the top-centre ToF distance for a
  large object in the globe. That check is deliberately skipped for cycle
  stalls: ``litterLevel`` is frozen mid-cycle (see
  ``docs/cat-detect-investigation.md``), so a mid-cycle reading proves nothing.
* A removed bonnet means a person has the unit open, and this unit reports
  ``ROBOT_CAT_DETECT_DELAY`` throughout a cleaning session — indistinguishable
  from an idle latch on status alone. No command is dispatched while the bonnet
  is off or its state is unknown, and reseating it restarts the stuck timers so
  a latch that ripened during maintenance cannot license an immediate reset.
* Attempts are rate limited, and repeated failures latch the watchdog into an
  escalated state where it stops acting and asks for a human.

Every assessment and attempt is recorded to the same store as the diagnostic
capture, under source ``intervention``, so a later analysis can exclude or
annotate windows the watchdog interfered with.
"""

from __future__ import annotations

import asyncio
import copy
import logging
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Any, Protocol

from pylitterbot import Account, LitterRobot4
from pylitterbot.event import EVENT_UPDATE

from .auth import load_token, resolve_password, save_token
from .capture import _isolated_botocore_configuration
from .redact import pseudonym
from .sensors import number
from .store import EventStore
from .subscriptions import stream_activity

_LOGGER = logging.getLogger(__name__)

#: ``displayCode``/``robotStatus`` values for the >30-minute scale latch. The
#: unit sits at home reporting a cat that never leaves, so the wait timer never
#: elapses and no cycle runs at all.
LATCH_DISPLAY_CODES = frozenset({"DC_CAT_DETECT_30M"})
LATCH_ROBOT_STATUSES = frozenset({"ROBOT_CAT_DETECT_DELAY"})

#: ``robotCycleStatus`` while the globe is parked. Anything else is a cycle in
#: progress, which is what makes a stalled cycle detectable.
IDLE_CYCLE_STATUSES = frozenset({"CYCLE_IDLE", "CYCLE_NONE"})

#: A cycle halted partway rather than progressing. Established by the probe on
#: 2026-07-30: a ``shortResetPress`` during ``CYCLE_DUMP`` left the unit at
#: ``DC_USER_PAUSE`` / ``CYCLE_STATE_PAUSE`` with ``robotCycleStatus`` still
#: reading ``CYCLE_DUMP``. The globe does not move again on its own from here.
PAUSED_CYCLE_STATES = frozenset({"CYCLE_STATE_PAUSE"})
PAUSED_DISPLAY_CODES = frozenset({"DC_USER_PAUSE"})

#: Activity ``value`` markers that mean something really interacted with the
#: box. A fresh one inside the quiet period blocks any intervention.
CAT_ACTIVITY_VALUES = frozenset({"catWeight", "robotStatusCatDetect", "robotCycleStateCatDetect"})

IDLE_LATCH = "idle-latch"
CYCLE_STALL = "cycle-stall"
CYCLE_OVERRUN = "cycle-overrun"

#: The two commands this module may send. `RESET` is `shortResetPress`, which
#: is a pause/resume toggle mid-cycle rather than a "return home" command;
#: `CLEAN_CYCLE` starts a cycle from home and does nothing to a paused unit.
RESET = "reset"
CLEAN_CYCLE = "clean_cycle"


@dataclass(frozen=True)
class RecoveryPolicy:
    """Thresholds and limits governing when the watchdog may act."""

    #: Extra time to wait after the unit reports the >30-minute latch. The
    #: latch already implies 30 minutes of no usable box, so the default puts
    #: intervention at roughly the 45-minute mark. Every latch observed so far
    #: eventually self-resolved, but they ran 0.8h to 8.2h.
    latch_grace: float = 900.0
    #: How long a cycle may sit on an unchanged state before it counts as
    #: stalled. Healthy cycles finish in 2.2m, aborting ones in 3.4m, and the
    #: longest single phase on this unit's own diagnostics timers is 57s, so
    #: 5m on one unchanged state is well past anything normal. A stall is the
    #: urgent mode: the globe is parked away from home and the cats cannot use
    #: the box at all, so it is deliberately tighter than `latch_grace`.
    stall_grace: float = 300.0
    #: Ceiling on total cycle duration. A cycle that keeps flipping between
    #: internal states never trips `stall_grace`, but on this unit a cycle
    #: retrying against a false detection does exactly that, so cap the whole
    #: cycle as well. Median cycle is 3.4m even when aborting.
    max_cycle_seconds: float = 600.0
    #: Required silence on the activity stream before acting. This, not
    #: `stall_grace`, is usually what sets the response time for a stall: the
    #: abort that caused it emits a cat-detect activity row, so the clock
    #: effectively starts at the last abort.
    quiet_period: float = 600.0
    #: Minimum gap between attempts.
    cooldown: float = 900.0
    #: Ceiling on attempts per rolling hour. Keep this below `3600 / cooldown`
    #: or the cooldown alone already satisfies it and this gate never binds.
    max_per_hour: int = 2
    #: Consecutive failed recoveries before the watchdog stops and escalates.
    max_consecutive_failures: int = 3
    #: Pause between the Reset press and the Cycle press.
    settle: float = 20.0
    #: How long to wait for the unit to reach a healthy state afterwards. This
    #: only decides what gets logged for the attempt; whether the recovery
    #: actually *held* is judged later, from `recovery_hold`.
    verify_timeout: float = 300.0
    #: How long the unit must stay healthy after a dispatched recovery before
    #: that recovery counts as successful. Reaching home once is not enough: a
    #: drifting scale re-latches minutes later, and judging success on the
    #: immediate check would clear the failure counter every time and leave
    #: `max_consecutive_failures` permanently out of reach.
    recovery_hold: float = 1800.0
    #: Ceiling on commands dispatched in one recovery attempt. Recovering a
    #: stalled cycle can legitimately need two Reset presses — one to break the
    #: stall into a pause, one to resume it — so this cannot be 1, but it must
    #: be small: Reset toggles, and an unbounded loop would sit there pausing
    #: and resuming its own globe.
    max_recovery_steps: int = 4
    #: Minimum acceptable top-centre ToF distance, in millimetres, before an
    #: idle latch may be reset. Nominal at-rest litter reads 451-454mm and a
    #: full globe about 441mm, so anything much closer is an object, not litter.
    tof_clear_floor: float = 400.0
    require_clear_tof: bool = True
    poll_interval: float = 30.0
    #: When false the watchdog only logs and records what it would have done.
    armed: bool = False
    duration: float | None = None

    def validate(self) -> None:
        """Reject settings that would make the watchdog unsafe or too eager."""
        if self.latch_grace < 60:
            raise ValueError("latch grace must be at least 60 seconds")
        if self.stall_grace < 60:
            raise ValueError("stall grace must be at least 60 seconds")
        if self.max_cycle_seconds < 300:
            raise ValueError("max cycle seconds must be at least 300 seconds")
        if self.quiet_period < 60:
            raise ValueError("quiet period must be at least 60 seconds")
        if self.cooldown < 300:
            raise ValueError("cooldown must be at least 300 seconds")
        if self.max_per_hour < 1:
            raise ValueError("max per hour must be greater than zero")
        if self.max_per_hour > 3600 / self.cooldown:
            raise ValueError("max per hour is above what the cooldown already allows")
        if self.recovery_hold < 300:
            raise ValueError("recovery hold must be at least 300 seconds")
        if self.max_consecutive_failures < 1:
            raise ValueError("max consecutive failures must be greater than zero")
        if self.settle < 5:
            raise ValueError("settle must be at least 5 seconds")
        if self.verify_timeout < 60:
            raise ValueError("verify timeout must be at least 60 seconds")
        if self.poll_interval < 10:
            raise ValueError("poll interval must be at least 10 seconds")
        if self.tof_clear_floor < 0:
            raise ValueError("ToF clear floor cannot be negative")
        if self.duration is not None and self.duration <= 0:
            raise ValueError("duration must be greater than zero")


@dataclass(frozen=True)
class Observation:
    """One read-only look at the unit, reduced to the fields that matter."""

    at: float
    robot_status: str | None = None
    display_code: str | None = None
    cycle_status: str | None = None
    cycle_state: str | None = None
    litter_level_mm: float | None = None
    #: ``None`` when the unit did not report the bonnet sensor at all, which is
    #: treated as "not known to be closed" rather than as closed.
    bonnet_removed: bool | None = None

    @classmethod
    def from_state(cls, at: float, payload: dict[str, Any]) -> Observation:
        """Reduce a raw LR4 state payload to an observation."""
        return cls(
            at=at,
            robot_status=_text(payload.get("robotStatus")),
            display_code=_text(payload.get("displayCode")),
            cycle_status=_text(payload.get("robotCycleStatus")),
            cycle_state=_text(payload.get("robotCycleState")),
            litter_level_mm=number(payload.get("litterLevel")),
            bonnet_removed=_flag(payload.get("isBonnetRemoved")),
        )

    @property
    def bonnet_secure(self) -> bool:
        """Whether the bonnet is *known* to be in place.

        Deliberately false for an absent reading: the gate this feeds must not
        open just because the unit stopped reporting the sensor.
        """
        return self.bonnet_removed is False

    @property
    def is_latched(self) -> bool:
        """Whether the unit reports the >30-minute scale latch."""
        return self.display_code in LATCH_DISPLAY_CODES or self.robot_status in LATCH_ROBOT_STATUSES

    @property
    def is_cycling(self) -> bool:
        """Whether a clean cycle is in progress.

        A paused cycle still counts here, deliberately: the globe is away from
        home and nothing is going to move it, which is exactly what the stall
        timers exist to catch. Use :attr:`is_paused` to tell the two apart when
        deciding what command to send.
        """
        return self.cycle_status is not None and self.cycle_status not in IDLE_CYCLE_STATUSES

    @property
    def is_paused(self) -> bool:
        """Whether a cycle is halted partway rather than progressing.

        ``robotCycleStatus`` keeps reporting the phase it stopped in, so a
        paused globe is indistinguishable from a running one on that field
        alone; the pause only shows in ``robotCycleState``/``displayCode``.
        """
        return self.cycle_state in PAUSED_CYCLE_STATES or self.display_code in PAUSED_DISPLAY_CODES

    @property
    def is_healthy(self) -> bool:
        """Whether the unit is parked at home with no fault showing."""
        return not self.is_latched and not self.is_cycling and self.robot_status == "ROBOT_IDLE"

    def progress_key(self) -> tuple[str | None, str | None]:
        """Identify where in a cycle the unit is, for stall detection."""
        return (self.cycle_status, self.cycle_state)


@dataclass(frozen=True)
class Assessment:
    """The watchdog's verdict on one observation."""

    stuck: bool
    reason: str | None = None
    stuck_for: float = 0.0
    should_act: bool = False
    blocked_by: str | None = None

    def to_payload(self) -> dict[str, Any]:
        """Render for the intervention log."""
        return {
            "stuck": self.stuck,
            "reason": self.reason,
            "stuck_for_seconds": round(self.stuck_for, 1),
            "should_act": self.should_act,
            "blocked_by": self.blocked_by,
        }


class Watchdog:
    """Decide when a stuck unit warrants intervention.

    Pure state machine: no I/O, no clock of its own beyond the injected one, so
    the whole decision surface is testable without a robot or a network.
    """

    def __init__(
        self,
        policy: RecoveryPolicy,
        *,
        now: Callable[[], float] = monotonic,
    ) -> None:
        self.policy = policy
        self._now = now
        self._latched_since: float | None = None
        self._cycling_since: float | None = None
        self._progress_key: tuple[str | None, str | None] | None = None
        self._progress_since: float | None = None
        self._last_cat_activity: float | None = None
        self._last_attempt: float | None = None
        self._attempts: deque[float] = deque()
        self._consecutive_failures = 0
        self._bonnet_secure = False
        self._awaiting_verdict = False

    @property
    def consecutive_failures(self) -> int:
        """How many recoveries have failed back to back."""
        return self._consecutive_failures

    @property
    def escalated(self) -> bool:
        """Whether repeated failures have taken the watchdog out of service."""
        return self._consecutive_failures >= self.policy.max_consecutive_failures

    def note_cat_activity(self, at: float | None = None) -> None:
        """Record that the box saw real interaction, refreshing the quiet period.

        Keeps the newest marker rather than the last one supplied: callers feed
        rows straight off the activity stream, which is ordered newest-first, so
        assigning unconditionally would measure the quiet period from the oldest
        row in the batch.
        """
        moment = self._now() if at is None else at
        if self._last_cat_activity is None or moment > self._last_cat_activity:
            self._last_cat_activity = moment

    def note_attempt(self, *, dispatched: bool, at: float | None = None) -> None:
        """Record a recovery attempt and restart the stuck timers.

        An attempt moves the unit, so every "how long has it been like this"
        clock has to start over rather than measuring across the intervention.

        ``dispatched`` says whether a command actually reached the device. An
        unarmed run books the attempt so the cooldown and rate limit behave
        exactly as they would armed, but leaves no verdict outstanding: there
        is nothing to succeed or fail at.
        """
        moment = self._now() if at is None else at
        self._last_attempt = moment
        self._attempts.append(moment)
        self._latched_since = None
        self._cycling_since = None
        self._progress_key = None
        self._progress_since = None
        self._awaiting_verdict = dispatched

    def note_recovery_failed(self) -> None:
        """Record an unambiguous failure: the commands never took effect."""
        self._awaiting_verdict = False
        self._consecutive_failures += 1

    def snapshot(self) -> dict[str, object]:
        """Return the durable state needed to continue a scheduled watch safely.

        The normal sidecar keeps this state in memory.  A scheduled runtime must
        persist exactly the same timers, rather than treating each invocation as
        a fresh observation (which would silently disable the safety gates).
        """
        return {
            "latched_since": self._latched_since,
            "cycling_since": self._cycling_since,
            "progress_key": list(self._progress_key) if self._progress_key is not None else None,
            "progress_since": self._progress_since,
            "last_cat_activity": self._last_cat_activity,
            "last_attempt": self._last_attempt,
            "attempts": list(self._attempts),
            "consecutive_failures": self._consecutive_failures,
            # Without this, a scheduled runtime would start every invocation
            # believing the unit had just been opened, and the reseat handling
            # below would wipe the stuck timers once a minute — quietly
            # disabling the persistence gate altogether.
            "bonnet_secure": self._bonnet_secure,
            # A recovery is judged across invocations, so the outstanding
            # verdict has to survive with the timers it will be judged against.
            "awaiting_verdict": self._awaiting_verdict,
        }

    def restore(self, state: dict[str, object]) -> None:
        """Restore a previously saved :meth:`snapshot` without trusting it blindly."""
        self._latched_since = _optional_number(state.get("latched_since"))
        self._cycling_since = _optional_number(state.get("cycling_since"))
        progress_key = state.get("progress_key")
        if isinstance(progress_key, list) and len(progress_key) == 2:
            self._progress_key = tuple(
                None if value is None else str(value) for value in progress_key
            )  # type: ignore[assignment]
        else:
            self._progress_key = None
        self._progress_since = _optional_number(state.get("progress_since"))
        self._last_cat_activity = _optional_number(state.get("last_cat_activity"))
        self._last_attempt = _optional_number(state.get("last_attempt"))
        attempts = state.get("attempts")
        restored_attempts = (
            [_optional_number(item) for item in attempts] if isinstance(attempts, list) else []
        )
        self._attempts = deque(value for value in restored_attempts if value is not None)
        failures = state.get("consecutive_failures")
        self._consecutive_failures = (
            int(failures) if isinstance(failures, int) and failures >= 0 else 0
        )
        self._bonnet_secure = state.get("bonnet_secure") is True
        self._awaiting_verdict = state.get("awaiting_verdict") is True

    def assess(self, observation: Observation) -> Assessment:
        """Fold one observation into the state machine and return a verdict."""
        self._track_service(observation)
        self._track_latch(observation)
        self._track_progress(observation)

        reason, stuck_for = self._stuck_reason(observation)
        self._judge_pending_recovery(observation, reason)
        if reason is None:
            return Assessment(stuck=False)
        blocked_by = self._blocked_by(observation, reason)
        return Assessment(
            stuck=True,
            reason=reason,
            stuck_for=stuck_for,
            should_act=blocked_by is None,
            blocked_by=blocked_by,
        )

    def _judge_pending_recovery(self, observation: Observation, reason: str | None) -> None:
        """Decide whether the last dispatched recovery actually held.

        The check inside the attempt itself can only see that the unit reached
        home, and it always will: a reset drives the globe home and a cycle
        finishes in about three minutes. A drifting scale then re-forms the
        fault minutes later. Judging success there marked every attempt a
        success, cleared the failure counter, and left the escalation gate
        permanently out of reach while the unit was reset every ~16 minutes.

        So the verdict is drawn from later observations instead: the fault
        coming back is a failed recovery, and only staying healthy through
        `recovery_hold` counts as a real one.
        """
        if not self._awaiting_verdict or self._last_attempt is None:
            return
        if reason is not None:
            self._awaiting_verdict = False
            self._consecutive_failures += 1
        elif (
            observation.is_healthy
            and observation.at - self._last_attempt >= self.policy.recovery_hold
        ):
            self._awaiting_verdict = False
            self._consecutive_failures = 0

    def _track_service(self, observation: Observation) -> None:
        """Restart the stuck timers when the unit comes back from being opened.

        This unit reports ``ROBOT_CAT_DETECT_DELAY`` for the whole time the
        bonnet is off, so a cleaning session looks exactly like an idle latch
        ripening. Carrying that elapsed time past the reseat would let the
        watchdog fire the moment the bonnet went back on, with the owner still
        standing at the unit. Time spent open measures a person, not a fault.
        """
        secure = observation.bonnet_secure
        if secure and not self._bonnet_secure:
            self._latched_since = None
            self._cycling_since = None
            self._progress_key = None
            self._progress_since = None
        self._bonnet_secure = secure

    def _track_latch(self, observation: Observation) -> None:
        if observation.is_latched:
            if self._latched_since is None:
                self._latched_since = observation.at
        else:
            self._latched_since = None

    def _track_progress(self, observation: Observation) -> None:
        key = observation.progress_key()
        if not observation.is_cycling:
            self._cycling_since = None
            self._progress_key = None
            self._progress_since = None
            return
        if self._cycling_since is None:
            self._cycling_since = observation.at
        if key != self._progress_key:
            self._progress_key = key
            self._progress_since = observation.at

    def _stuck_reason(self, observation: Observation) -> tuple[str | None, float]:
        if observation.is_cycling and self._cycling_since is not None:
            running = observation.at - self._cycling_since
            if running >= self.policy.max_cycle_seconds:
                return CYCLE_OVERRUN, running
        if observation.is_cycling and self._progress_since is not None:
            held = observation.at - self._progress_since
            if held >= self.policy.stall_grace:
                return CYCLE_STALL, held
        if observation.is_latched and self._latched_since is not None:
            held = observation.at - self._latched_since
            if held >= self.policy.latch_grace:
                return IDLE_LATCH, held
        return None, 0.0

    def _blocked_by(self, observation: Observation, reason: str) -> str | None:
        """Return the first gate that forbids acting, or None if all pass."""
        # Checked first because it is the only gate guarding against a person
        # having their hands inside the globe, rather than a cat.
        if not observation.bonnet_secure:
            if observation.bonnet_removed is None:
                return "bonnet state unavailable"
            return "bonnet removed, someone is at the unit"

        if self.escalated:
            return f"escalated after {self._consecutive_failures} consecutive failures"

        if self._last_attempt is not None:
            since = observation.at - self._last_attempt
            if since < self.policy.cooldown:
                return f"cooldown, {self.policy.cooldown - since:.0f}s remaining"

        self._expire_attempts(observation.at)
        if len(self._attempts) >= self.policy.max_per_hour:
            return f"rate limit, {len(self._attempts)} attempts in the last hour"

        if self._last_cat_activity is not None:
            quiet_for = observation.at - self._last_cat_activity
            if quiet_for < self.policy.quiet_period:
                return f"cat activity {quiet_for:.0f}s ago"

        # Only meaningful at rest: the firmware freezes `litterLevel` for the
        # duration of a cycle, so a mid-cycle reading says nothing about
        # whether the globe is clear.
        if reason == IDLE_LATCH and self.policy.require_clear_tof:
            level = observation.litter_level_mm
            if level is None:
                return "top-centre ToF distance unavailable"
            if level < self.policy.tof_clear_floor:
                floor = self.policy.tof_clear_floor
                return f"ToF reads {level:.0f}mm, under the {floor:.0f}mm clear floor"

        return None

    def _expire_attempts(self, at: float) -> None:
        while self._attempts and at - self._attempts[0] > 3600.0:
            self._attempts.popleft()


@dataclass
class AutoResetConfig:
    """Runtime settings for a watchdog session."""

    database: Path
    policy: RecoveryPolicy = field(default_factory=RecoveryPolicy)

    def validate(self) -> None:
        """Reject unsafe settings before connecting."""
        self.policy.validate()


@dataclass(frozen=True)
class AutoResetResult:
    """Summary returned when a watchdog session stops."""

    robots: int
    attempts: int
    recoveries: int


class InterventionStore(Protocol):
    """The minimal persistence interface required by recovery recording."""

    def add(
        self,
        *,
        observed_at: str,
        source: str,
        robot_id: str,
        payload: Any,
        source_timestamp: str | None = None,
    ) -> bool: ...


async def run_autoreset(username: str, config: AutoResetConfig) -> AutoResetResult:
    """Watch owner-authorized LR4 units and recover them from stuck states."""
    config.validate()
    policy = config.policy
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

    if not policy.armed:
        _LOGGER.warning(
            "Running unarmed: stuck states will be detected and logged, but no "
            "command will be sent. Pass --arm to enable recovery."
        )

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

            watchdogs = {robot.serial: Watchdog(policy) for robot in robots}

            for robot in robots:

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

            return await _watch_loop(robots, watchdogs, updates, activity_updates, store, policy)
        finally:
            for task in activity_tasks:
                task.cancel()
            if activity_tasks:
                await asyncio.gather(*activity_tasks, return_exceptions=True)
            for unsubscribe in unsubscribers:
                unsubscribe()
            await account.disconnect()


async def _watch_loop(
    robots: list[LitterRobot4],
    watchdogs: dict[str, Watchdog],
    updates: asyncio.Queue[tuple[LitterRobot4, dict[str, Any]]],
    activity_updates: asyncio.Queue[tuple[LitterRobot4, dict[str, Any]]],
    store: EventStore,
    policy: RecoveryPolicy,
) -> AutoResetResult:
    started = monotonic()
    next_poll = started
    attempts = 0
    recoveries = 0
    announced: dict[str, str | None] = {}

    while True:
        now = monotonic()
        if policy.duration is not None and now - started >= policy.duration:
            return AutoResetResult(len(robots), attempts, recoveries)

        timeout = max(0.0, min(next_poll - now, 1.0))
        try:
            robot, payload = await asyncio.wait_for(updates.get(), timeout=timeout)
        except TimeoutError:
            pass
        else:
            watchdogs[robot.serial].assess(Observation.from_state(monotonic(), payload))

        while not activity_updates.empty():
            robot, row = activity_updates.get_nowait()
            if str(row.get("value")) in CAT_ACTIVITY_VALUES:
                watchdogs[robot.serial].note_cat_activity()

        now = monotonic()
        if now < next_poll:
            continue
        next_poll = now + policy.poll_interval

        for robot in robots:
            watchdog = watchdogs[robot.serial]
            try:
                await robot.refresh()
            except Exception as exc:  # noqa: BLE001 - a poll failure must not stop the watch
                _LOGGER.warning("State refresh failed (%s); will retry.", type(exc).__name__)
                continue

            observation = Observation.from_state(monotonic(), robot.to_dict())
            assessment = watchdog.assess(observation)
            _announce(robot, assessment, announced)
            if not assessment.stuck:
                continue

            _record(store, robot, "assessment", observation, assessment, policy)
            log_stuck(assessment, policy)
            if not assessment.should_act:
                continue

            attempts += 1
            # Booked before dispatch: the cooldown and rate limit must hold
            # even if everything after this raises.
            watchdog.note_attempt(dispatched=policy.armed)
            returned_home = await _recover(robot, observation, assessment, store, policy)
            if not policy.armed:
                continue
            if returned_home:
                recoveries += 1
            else:
                watchdog.note_recovery_failed()
            if watchdog.escalated:
                _LOGGER.error(
                    "WATCHDOG_ESCALATED failures=%d; standing down until a "
                    "human intervenes at the unit.",
                    watchdog.consecutive_failures,
                )


async def _recover(
    robot: LitterRobot4,
    observation: Observation,
    assessment: Assessment,
    store: InterventionStore,
    policy: RecoveryPolicy,
) -> bool:
    """Drive the unit back to a usable state, observing between every press.

    This is a loop, not a fixed sequence, because ``shortResetPress`` is a
    **pause/resume toggle** mid-cycle rather than a "go home" command. Measured
    on 2026-07-30:

    ===================  ==========================  =======================
    State                ``shortResetPress``         ``cleanCycle``
    ===================  ==========================  =======================
    Idle at home         (recalibrates)              starts a cycle
    Cycle running        pauses it where it stands   --
    Cycle paused         resumes it, runs to home    ignored entirely
    ===================  ==========================  =======================

    So recovering a stalled cycle can take *two* Reset presses — one to break
    the stall into a pause, one to resume it — and which presses are needed
    depends on where the globe actually ends up after each. A fixed sequence
    gets this wrong in both directions: firing Reset twice blindly would pause
    a cycle that had already resumed, and firing Reset-then-Cycle leaves a
    paused globe untouched, since ``cleanCycle`` does nothing to a pause.

    `next_recovery_command` holds that decision table; this function just
    dispatches what it asks for and re-observes.
    """
    _LOGGER.info(
        "Dispatching recovery: reason=%s stuck_for=%.0fs",
        assessment.reason,
        assessment.stuck_for,
    )
    if not policy.armed:
        _record(store, robot, "skipped_unarmed", observation, assessment, policy)
        return False

    steps: dict[str, Any] = {}
    dispatched: list[str] = []
    try:
        current = observation
        for _ in range(policy.max_recovery_steps):
            command = next_recovery_command(assessment.reason, current, sent=dispatched)
            if command is None:
                break
            if command == RESET:
                await robot.reset()
            else:
                await robot.start_cleaning()
            dispatched.append(command)
            current = await _observe_after_command(robot, policy, current)

        steps["commands"] = list(dispatched)
        steps["state_after_commands"] = {
            "robot_status": current.robot_status,
            "display_code": current.display_code,
            "paused": current.is_paused,
            "cycling": current.is_cycling,
            "home": current.is_healthy,
        }
        succeeded = await _wait_for_recovery(robot, policy)
    except Exception as exc:  # noqa: BLE001 - a failed recovery is data, not a crash
        _LOGGER.error("Recovery attempt failed: %s", type(exc).__name__)
        steps["commands"] = list(dispatched)
        steps["error"] = type(exc).__name__
        succeeded = False

    steps["succeeded"] = succeeded
    _record(store, robot, "recovery", observation, assessment, policy, extra=steps)
    _LOGGER.info("Recovery %s.", "succeeded" if succeeded else "did not clear the fault")
    return succeeded


@dataclass(frozen=True)
class ProbeResult:
    """What a supervised Reset probe established."""

    reset_alone_returned_home: bool
    paused_by_first_reset: bool
    resumed_by_second_reset: bool
    recovered: bool
    seconds_to_home: float | None
    timeline: list[dict[str, Any]]

    def summary(self) -> str:
        """One-line verdict for the operator standing at the unit."""
        if self.reset_alone_returned_home:
            return f"One Reset brought the globe home in {self.seconds_to_home:.0f}s."
        if self.recovered:
            return (
                "The first Reset paused the globe; a second Reset resumed it and ran "
                f"the cycle out to home in {self.seconds_to_home:.0f}s. "
                "A stall recovery needs two Reset presses, not a Cycle press."
            )
        return "Neither Reset press recovered the unit; it needs a hand at the machine."


async def probe_reset_recovery(username: str, config: AutoResetConfig) -> ProbeResult:
    """Deliberately stall a cycle, press Reset, and record where the globe goes.

    This answers one question the passive record cannot: does ``shortResetPress``
    on its own return a globe that is away from home, or is a ``cleanCycle``
    press required afterwards? It is a supervised experiment in the same spirit
    as the empty-box tests in ``docs/cat-detect-investigation.md`` — **the globe
    must be empty and someone must be watching the unit.**

    It rotates the globe on purpose, which nothing else in this package does, so
    it refuses to run unless armed and unless every pre-flight check passes: the
    unit healthy and at home, the bonnet on, the ToF floor clear, and the
    activity stream quiet. Whatever happens, it finishes by getting the unit
    back to home rather than leaving it stalled.
    """
    config.validate()
    policy = config.policy
    if not policy.armed:
        raise RuntimeError("The reset probe sends commands to the device; pass --arm to run it.")

    token = load_token(username)
    password = None if token else resolve_password(username)
    account = Account(
        token=token,
        token_update_callback=lambda value: save_token(username, value),
        robot_types=[LitterRobot4],
    )

    with _isolated_botocore_configuration(), EventStore(config.database) as store:
        try:
            await account.connect(
                username=username if token is None else None,
                password=password,
                load_robots=True,
                subscribe_for_updates=False,
            )
            robots = [robot for robot in account.robots if isinstance(robot, LitterRobot4)]
            if not robots:
                raise RuntimeError("No Litter-Robot 4 was found on this Whisker account.")
            robot = robots[0]
            return await _run_probe(robot, store, policy)
        finally:
            await account.disconnect()


async def _run_probe(
    robot: LitterRobot4, store: InterventionStore, policy: RecoveryPolicy
) -> ProbeResult:
    """Execute one probe against a unit that has already been connected."""
    await robot.refresh()
    start = Observation.from_state(monotonic(), robot.to_dict())
    _refuse_unsafe_probe(start, policy)

    timeline: list[dict[str, Any]] = []

    def mark(phase: str, observation: Observation) -> None:
        timeline.append(
            {
                "phase": phase,
                "offset_seconds": round(observation.at - start.at, 1),
                "robot_status": observation.robot_status,
                "display_code": observation.display_code,
                "cycle_status": observation.cycle_status,
                "cycle_state": observation.cycle_state,
            }
        )

    mark("baseline", start)
    _LOGGER.info("Probe: starting a cycle so the globe leaves home.")
    await robot.start_cleaning()
    away = await _poll_until(robot, lambda o: o.is_cycling, policy.settle * 3, mark, "leaving-home")
    if not away.is_cycling:
        raise RuntimeError("The unit never started cycling; probe aborted without pressing Reset.")

    # Let the globe get well away from home before interrupting it, so the
    # question being asked is genuinely "can Reset recover a stalled globe".
    await asyncio.sleep(policy.settle)
    await robot.refresh()
    mark("mid-cycle", Observation.from_state(monotonic(), robot.to_dict()))

    _LOGGER.info("Probe: pressing Reset, and nothing else.")
    reset_at = monotonic()
    await robot.reset()
    # Stop as soon as the outcome is knowable, rather than burning the whole
    # verify timeout on a state that will not change. The first run waited the
    # full five minutes before its second press and stranded the globe for it.
    after = await _poll_until(
        robot,
        lambda o: o.is_healthy or o.is_paused,
        policy.verify_timeout,
        mark,
        "post-reset",
    )

    if after.is_healthy:
        result = ProbeResult(True, False, False, True, after.at - reset_at, timeline)
    else:
        # A second Reset, not a Cycle press. Reset toggles pause/resume; the
        # first probe run sent `cleanCycle` here and the unit ignored it for
        # five minutes, stranding the globe until someone pressed Reset by hand.
        _LOGGER.info("Probe: Reset paused the globe; pressing Reset again to resume it.")
        second_at = monotonic()
        await robot.reset()
        recovered = await _poll_until(
            robot, lambda o: o.is_healthy, policy.verify_timeout, mark, "post-second-reset"
        )
        result = ProbeResult(
            reset_alone_returned_home=False,
            paused_by_first_reset=after.is_paused,
            resumed_by_second_reset=recovered.is_healthy,
            recovered=recovered.is_healthy,
            seconds_to_home=recovered.at - second_at if recovered.is_healthy else None,
            timeline=timeline,
        )

    _LOGGER.info("Probe: %s", result.summary())
    store.add(
        observed_at=datetime.now(UTC).isoformat(),
        source="intervention",
        robot_id=pseudonym(robot.serial),
        payload={
            "at": datetime.now(UTC).isoformat(),
            "kind": "probe",
            "armed": True,
            "reset_alone_returned_home": result.reset_alone_returned_home,
            "paused_by_first_reset": result.paused_by_first_reset,
            "resumed_by_second_reset": result.resumed_by_second_reset,
            "recovered": result.recovered,
            "seconds_to_home": result.seconds_to_home,
            "timeline": timeline,
        },
    )
    return result


def _refuse_unsafe_probe(start: Observation, policy: RecoveryPolicy) -> None:
    """Reject a probe on a unit that is not in a known-good starting state."""
    if not start.bonnet_secure:
        raise RuntimeError("The bonnet is off or unreported; do not rotate the globe.")
    if not start.is_healthy:
        raise RuntimeError(
            f"The unit is not idle at home (robotStatus={start.robot_status}); "
            "the probe needs a clean starting state."
        )
    level = start.litter_level_mm
    if policy.require_clear_tof and (level is None or level < policy.tof_clear_floor):
        raise RuntimeError(
            f"Top-centre ToF reads {level}mm against a {policy.tof_clear_floor:.0f}mm "
            "floor; something may be in the globe."
        )


async def _poll_until(
    robot: LitterRobot4,
    predicate: Callable[[Observation], bool],
    timeout: float,
    mark: Callable[[str, Observation], None],
    phase: str,
) -> Observation:
    """Refresh every few seconds until the predicate holds or time runs out."""
    deadline = monotonic() + timeout
    observation = Observation.from_state(monotonic(), robot.to_dict())
    while monotonic() < deadline:
        await asyncio.sleep(min(5.0, max(0.0, deadline - monotonic())))
        try:
            await robot.refresh()
        except Exception as exc:  # noqa: BLE001 - keep polling through transient errors
            _LOGGER.debug("Probe refresh failed (%s).", type(exc).__name__)
            continue
        observation = Observation.from_state(monotonic(), robot.to_dict())
        mark(phase, observation)
        if predicate(observation):
            break
    return observation


def log_stuck(assessment: Assessment, policy: RecoveryPolicy) -> None:
    """Emit the alarm line for a stuck unit, whatever the watchdog does next.

    This has to fire on *detection*, not on dispatch. Any gate in
    `Watchdog._blocked_by` can hold a recovery off — bonnet, escalation,
    cooldown, rate limit, quiet period, ToF clearance — and an unarmed
    deployment dispatches nothing at all. If the line only appeared when a
    command went out, the CloudWatch alarm would stay silent for precisely the
    cases where the owner has to walk to the unit themselves.

    Repeating it every poll while the unit stays stuck is deliberate: the alarm
    only notifies on the OK->ALARM transition, and continued lines keep it from
    flapping back to OK while the box is still unusable.
    """
    _LOGGER.info(
        "WATCHDOG_STUCK reason=%s stuck_for=%.0fs armed=%s acting=%s blocked=%s",
        assessment.reason,
        assessment.stuck_for,
        policy.armed,
        assessment.should_act,
        assessment.blocked_by or "-",
    )


def next_recovery_command(
    reason: str | None, observation: Observation, *, sent: Sequence[str]
) -> str | None:
    """The one command that moves this state toward a usable box, or None.

    Returning None means "stop pressing things": either the unit is home, or it
    is progressing under its own steam and the right move is to wait.

    ``sent`` is what has already been dispatched this attempt, and bounds the
    loop. Reset is capped at two presses because it toggles: a third would pause
    a cycle that the second had just resumed, which is how a recovery loop turns
    into a machine that keeps its own globe parked.
    """
    if observation.is_paused:
        # Reset resumes a pause and runs the cycle out to home. `cleanCycle` is
        # inert here -- dispatched and ignored for five minutes in the probe.
        return RESET if sent.count(RESET) < 2 else None

    if observation.is_latched:
        return RESET if RESET not in sent else None

    if observation.is_cycling:
        # A stalled cycle reads as "cycling" while going nowhere. The first
        # Reset breaks it into a pause, which the branch above then resumes.
        # A genuinely progressing cycle wants no interference at all.
        if reason in (CYCLE_STALL, CYCLE_OVERRUN) and RESET not in sent:
            return RESET
        return None

    if observation.is_healthy:
        # Home and clear. A latch still owes the cleaning it was refusing to do;
        # nothing else does.
        if reason == IDLE_LATCH and CLEAN_CYCLE not in sent:
            return CLEAN_CYCLE
        return None

    # Away from home, not cycling, not paused, not latched. A Cycle press is the
    # only remaining lever that moves the globe.
    return CLEAN_CYCLE if CLEAN_CYCLE not in sent else None


def _state_key(observation: Observation) -> tuple[object, ...]:
    """What has to change before a command counts as having taken effect."""
    return (
        observation.is_paused,
        observation.is_latched,
        observation.is_cycling,
        observation.cycle_status,
        observation.cycle_state,
    )


async def _observe_after_command(
    robot: LitterRobot4, policy: RecoveryPolicy, before: Observation
) -> Observation:
    """Poll until the state actually moves, or ``settle`` elapses.

    Waiting for a *change* rather than for a fixed pause matters: read too
    early and a Reset that has not landed yet still looks like a pause, so the
    loop would press Reset again and re-pause the cycle it just resumed.
    """
    deadline = monotonic() + policy.settle
    observation = before
    while True:
        await asyncio.sleep(min(5.0, max(0.0, deadline - monotonic())))
        try:
            await robot.refresh()
        except Exception as exc:  # noqa: BLE001 - keep watching through transient errors
            _LOGGER.debug("Settle refresh failed (%s).", type(exc).__name__)
        else:
            observation = Observation.from_state(monotonic(), robot.to_dict())
            if observation.is_healthy or _state_key(observation) != _state_key(before):
                break
        # Always look at least once: dispatching a command and never checking
        # what it did is how the loop ends up deciding its next press from a
        # state that predates the press it just made.
        if monotonic() >= deadline:
            break
    return observation


async def _wait_for_recovery(robot: LitterRobot4, policy: RecoveryPolicy) -> bool:
    """Poll until the unit parks at home cleanly, or the timeout elapses."""
    deadline = monotonic() + policy.verify_timeout
    while monotonic() < deadline:
        await asyncio.sleep(min(policy.poll_interval, max(0.0, deadline - monotonic())))
        try:
            await robot.refresh()
        except Exception as exc:  # noqa: BLE001 - keep polling through transient errors
            _LOGGER.debug("Verification refresh failed (%s).", type(exc).__name__)
            continue
        if Observation.from_state(monotonic(), robot.to_dict()).is_healthy:
            return True
    return False


def _announce(
    robot: LitterRobot4,
    assessment: Assessment,
    announced: dict[str, str | None],
) -> None:
    """Log state changes once rather than on every poll."""
    key = f"{assessment.reason}:{assessment.blocked_by}" if assessment.stuck else None
    if announced.get(robot.serial) == key:
        return
    announced[robot.serial] = key
    if key is None:
        _LOGGER.info("Unit healthy.")
    elif assessment.blocked_by:
        _LOGGER.info(
            "Unit stuck (%s) for %.0fs but holding off: %s.",
            assessment.reason,
            assessment.stuck_for,
            assessment.blocked_by,
        )


def _record(
    store: InterventionStore,
    robot: LitterRobot4,
    kind: str,
    observation: Observation,
    assessment: Assessment,
    policy: RecoveryPolicy,
    extra: dict[str, Any] | None = None,
) -> None:
    """Persist a watchdog decision so analysis can exclude the window later."""
    observed_at = datetime.now(UTC).isoformat()
    payload: dict[str, Any] = {
        # The events table deduplicates on payload hash; the timestamp keeps
        # otherwise-identical decisions distinct.
        "at": observed_at,
        "kind": kind,
        "armed": policy.armed,
        "observation": {
            "robot_status": observation.robot_status,
            "display_code": observation.display_code,
            "cycle_status": observation.cycle_status,
            "cycle_state": observation.cycle_state,
            "litter_level_mm": observation.litter_level_mm,
            "bonnet_removed": observation.bonnet_removed,
        },
        "assessment": assessment.to_payload(),
    }
    if extra:
        payload["steps"] = extra
    store.add(
        observed_at=observed_at,
        source="intervention",
        robot_id=pseudonym(robot.serial),
        payload=payload,
    )


def _text(value: object) -> str | None:
    return None if value is None else str(value)


def _flag(value: object) -> bool | None:
    """Read a boolean state field, keeping "absent" distinct from "false"."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str) and value.strip().lower() in {"true", "false"}:
        return value.strip().lower() == "true"
    return None


def _optional_number(value: object) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None
