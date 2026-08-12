"""The one loop. Both kinds of job go through it; the executor is the only thing that differs.

    work item -> admission -> atomic claim -> execute -> verdict -> record -> release

MIDDLE LINK OF THE SCHEDULING CHAIN, and the file most people land in first, so it is stated here in
full: `admission` (may I?) -> `allocator` (which first?) -> **loop** (run exactly ONE) -> `tick` (one
pass of the whole thing) -> `clock` (pull the next tick). `run_one` is the boundary that makes the
other four possible -- it does one job and returns, so a crash costs one job, and everything above it
is free to be a plain function rather than a service.

NOTHING HERE READS `job.kind`, and that is enforced by a test rather than asked for in prose. A
branch on the kind is the second scheduler in its first commit: it starts as one `if`, accretes,
and by the time collaboration and testing are two systems with two queues and two verdict
vocabularies, no commit ever said so. The `Executor` protocol makes that divergence a type error
instead -- an LLM worker session and a deterministic `gate.py` runner are two implementations of
one three-line interface.

THE ORDER OF OPERATIONS IS THE WHOLE CONTRACT. Each step is placed against a specific failure:

* ADMISSION BEFORE THE CLAIM. Claiming and then discovering there is no capacity leaves the job
  locked by a box that is not running it -- indistinguishable from a hung run, and it clears only
  when the lease expires.
* THE CLAIM BEFORE EXECUTION. The reverse is the duplicate-execution bug this layer was extracted
  to remove.
* THE VERDICT BEFORE THE RELEASE. Release-then-record opens a window in which the job is both
  unclaimed and unanswered, so a second box takes work that is already finished. Nothing errors;
  it silently costs a run.
* THE RELEASE EVEN ON A CRASH. An executor that dies holding its claim is a job nothing will ever
  retry -- the failure mode that looks like a slow run, forever.

A CRASHED EXECUTOR IS INCONCLUSIVE, NEVER FAIL. A box that fell over has said nothing about the
code under test; recording FAIL would convert it into evidence against a diff. That is the
unearned RED, and it is the same defect as the unearned green with its sign flipped.

THE CALLER CAN ALWAYS TELL WHAT HAPPENED. `run_one` returns an :class:`Outcome` rather than logging
one -- a warning on an unchanged success return is the forbidden shape, and a scheduler that cannot
distinguish "refused, try later" from "someone else has it" from "the box died" cannot make its
next decision.

ADMISSION IS THE DOOR; `scaling` IS THE ROOM. `Box.blockers` is asked ONCE, before the claim, so the
fleet's whole answer to "this workstation just got busy" was to decline the NEXT item while the job
already running kept the machine. `run_regulated` is the same ordering with a capacity RE-READ
inside it: the width of a run in flight follows the box, and the reduction reaches the caller in
:class:`RegulatedRun` rather than in a log. It is the SAME loop -- both entry points go through one
`_run`, because two copies of this ordering would be two copies free to drift about the one thing
the ordering exists to guarantee.
"""

from __future__ import annotations

import enum
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from agent_swarm.admission import (
    _CAPACITY_RESERVE_GIB,
    admission_blockers,
    capacity_blocker,
    time_blocker,
)
from agent_swarm.job import Job
from agent_swarm.scaling import Adjustment, Regulator
from agent_swarm.store import VERDICTS, Store


class Outcome(enum.Enum):
    """What one turn of the loop did. Returned, not logged: the caller acts on it."""

    ANSWERED = 'answered'
    REFUSED = 'refused'  # no capacity right now; retry next tick, nothing is wrong
    CLAIMED_BY_ANOTHER = 'claimed-by-another'  # someone else is on it; not an error either
    ALREADY_ANSWERED = 'already-answered'  # a verdict exists; re-running needs `retry=True`
    CRASHED = 'crashed'  # the executor died; recorded INCONCLUSIVE, claim released


@runtime_checkable
class Executor(Protocol):
    """Something that can do a job and answer in the one verdict vocabulary.

    THE ENTIRE POINT OF THIS FILE. A worker session driving an LLM and a runner shelling out to
    `gate.py` are conversational versus deterministic -- an attribute of the EXECUTOR, not a system
    boundary. Both satisfy this.
    """

    def execute(self, job: Job) -> tuple[str, str]:
        """Do the work and return ``(verdict, detail)``. ``verdict`` must be in :data:`VERDICTS`."""
        ...


@runtime_checkable
class RegulatedExecutor(Protocol):
    """An executor that can be RESIZED while it runs. A SEPARATE protocol, deliberately.

    Widening `Executor` with an optional regulator argument would make "regulated" the property of a
    call rather than of an executor, and every existing implementation would silently claim a
    capability it does not have. Here the type says it: something that cannot narrow itself cannot
    be passed to `run_regulated`.
    """

    def execute(self, job: Job, regulator: Regulator) -> tuple[str, str]:
        """Do the work, polling ``regulator`` at every safe point, and return ``(verdict, detail)``.

        THE POLL IS THE CONTRACT. Each `reading()` re-reads capacity and grants a width; each grant
        must be answered with `honour(...)`, and a grant of zero means STOP -- the box has been
        taken by other work and the honest answer is INCONCLUSIVE, not a result produced on a
        machine that no longer had room for it.
        """
        ...


@dataclass(frozen=True, slots=True)
class Box:
    """What the machine can offer right now. Supplied by the caller, never guessed here.

    THIS LAYER IS DEPENDENCY-FREE AND STAYS THAT WAY: reading free memory and enumerating live
    class locks are I/O, and a default of "unknown" would quietly mean "unlimited" at every call
    site that forgot to measure. `capacity_blocker` already refuses on ``None`` for exactly that
    reason -- the refusal is deliberate, not a gap.

    Attributes:
        available_gib: free memory, or ``None`` if it could not be read (which REFUSES).
        held: ``{class: is_the_holder_alive}`` for every existing class lock. A dead holder does
            not block; liveness is the caller's to determine.
        reserve_gib: headroom left for the OS and this process's own subprocesses.
    """

    available_gib: float | None
    held: dict[str, bool] = field(default_factory=dict)
    reserve_gib: float = _CAPACITY_RESERVE_GIB

    def blockers(self, job: Job) -> list[str]:
        """Every reason this box will not start ``job`` now. Empty means "may start".

        EVERY reason, not the first: a refusal naming one of three sends the reader to fix the
        wrong thing.
        """
        sharing = any(self.held.values())
        reasons = [f'class lock held by {cls}' for cls in admission_blockers(self.held, job.exclusivity)]
        for reason in (
            capacity_blocker(self.available_gib, job.ram_gib, self.reserve_gib),
            time_blocker(job.solo_seconds, job.ceiling_seconds, sharing=sharing),
        ):
            if reason:
                reasons.append(reason)
        return reasons


def run_one(job: Job, *, executor: Executor, store: Store, owner: str, box: Box, retry: bool = False) -> Outcome:
    """Take ``job`` if this box may have it, run it, record the answer, and let it go.

    Returns what happened; see :class:`Outcome`. Raises only if the executor answers with a word
    outside :data:`VERDICTS` -- a fourth state is a caller bug, and coercing it into one of the
    three would invent a result nobody measured.

    AN ANSWERED JOB IS NOT RE-RUN UNLESS ASKED. THE CLAIM ALONE DOES NOT GIVE THIS: a claim is a
    mutual-exclusion LEASE, released the moment the work finishes, so sixteen boxes racing for one
    job serialise into sixteen sequential runs -- each one correctly claimed, each one duplicated
    work, and nothing anywhere errors. The recorded verdict is what makes the job DONE.

    ``retry`` is the caller's decision and must stay so. INCONCLUSIVE is exactly the verdict worth
    re-running, and `admission.should_retry` already prices how often; deciding that here would put
    the retry policy in two places, which is how the two disagree.

    THE WIDTH OF THIS RUN IS FIXED FOR ITS WHOLE LIFE. That is correct for work that cannot be
    resized and wrong for work that can -- see `run_regulated`, which is this function with a
    capacity re-read.
    """
    return _run(job, lambda: executor.execute(job), store=store, owner=owner, box=box, retry=retry)


@dataclass(frozen=True, slots=True)
class RegulatedRun:
    """What one REGULATED turn did, including every width change along the way.

    THE REDUCTION IS IN THE RETURN OR IT DID NOT HAPPEN. A regulator that shrank a run and logged it
    is the forbidden shape with extra machinery: the caller -- the thing deciding what to start
    next, and what this box is worth -- must be able to read that this run gave capacity back.

    Attributes:
        outcome: exactly as `run_one`'s, so a caller can treat both paths alike.
        adjustments: every reading taken, in order. EMPTY when the job never ran (refused, already
            answered, claimed by another), which is the honest answer: nothing was regulated.
        final_workers: the width the executor last said it ADOPTED, or ``None`` if it never ran.
            NOT the last GRANT, and the difference is the whole handshake: a grant is a ceiling the
            executor may undercut, so reporting it would be reporting permission as if it were
            behaviour -- the same distance between a log line and a measurement.
    """

    outcome: Outcome
    adjustments: tuple[Adjustment, ...] = ()
    final_workers: int | None = None

    @property
    def yielded(self) -> bool:
        """Did this run give the box back mid-flight? A DIFFERENT EVENT FROM A CRASH.

        An INCONCLUSIVE from a yield says "someone else needed this machine"; one from a crash says
        "this box fell over". Collapsing them would make a fleet that is politely sharing look
        exactly like a fleet that is breaking.
        """
        return any(a.workers == 0 for a in self.adjustments)


def run_regulated(
    job: Job,
    *,
    executor: RegulatedExecutor,
    store: Store,
    owner: str,
    box: Box,
    regulator: Regulator,
    retry: bool = False,
) -> RegulatedRun:
    """`run_one`, plus a capacity RE-READ for as long as the job runs.

    THE DOOR IS UNCHANGED. Admission still decides whether this job may START here, in the same
    order, before the claim: regulation is added to the middle and does not reopen the entry
    decision. A job the door refuses is refused with no reading taken at all.

    WHAT THE REGULATOR CHANGES is everything after the claim. The executor polls at its own safe
    points; each poll re-reads capacity and returns a width. A width of zero is a YIELD -- the box
    has been taken by other work -- and the executor is expected to stop and answer INCONCLUSIVE,
    which is what it has actually established about the code.

    A REDUCTION THAT CANNOT BE HONOURED IS A CRASH, NOT A WARNING. `Regulator.honour` refuses a
    width above the grant and `close()` refuses an unanswered final grant, both by raising; the
    raise lands in the same broad handler that catches an executor dying, so the run records
    INCONCLUSIVE and the caller reads `Outcome.CRASHED`. Nothing is swallowed and nothing continues:
    a regulator whose grants can be ignored is decoration, and decoration on this path is worse than
    an entry gate alone, because it reads as a mechanism.

    `close()` RUNS BEFORE THE VERDICT IS ACCEPTED, inside the executor call, so an executor that
    ignored its last grant cannot deliver a PASS on the way out.
    """

    def call() -> tuple[str, str]:
        verdict, detail = executor.execute(job, regulator)
        regulator.close()
        return verdict, detail

    outcome = _run(job, call, store=store, owner=owner, box=box, retry=retry)
    adjustments = regulator.adjustments
    # `None` rather than the regulator's resting width when nothing ran: a job the door refused was
    # not regulated at all, and reporting the width it WOULD have had is a number about nothing.
    return RegulatedRun(
        outcome=outcome,
        adjustments=adjustments,
        final_workers=regulator.workers if adjustments else None,
    )


def _run(job: Job, call: Callable[[], tuple[str, str]], *, store: Store, owner: str, box: Box, retry: bool) -> Outcome:
    """THE ONE ORDERING, shared by every entry point. See the module docstring for why each step sits
    where it does; there is nothing here that is not one of those four rules.

    A SINGLE COPY BECAUSE THE ORDERING IS THE CONTRACT. A second entry point re-spelling
    admit-claim-execute-record-release would be two implementations of the one guarantee this layer
    was extracted to provide, and they would diverge at the step nobody re-read.
    """
    if box.blockers(job):
        return Outcome.REFUSED
    if not retry and store.verdict(job) is not None:
        return Outcome.ALREADY_ANSWERED
    if not store.try_claim(job, owner=owner):
        return Outcome.CLAIMED_BY_ANOTHER
    try:
        # The broad catch wraps ONLY the executor call. Widening it to cover the validation below
        # would let an executor that legitimately raises ValueError be reported as a crash -- and,
        # worse, would swallow OUR OWN bad-verdict error into an INCONCLUSIVE, which is precisely
        # the fourth-state-laundered-into-a-third the vocabulary exists to prevent.
        try:
            verdict, detail = call()
        except Exception:  # noqa: BLE001 -- ANY executor failure must still free the claim
            store.record_verdict(job, verdict='INCONCLUSIVE', detail=traceback.format_exc())
            return Outcome.CRASHED
        if verdict not in VERDICTS:
            msg = f'executor returned verdict {verdict!r}, not one of {sorted(VERDICTS)}'
            raise ValueError(msg)
        store.record_verdict(job, verdict=verdict, detail=detail)
        return Outcome.ANSWERED
    finally:
        store.release(job, owner=owner)
