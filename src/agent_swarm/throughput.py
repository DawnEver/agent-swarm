"""HOW CLOSE THE FLEET IS TO ITS CEILING -- which one series cannot say.

THE MISREAD THIS MODULE IS SHAPED AROUND. Count the integrations per day and the graph goes down.
Two things produce that fall and they are INDISTINGUISHABLE in a count series: less work arriving,
or a box that got slower. They call for opposite responses -- one is "we have headroom", the other
is "we are at the ceiling and work is queueing" -- so a monitor that records only the count is not
an incomplete monitor, it is a confidently wrong one.

So both series are mandatory, and the quantity that answers the actual question is neither of them
but their RATIO: integrations achieved against integrations POSSIBLE, where possible is derived from
the observed gate duration. :func:`compare` is what a count series cannot do -- it separates the two
readings by holding one term against the other.

THE CALIBRATION, so the arithmetic can be checked rather than trusted. Measured 2026-08-12: one full
fast gate is 704 s and occupies the whole box. That is 3600/704 = 5.11 per hour, and over the
available hours below, 40.9 -- the "<=40/day at zero retries" the measurement was reported as.
Observed demand was ~10/day, so saturation ~0.25. Those numbers are HERE, in prose, and NOT constants
below: a stored 704.0 is a figure that drifts silently from the box it described, and the whole
point is that the duration is MEASURED per window. What is checked is that the formula reproduces
the calibration -- see `test_throughput_cannot_read_a_slow_box_as_falling_demand.py`.

RETRIES ARE NOT MODELLED AND THAT IS DELIBERATE. The ceiling is stated at zero retries, which is an
UPPER bound; a fleet that re-gates a third of its submissions is nearer the ceiling than this says.
Reported the other way -- an optimistic saturation on a pessimistic ceiling -- the number would
understate the pressure it exists to reveal, so the bound leans toward "you have less room than
this", which is the safe direction for a capacity figure.
"""

from __future__ import annotations

import statistics
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date

__all__ = [
    'AVAILABLE_HOURS_PER_DAY',
    'DEMAND_FELL',
    'DEMAND_ROSE',
    'THE_BOX_SLOWED',
    'THE_BOX_SPED_UP',
    'UNCHANGED',
    'Integration',
    'Throughput',
    'ThroughputChange',
    'UnmeasuredIntegration',
    'Window',
]

SECONDS_PER_HOUR = 3600.0

#: Hours per day a box is actually available to spend on verdicts. NOT 24: the measured ceiling was
#: reported as <=40/day from a 5.11/hour rate, and 40/5.11 is 7.8 -- a working day, because the
#: participant that opens the terminal is the one that closes it, and a box leaves the fleet when
#: its window does. Stated as a named input rather than folded into a formula so that a fleet with
#: a different working pattern changes ONE readable number and every derived figure follows.
AVAILABLE_HOURS_PER_DAY = 8.0

#: The readings :func:`compare` can return. Strings rather than an enum for the reason the rest of
#: this package uses reason strings: the caller renders them, and a bool would throw away which of
#: the two indistinguishable causes was found.
DEMAND_ROSE = 'more work arrived at the same gate cost'
DEMAND_FELL = 'less work arrived at the same gate cost'
THE_BOX_SLOWED = 'the gate got more expensive; a lower count here is capacity, not demand'
THE_BOX_SPED_UP = 'the gate got cheaper; a higher count here is capacity, not demand'
UNCHANGED = 'neither the count nor the gate cost moved materially'

#: How far either series may move before :func:`compare` calls it a change. A monitor with no dead
#: band reports a reading on every rounding difference, and a reading that always fires carries no
#: information -- the same failure as a report section that always prints.
MATERIAL_CHANGE = 0.10


class UnmeasuredIntegration(ValueError):
    """An integration recorded without its gate duration, or a figure asked of a window that has
    none.

    Both are the same defect: a count that nobody can interpret. Refused at the door rather than
    served as a plausible number, because a saturation computed from no durations reads as
    "plenty of room" on exactly the evidence that cannot support it.
    """


@dataclass(frozen=True)
class Integration:
    """One integration that reached `main`, with what its verdict COST.

    The duration is the whole gate wall-clock, not the test time: the scarce resource is the box's
    occupancy, and setup, collection and teardown occupy it identically.
    """

    day: date
    gate_seconds: float

    def __post_init__(self) -> None:
        if not self.gate_seconds > 0:
            raise UnmeasuredIntegration(
                f'an integration on {self.day} was recorded with gate_seconds={self.gate_seconds!r}; '
                'a count without its duration cannot tell falling demand from a slower box'
            )


@dataclass(frozen=True)
class Window:
    """A span of days, both series intact.

    `days` IS CARRIED SEPARATELY from the recorded integrations because a window with no
    integrations still has a length, and dividing by "the number of days that had activity" would
    make an idle fleet look fully busy on the one day it moved.
    """

    days: int
    gate_seconds: tuple[float, ...]

    def __post_init__(self) -> None:
        if self.days <= 0:
            raise UnmeasuredIntegration(f'a window spans at least one day, got {self.days!r}')
        object.__setattr__(self, 'gate_seconds', tuple(self.gate_seconds))

    @property
    def integrations(self) -> int:
        return len(self.gate_seconds)

    @property
    def per_day(self) -> float:
        """Integrations achieved per day -- the series that is ambiguous ON ITS OWN."""
        return self.integrations / self.days

    def _measured(self) -> tuple[float, ...]:
        if not self.gate_seconds:
            raise UnmeasuredIntegration(
                'this window recorded no gate duration, so it cannot state a ceiling; '
                'reporting 0.0 saturation here would read as headroom on no evidence'
            )
        return self.gate_seconds

    @property
    def mean_gate_seconds(self) -> float:
        return statistics.fmean(self._measured())

    @property
    def median_gate_seconds(self) -> float:
        """Carried beside the mean because one 40-minute retry drags a mean and not a median, and a
        reader comparing the two learns whether the window is uniform or has a tail.
        """
        return statistics.median(self._measured())

    @property
    def per_hour(self) -> float:
        """Verdicts an occupied box can turn out in an hour, at the observed cost. Serial: a gate
        takes the WHOLE box, so this does not multiply by cores.
        """
        return SECONDS_PER_HOUR / self.mean_gate_seconds

    @property
    def ceiling_per_day(self) -> float:
        """Integrations the fleet COULD do per day at this gate cost, at zero retries."""
        return self.per_hour * AVAILABLE_HOURS_PER_DAY

    @property
    def saturation(self) -> float:
        """`per_day / ceiling_per_day` -- how close to the ceiling, and the only figure here that
        answers the question the monitor exists for. Above 1.0 means the window integrated more
        than one box could produce, i.e. the fleet is wider than one box or the window overlapped.
        """
        return self.per_day / self.ceiling_per_day

    @property
    def inputs(self) -> dict[str, float | int]:
        """Every term the derived figures are built from.

        A computed property whose inputs are not readable is a number the reader must take on
        trust, and the first disagreement about it is unresolvable.
        """
        return {
            'days': self.days,
            'integrations': self.integrations,
            'per_day': self.per_day,
            'mean_gate_seconds': self.mean_gate_seconds,
            'median_gate_seconds': self.median_gate_seconds,
            'available_hours_per_day': AVAILABLE_HOURS_PER_DAY,
            'per_hour': self.per_hour,
            'ceiling_per_day': self.ceiling_per_day,
            'saturation': self.saturation,
        }


@dataclass(frozen=True)
class ThroughputChange:
    """Two windows held against each other, with the reading a single series cannot produce.

    Every field is a RATIO between two operating points, never a value at one. A pinned absolute --
    "the gate takes 704 s" -- is invisible to any test written against the box that produced it and
    becomes a lie on the next machine; a ratio states a relationship that either holds or does not.
    """

    count_ratio: float
    duration_ratio: float
    saturation_ratio: float
    reading: str


def _moved(ratio: float) -> int:
    """-1, 0 or +1 -- which side of the dead band a ratio fell on."""
    if ratio > 1 + MATERIAL_CHANGE:
        return 1
    if ratio < 1 - MATERIAL_CHANGE:
        return -1
    return 0


def compare(before: Window, after: Window) -> ThroughputChange:
    """What actually changed between two windows.

    THE DURATION IS READ FIRST, and that ordering is the whole point: when the gate cost moved, a
    change in count is a consequence of capacity and calling it demand is the misread this module
    is named after. Only when the cost held still does the count series mean what it appears to.

    Raises :class:`UnmeasuredIntegration` if either window recorded no duration -- there is no
    comparison to make, and returning UNCHANGED would state agreement between two things that were
    never measured.
    """
    count_ratio = after.per_day / before.per_day if before.per_day else float('inf')
    duration_ratio = after.mean_gate_seconds / before.mean_gate_seconds
    saturation_ratio = after.saturation / before.saturation if before.saturation else float('inf')

    if (cost := _moved(duration_ratio)) != 0:
        reading = THE_BOX_SLOWED if cost > 0 else THE_BOX_SPED_UP
    elif (work := _moved(count_ratio)) != 0:
        reading = DEMAND_ROSE if work > 0 else DEMAND_FELL
    else:
        reading = UNCHANGED
    return ThroughputChange(count_ratio, duration_ratio, saturation_ratio, reading)


class Throughput:
    """The monitor. Append-only in use; a window is a read over what was recorded.

    IT HOLDS NO CLOCK AND NO STORE. The day of an integration and the span of a window arrive from
    the caller, so this is testable without freezing time and reusable by a consumer whose records
    live somewhere this package must not know about.
    """

    def __init__(self, integrations: Iterable[Integration] = ()) -> None:
        self._integrations: list[Integration] = list(integrations)

    def record(self, day: date, *, gate_seconds: float) -> Integration:
        """Record one integration. BOTH arguments, or :class:`UnmeasuredIntegration`."""
        entry = Integration(day=day, gate_seconds=float(gate_seconds))
        self._integrations.append(entry)
        return entry

    @property
    def integrations(self) -> tuple[Integration, ...]:
        return tuple(self._integrations)

    def days_covered(self) -> tuple[date, ...]:
        """The distinct days that saw an integration, sorted. NOT the window length -- see
        :class:`Window`; an idle day is part of the span and is invisible here by construction.
        """
        return tuple(sorted({entry.day for entry in self._integrations}))

    def window(self, *, days: int, ending: date | None = None) -> Window:
        """The last `days` days, ending on `ending` (default: the latest day recorded).

        `days` HAS NO DEFAULT. "Recently" is the caller's question and a default here would put a
        span nobody chose underneath every saturation figure this module reports.
        """
        if not self._integrations:
            return Window(days=days, gate_seconds=())
        last = ending if ending is not None else max(entry.day for entry in self._integrations)
        first_ordinal = last.toordinal() - days + 1
        seconds = [
            entry.gate_seconds
            for entry in self._integrations
            if first_ordinal <= entry.day.toordinal() <= last.toordinal()
        ]
        return Window(days=days, gate_seconds=tuple(seconds))

    def daily_counts(self) -> dict[date, int]:
        """Integrations per day, for the days that had any. The count series, exposed as data --
        never as the answer; anything reading this alone is the misread in the module docstring.
        """
        counts: dict[date, int] = {}
        for entry in self._integrations:
            counts[entry.day] = counts.get(entry.day, 0) + 1
        return dict(sorted(counts.items()))

    def daily_gate_seconds(self) -> dict[date, tuple[float, ...]]:
        """The duration series, day by day. The other half, and it is why the first is readable."""
        durations: dict[date, list[float]] = {}
        for entry in self._integrations:
            durations.setdefault(entry.day, []).append(entry.gate_seconds)
        return {day: tuple(seconds) for day, seconds in sorted(durations.items())}

    def render(self, window: Window) -> list[str]:
        """The window as lines a human reads, with the inputs beside the derived figures."""
        if not window.gate_seconds:
            return [f'throughput: no integration measured in the last {window.days} day(s)']
        inputs: Sequence[tuple[str, float | int]] = tuple(window.inputs.items())
        width = max(len(name) for name, _ in inputs)
        headline = (
            f'throughput over {window.days} day(s): '
            f'{window.per_day:.2f}/day against a ceiling of {window.ceiling_per_day:.1f}/day '
            f'({window.saturation:.0%} of capacity)'
        )
        return [
            headline,
            *(
                f'  {name:<{width}}  {value:.2f}' if isinstance(value, float) else f'  {name:<{width}}  {value}'
                for name, value in inputs
            ),
        ]
