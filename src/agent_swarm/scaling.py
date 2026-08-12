"""CONTINUOUS REGULATION: the half of the capacity story that admission cannot express.

THE GAP THIS CLOSES, and it is a gap in the MIDDLE rather than at either end. The compute substrate
is a human-observable TERMINAL: opening one enrols this box, closing it withdraws the box, and
`lifetime.py` binds that to the kernel so the promise is not prose. Between those two events sat
nothing. `admission` is asked ONCE, before the claim -- `loop`'s `Outcome.REFUSED` is its whole
vocabulary -- so when the workstation acquires other work MID-RUN, the only response the fleet has
is to decline the NEXT item while the job already running keeps the entire machine.

    door      admission.capacity_blocker  -> may this job START here?     asked once
    room      scaling.Regulator           -> how wide may it run NOW?     asked every safe point
    exit      lifetime                    -> the terminal closed          the kernel answers

A WIDTH IS DERIVED, NEVER FIXED AT START. `workers_for` is the arithmetic and it is pure: free
memory minus a declared reserve, divided by what one worker was MEASURED to cost, floored, and
capped by the width the caller asked for. Capacity may only LOWER the caller's ceiling -- a box with
plenty free does not get to invent workers for work nobody priced at that width.

COOPERATIVE, NOT SUPERVISED, AND THIS IS THE LOAD-BEARING CHOICE. There is no thread here, no
watchdog and no daemon: a supervisor could only resize a pool it owns, and the thing that knows
where a run's safe points are is the run. So the executor POLLS, and the poll re-reads capacity.
The cost is honest and stated: a job that never polls is never regulated, which is why the handshake
below makes "never polled" and "polled and obeyed" different observable things.

THE HANDSHAKE IS THE POINT, NOT THE ARITHMETIC. A regulator that computed a smaller number and
logged it would be the forbidden shape exactly: a capacity reduction the caller cannot observe,
dressed as diligence. So a grant must be ANSWERED -- `honour(n)` reports the width actually adopted
-- and an unanswered or exceeded grant RAISES (`WidthNotHonoured`). Running NARROWER than granted is
always legal, because the grant is a ceiling and under-using a box is safe in the direction that
matters.

UNREADABLE IS NOT "PLENTY", AND IT IS NOT "NOTHING" EITHER. `capacity_blocker` refuses at the door
when memory cannot be read; mid-run there is no refusal available, so `CapacityUnreadable` is raised
and the run ends saying nothing -- an INCONCLUSIVE, which is what a box that lost its instrument has
actually established. Picking whichever reading lets the run continue is the shape this package
exists to refuse.

NO HYSTERESIS, AND THAT IS AN ADMISSION RATHER THAN A DESIGN. Each reading is independent, so a
capacity figure oscillating around a worker boundary makes the width oscillate with it. Damping it
needs a MEASUREMENT of how a real neighbour's load actually moves, and no such measurement exists;
inventing a time constant here would put a made-up number on the blocking path of every run. The
executor polls at ITS safe points, which already bounds how often this can happen.
"""

from __future__ import annotations

import enum
from collections.abc import Callable
from dataclasses import dataclass


class CapacityUnreadable(RuntimeError):
    """Free memory could not be read, so no width can be derived.

    RAISED, NEVER FOLDED INTO A NUMBER. Zero would stop a run that may be perfectly fine; the
    caller's ceiling would promise a fit nobody measured. Both are answers invented to avoid an
    exception, and the exception is the only true statement available.
    """


class WidthNotHonoured(RuntimeError):
    """The executor was granted a width and either exceeded it or never said what it adopted.

    THIS IS THE DEFECT THE MODULE EXISTS TO MAKE VISIBLE. Without it, an executor that ignores a
    shrink is indistinguishable from one that obeys: same return value, same verdict, and a log full
    of reductions that never happened. Raising converts it into a crash the caller sees.
    """


class Regulation(enum.Enum):
    """What one reading did to the width. Returned, never logged: the caller acts on it."""

    UNCHANGED = 'unchanged'
    GREW = 'grew'  # capacity came back; the ceiling the caller asked for is the only bound
    SHRANK = 'shrank'  # someone else is using this box; this run gives some of it up
    YIELDED = 'yielded'  # nothing left to run with; the job must stop rather than run on


@dataclass(frozen=True, slots=True)
class Adjustment:
    """One reading and what it decided. The unit the caller observes a reduction THROUGH.

    Attributes:
        regulation: which way the width moved.
        workers: the width granted from now on. ``0`` means YIELD -- stop, do not run narrower.
        previous: the width in force before this reading. The first reading measures against the
            width the caller ASKED for, so a run that starts constrained is itself a SHRANK rather
            than looking identical to an unconstrained one.
        available_gib: the reading this was derived from, so a report can be checked rather than
            believed.
        reason: a sentence naming the arithmetic. Never empty, including for UNCHANGED.
    """

    regulation: Regulation
    workers: int
    previous: int
    available_gib: float
    reason: str


def workers_for(available_gib: float | None, *, per_worker_gib: float, reserve_gib: float, max_workers: int) -> int:
    """How many workers this much free memory supports, capped by what the caller asked for.

    EVERY ARGUMENT IS REQUIRED AND NONE IS DEFAULTED. What one worker costs is a MEASUREMENT the
    consumer took of its own work, and the width it wants is its own too; a default here would make
    every caller that omitted one silently adopt another project's numbers, which is the exact
    mechanism `default_forge`'s old `DEFAULT_REPO` used to stay invisible.

    THE RESERVE IS SUBTRACTED BEFORE THE DIVISION, matching `admission.capacity_blocker`: the OS,
    this process's own subprocesses and in-run growth are not available to workers, and "it happened
    to fit last time" is the reasoning a 0.33 GB sample already refuted.

    Raises:
        CapacityUnreadable: ``available_gib`` is ``None``. Unknown must never become unlimited by
            arithmetic.
        ValueError: a worker priced at zero or less, which divides to an unbounded width.
    """
    if available_gib is None:
        raise CapacityUnreadable('cannot read available memory, so no width can be derived from it')
    if per_worker_gib <= 0:
        msg = f'per_worker_gib must be a positive measurement, got {per_worker_gib!r}'
        raise ValueError(msg)
    usable = available_gib - reserve_gib
    if usable <= 0:
        return 0
    return max(0, min(max_workers, int(usable // per_worker_gib)))


class Regulator:
    """A live capacity source plus the handshake that makes obeying it observable.

    NOT A SCHEDULER AND NOT A SUPERVISOR. It holds no queue, spawns nothing, and knows nothing about
    jobs, populations, barriers or generations -- it answers one question, "how wide, now", and
    records what the executor said it did about the answer.

    THE CAPACITY SOURCE IS THE CALLER'S. Reading free memory is I/O and this layer decides rather
    than reaches; `procs` is where the measurement lives, behind an extra, and passing it in keeps
    this module testable against a source that CHANGES -- which is the only kind of source that can
    distinguish continuous regulation from a width computed once.
    """

    __slots__ = ('_adjustments', '_capacity', '_max_workers', '_pending', '_per_worker_gib', '_reserve_gib', '_workers')

    def __init__(
        self,
        capacity: Callable[[], float | None],
        *,
        per_worker_gib: float,
        reserve_gib: float,
        max_workers: int,
    ) -> None:
        if max_workers < 1:
            msg = f'max_workers must be at least 1, got {max_workers!r}; a run of width zero is not a run'
            raise ValueError(msg)
        self._capacity = capacity
        self._per_worker_gib = per_worker_gib
        self._reserve_gib = reserve_gib
        self._max_workers = max_workers
        #: The width in force. Starts at what the caller ASKED for, so the first reading is a real
        #: regulation against that ask rather than a self-fulfilling UNCHANGED.
        self._workers = max_workers
        #: A grant issued and not yet answered. `None` means the handshake is settled.
        self._pending: int | None = None
        self._adjustments: list[Adjustment] = []

    @property
    def adjustments(self) -> tuple[Adjustment, ...]:
        """Every reading this run has taken, in order. What the caller reports a shrink FROM."""
        return tuple(self._adjustments)

    @property
    def workers(self) -> int:
        """The width in force right now -- what the executor last said it adopted."""
        return self._workers

    def reading(self) -> Adjustment:
        """Re-read capacity and grant a width for the work from here on.

        Raises:
            WidthNotHonoured: the previous grant was never answered. Checked BEFORE the new reading
                so an executor cannot poll its way past a grant it ignored.
            CapacityUnreadable: the source could not answer.
        """
        self._settle()
        available = self._capacity()
        granted = workers_for(
            available,
            per_worker_gib=self._per_worker_gib,
            reserve_gib=self._reserve_gib,
            max_workers=self._max_workers,
        )
        # `available` cannot be None below: `workers_for` OWNS that refusal and has already raised.
        # Re-checking it here would be a second copy of the rule, free to disagree with the first.
        adjustment = Adjustment(
            regulation=self._regulation(granted),
            workers=granted,
            previous=self._workers,
            available_gib=available,
            reason=(
                f'{available:.1f} GiB available less {self._reserve_gib:.1f} GiB reserve, at '
                f'{self._per_worker_gib:.1f} GiB per worker, capped at {self._max_workers}: '
                f'{self._workers} -> {granted}'
            ),
        )
        self._adjustments.append(adjustment)
        self._pending = granted
        return adjustment

    def honour(self, workers: int) -> None:
        """Report the width actually adopted. THE HALF THAT MAKES A SHRINK NON-SILENT.

        NARROWER IS ALWAYS ALLOWED: the grant is a ceiling, and under-using the box fails in the
        harmless direction. Wider is refused, including the case that matters most -- answering a
        YIELD (a grant of zero) with anything but zero.

        Raises:
            WidthNotHonoured: the reported width exceeds the grant, or no grant is outstanding.
        """
        if self._pending is None:
            msg = f'honour({workers}) with no grant outstanding; call reading() first'
            raise WidthNotHonoured(msg)
        if workers > self._pending:
            msg = (
                f'granted {self._pending} worker(s) and adopted {workers}: a reduction the caller '
                f'cannot rely on is worse than no regulation at all'
            )
            raise WidthNotHonoured(msg)
        self._workers = workers
        self._pending = None

    def close(self) -> None:
        """Settle the handshake at the end of a run.

        THE HOLE A NEXT-READING-ONLY CHECK WOULD LEAVE: honour every grant but the LAST and the run
        finishes with a reduction nobody ever adopted and nothing ever noticed. Called after the
        executor returns and BEFORE its verdict is accepted, so the crash outranks the answer.
        """
        self._settle()

    def _settle(self) -> None:
        if self._pending is not None:
            msg = (
                f'granted {self._pending} worker(s) and never said what was adopted; an unanswered '
                f'grant is indistinguishable from an obeyed one, which is the whole defect'
            )
            raise WidthNotHonoured(msg)

    def _regulation(self, granted: int) -> Regulation:
        if granted == 0:
            return Regulation.YIELDED
        if granted < self._workers:
            return Regulation.SHRANK
        if granted > self._workers:
            return Regulation.GREW
        return Regulation.UNCHANGED
