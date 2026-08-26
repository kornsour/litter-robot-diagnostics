"""Read-only monitoring of the unit's latched hardware fault flags.

The LR4 reports fault flags alongside its status, and they latch: once set they
stay set in cloud state until the firmware clears them.  Nothing in the app
surfaces that once the unit is back to an idle display, and nothing in
`autoreset` reads them either -- `Observation` carries status, display code,
cycle status/state, litter level and bonnet, so a unit parked at home with a
latched motor fault assesses as `is_healthy` and draws no attention at all.
The first `globeMotorFaultStatus = FAULT_TIMEOUT` on this unit went four hours
without a notification for exactly that reason.

This turns those flags into a marker log line that a CloudWatch metric filter
converts into email through the topic the stuck, escalation and drawer alarms
already use.

Nothing here talks to the device.  Detection only, in the same shape as
`drawer`: a pure state machine over readings the scheduled check already
collects, so it is testable without a robot and cannot become a route for
undocumented commands.  Device control stays confined to `autoreset`.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

_LOGGER = logging.getLogger(__name__)

#: The latched fault flags worth telling the owner about.  `hopperStatus` is
#: deliberately absent: this unit reports `isHopperRemoved` with no hopper
#: fitted, so the field is null on every sample and would only add noise.
FAULT_FIELDS = (
    "globeMotorFaultStatus",
    "retractMotorFaultStatus",
    "pinchStatus",
    "USBFaultStatus",
)

#: What each field calls "no fault".  The motor fields use the `FAULT_` prefix
#: and the pinch/USB fields do not, so both spellings have to be recognised;
#: anything else that is not a clear value is treated as a fault rather than
#: guessed at, so an unrecognised new fault code alarms instead of passing.
CLEAR_VALUES = frozenset({"FAULT_CLEAR", "CLEAR"})


@dataclass(frozen=True)
class FaultPolicy:
    """Thresholds for deciding that a latched fault warrants an email."""

    #: Consecutive readings of the same fault value before it is reported.
    #: Deliberately lower than the drawer's five: this is a categorical flag
    #: rather than a noisy ToF distance, so persistence buys nothing except
    #: riding over a single garbled or partial payload.  At one scheduled check
    #: a minute the cost is three minutes of delay on a latch that, once set,
    #: has stayed set for hours.
    consecutive_samples: int = 3

    def validate(self) -> None:
        """Reject settings that would make the warning noisy or unreachable."""
        if self.consecutive_samples < 1:
            raise ValueError("consecutive samples must be greater than zero")


@dataclass(frozen=True)
class FaultReading:
    """One read-only look at the unit's fault flags.

    Only fields the unit actually reported are carried.  A field the payload
    omits is unknown, not clear: an absent reading must never release a latch
    that a previous reading raised.
    """

    values: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def from_state(cls, payload: dict[str, Any]) -> FaultReading:
        """Reduce a raw LR4 state payload to the fault flags it reported.

        Unlike the drawer reading there is no `at_rest` gate.  A drawer
        percentage taken mid-cycle is a measurement in progress, but a fault
        raised mid-cycle is the whole point -- a globe motor timeout happens
        *during* rotation, so discarding those readings would discard the
        evidence.
        """
        values = {}
        for name in FAULT_FIELDS:
            raw = payload.get(name)
            if isinstance(raw, str) and raw:
                values[name] = raw
        return cls(values=values)

    def faults(self) -> dict[str, str]:
        """The reported fields that are not in a clear state."""
        return {name: value for name, value in self.values.items() if value not in CLEAR_VALUES}


@dataclass(frozen=True)
class FaultVerdict:
    """The monitor's verdict on one reading."""

    #: Field name -> fault value, for every fault past the persistence gate.
    active: Mapping[str, str] = field(default_factory=dict)
    #: The subset that crossed the gate on this reading, so a caller can tell a
    #: new fault from the repeats that hold the alarm in ALARM.
    newly_active: Mapping[str, str] = field(default_factory=dict)
    #: Field name -> consecutive count, including faults still below the gate.
    streaks: Mapping[str, int] = field(default_factory=dict)

    @property
    def faulted(self) -> bool:
        """Whether any fault is currently past the persistence gate."""
        return bool(self.active)

    def to_payload(self) -> dict[str, Any]:
        """Render for a diagnostic record."""
        return {
            "active": dict(self.active),
            "newly_active": dict(self.newly_active),
            "streaks": dict(self.streaks),
        }


class FaultMonitor:
    """Decide when a latched fault flag warrants telling the owner about.

    Pure state machine: no I/O and no clock, so a scheduled runtime only has to
    carry :meth:`snapshot` between invocations for the persistence gate to
    behave the way it would in a long-lived process.

    Each field latches and releases independently.  A pinch fault and a globe
    motor fault mean different things and get different remedies, so letting
    one mask the other -- or clear the other's alarm -- would lose information
    the owner needs.
    """

    def __init__(self, policy: FaultPolicy) -> None:
        self.policy = policy
        self._streaks: dict[str, int] = {}
        self._active: dict[str, str] = {}

    @property
    def active(self) -> dict[str, str]:
        """The faults currently considered worth reporting."""
        return dict(self._active)

    def snapshot(self) -> dict[str, object]:
        """Return the durable state needed to continue across invocations."""
        return {"streaks": dict(self._streaks), "active": dict(self._active)}

    def restore(self, state: dict[str, object]) -> None:
        """Restore a previously saved :meth:`snapshot` without trusting it blindly."""
        streaks = state.get("streaks")
        self._streaks = (
            {
                k: v
                for k, v in streaks.items()
                if isinstance(k, str) and isinstance(v, int) and v >= 0
            }
            if isinstance(streaks, dict)
            else {}
        )
        active = state.get("active")
        self._active = (
            {k: v for k, v in active.items() if isinstance(k, str) and isinstance(v, str)}
            if isinstance(active, dict)
            else {}
        )

    def observe(self, reading: FaultReading) -> FaultVerdict:
        """Fold one reading into the state machine and return a verdict."""
        faults = reading.faults()
        newly: dict[str, str] = {}

        for name, value in faults.items():
            # A fault that changes code mid-latch restarts the count: a
            # different code is a different fault, and reporting the new one
            # under the old one's streak would understate how fresh it is.
            if self._streaks.get(name) and self._active.get(name, value) != value:
                self._streaks[name] = 0
            self._streaks[name] = self._streaks.get(name, 0) + 1
            settled = self._streaks[name] >= self.policy.consecutive_samples
            if settled and self._active.get(name) != value:
                self._active[name] = value
                newly[name] = value

        for name in list(self._streaks):
            if name in faults:
                continue
            if name in reading.values:
                # Reported and clear: the firmware released the latch.
                self._streaks.pop(name, None)
                self._active.pop(name, None)
            # Absent from the payload entirely: unknown, so hold both the
            # streak and the latch rather than reading silence as recovery.

        return FaultVerdict(
            active=dict(self._active),
            newly_active=newly,
            streaks=dict(self._streaks),
        )


def log_motor_fault(verdict: FaultVerdict) -> None:
    """Emit the alarm line for a unit reporting a latched fault.

    Repeating it on every check while the fault stays latched is deliberate,
    for the same reason ``log_stuck`` and ``log_drawer_full`` repeat: the alarm
    notifies on the OK->ALARM transition, so continued lines hold it in ALARM
    until the firmware clears the flag.  That makes this one mail per fault
    episode rather than one per check, and re-arms it for the next one.
    """
    _LOGGER.info(
        "WATCHDOG_MOTOR_FAULT %s",
        " ".join(f"{name}={value}" for name, value in sorted(verdict.active.items())),
    )
