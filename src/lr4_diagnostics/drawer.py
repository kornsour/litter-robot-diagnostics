"""Read-only waste-drawer level monitoring.

Whisker notifies about a full drawer through an app push and nothing else: the
notification settings offer no email, no webhook, and there is no public API to
subscribe to.  This turns the same condition into a marker log line that a
CloudWatch metric filter converts into email through the alerts topic the stuck
and escalation alarms already use.

Nothing here talks to the device.  The whole decision surface is a pure state
machine over readings the scheduled check already collects, so it is testable
without a robot and cannot become a route for undocumented commands.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from .sensors import number

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class DrawerPolicy:
    """Thresholds for deciding that the waste drawer needs emptying."""

    #: Level at or above which the drawer counts as needing attention.
    warn_percent: float = 85.0
    #: Level the drawer must fall back to before the warning is released.
    #: Deliberately far below :attr:`warn_percent`: `analysis` already flags the
    #: DFI ToF family as noisy when it swings 8% or more across idle samples, and
    #: releasing on the first dip below the warning level would flap the alarm
    #: OK->ALARM and re-send the email on every swing back up. Only an emptied
    #: drawer travels the whole band.
    clear_percent: float = 60.0
    #: Consecutive at-rest readings above :attr:`warn_percent` before warning.
    #: At one scheduled check a minute this trades a few minutes of delay --
    #: irrelevant for a drawer -- against a single noisy sample sending mail.
    consecutive_samples: int = 5

    def validate(self) -> None:
        """Reject settings that would make the warning noisy or unreachable."""
        if not 0 < self.warn_percent <= 100:
            raise ValueError("warn percent must be above zero and at most 100")
        if not 0 <= self.clear_percent < self.warn_percent:
            raise ValueError("clear percent must be below warn percent and not negative")
        if self.consecutive_samples < 1:
            raise ValueError("consecutive samples must be greater than zero")


@dataclass(frozen=True)
class DrawerReading:
    """One read-only look at the waste drawer."""

    #: ``DFILevelPercent``, or ``None`` when the unit did not report it.
    percent: float | None = None
    #: The firmware's own drawer-full flag -- the signal the app push uses.
    drawer_full: bool = False
    #: Whether the unit was at rest.  The DFI sensor is re-read during the DFI
    #: phase of a cycle, so a mid-cycle percentage is a measurement in progress
    #: rather than a level.
    at_rest: bool = True

    @classmethod
    def from_state(cls, payload: dict[str, Any], *, at_rest: bool) -> DrawerReading:
        """Reduce a raw LR4 state payload to a drawer reading."""
        return cls(
            percent=number(payload.get("DFILevelPercent")),
            drawer_full=payload.get("isDFIFull") is True,
            at_rest=at_rest,
        )


@dataclass(frozen=True)
class DrawerVerdict:
    """The monitor's verdict on one reading."""

    warned: bool
    percent: float | None = None
    streak: int = 0
    #: True only on the reading that first raised the warning, so a caller can
    #: tell a new warning from the repeats that hold the alarm in ALARM.
    newly_warned: bool = False

    def to_payload(self) -> dict[str, Any]:
        """Render for a diagnostic record."""
        return {
            "warned": self.warned,
            "percent": self.percent,
            "streak": self.streak,
            "newly_warned": self.newly_warned,
        }


class DrawerMonitor:
    """Decide when the waste drawer warrants telling the owner about.

    Pure state machine: no I/O and no clock, so a scheduled runtime only has to
    carry :meth:`snapshot` between invocations for the persistence gate and the
    hysteresis band to behave the way they do in a long-lived process.
    """

    def __init__(self, policy: DrawerPolicy) -> None:
        self.policy = policy
        self._streak = 0
        self._warned = False

    @property
    def warned(self) -> bool:
        """Whether the drawer is currently considered in need of emptying."""
        return self._warned

    def snapshot(self) -> dict[str, object]:
        """Return the durable state needed to continue across invocations."""
        return {"streak": self._streak, "warned": self._warned}

    def restore(self, state: dict[str, object]) -> None:
        """Restore a previously saved :meth:`snapshot` without trusting it blindly."""
        streak = state.get("streak")
        self._streak = int(streak) if isinstance(streak, int) and streak >= 0 else 0
        self._warned = state.get("warned") is True

    def observe(self, reading: DrawerReading) -> DrawerVerdict:
        """Fold one reading into the state machine and return a verdict."""
        was_warned = self._warned
        if reading.drawer_full:
            # The firmware's own flag needs no corroboration: it is a boolean
            # rather than a noisy distance, and it is the same signal the app
            # push fires on, so waiting out the persistence gate would only
            # deliver the mail later than the phone notification.
            self._streak = self.policy.consecutive_samples
            self._warned = True
        elif reading.percent is None or not reading.at_rest:
            # A missing reading, or one taken while the sensor is being re-read
            # mid-cycle, is not evidence in either direction. Hold the streak
            # rather than counting it toward or against the warning.
            pass
        elif reading.percent >= self.policy.warn_percent:
            self._streak += 1
            if self._streak >= self.policy.consecutive_samples:
                self._warned = True
        elif reading.percent <= self.policy.clear_percent:
            self._streak = 0
            self._warned = False
        else:
            # Inside the hysteresis band: not high enough to build toward a
            # warning, not low enough to say the drawer was emptied.
            self._streak = 0
        return DrawerVerdict(
            warned=self._warned,
            percent=reading.percent,
            streak=self._streak,
            newly_warned=self._warned and not was_warned,
        )


def log_drawer_full(verdict: DrawerVerdict, policy: DrawerPolicy) -> None:
    """Emit the alarm line for a drawer that needs emptying.

    Repeating it on every check while the drawer stays full is deliberate, for
    the same reason ``log_stuck`` repeats: the alarm notifies on the OK->ALARM
    transition, so continued lines hold it in ALARM until the drawer is actually
    emptied.  That is what makes this one mail per fill rather than one per
    check, and what re-arms it for the next fill.
    """
    _LOGGER.info(
        "WATCHDOG_DRAWER_FULL percent=%s warn_at=%.0f clear_at=%.0f",
        "-" if verdict.percent is None else f"{verdict.percent:.0f}",
        policy.warn_percent,
        policy.clear_percent,
    )
