"""The one loop. Both kinds of job go through it; the executor is the only thing that differs.

    work item -> admission -> atomic claim -> execute -> verdict -> record -> release

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
"""

from __future__ import annotations

import enum
import traceback
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from agent_swarm.admission import (
    _CAPACITY_RESERVE_GIB,
    admission_blockers,
    capacity_blocker,
    time_blocker,
)
from agent_swarm.job import Job
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
            verdict, detail = executor.execute(job)
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
