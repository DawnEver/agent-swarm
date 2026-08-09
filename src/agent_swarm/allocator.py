"""ALLOCATION: which of the work this box MAY do should it do NEXT. The choosing half.

Elastic allocation is REFUSE plus CHOOSE, and only refuse existed. `admission` answers "may this run
here"; `store.try_claim` answers "who gets it"; nothing answered "which one". A fleet with no chooser
either idles or takes whatever is first in a list -- and a list order nobody designed is a scheduling
policy nobody wrote down.

IT RANKS; IT DOES NOT REFUSE. Every capacity, class and retry question is delegated -- `Box.blockers`
for the first two, `admission.should_retry` for the third. Re-deriving any of them here would be the
duplicated-scheme defect this package was extracted to end: two modules deciding one thing are two
modules free to drift, and the drift shows up as a box refusing work it could do, or taking work it
cannot.

PURE. No clock, no filesystem, no randomness -- `now` is an argument. This is the layer that must be
replayed to explain why a box did what it did, and a function that reads the world cannot be
replayed. It is also why the tie-break is deterministic rather than random (see `rank`).

THE FAILURE MODE IT IS DESIGNED AGAINST IS STARVATION, WHICH IS SILENT. Under highest-priority-first,
a low-priority item is never picked while urgent work keeps arriving; every individual decision is
correct, the fleet is busy, throughput looks fine, and one item is simply never done. Nothing errors
and no log line fires. So the score AGES: see `score` and `starvation_bound_s`, and
`tests/test_allocator.py::TestStarvation::test_a_starved_job_is_eventually_picked`, which is written
to FAIL against the naive rule.
"""

from __future__ import annotations

from dataclasses import dataclass

from agent_swarm.admission import should_retry
from agent_swarm.job import Job
from agent_swarm.loop import Box

#: The closed priority range. THE RANGE IS WHAT MAKES THE STARVATION BOUND REAL: with an open-ended
#: priority, no finite wait overtakes a job somebody labelled 10_000, and `starvation_bound_s` would
#: be a docstring asserting a property the code does not enforce. Out of range RAISES.
PRIORITY_MIN = 0
PRIORITY_MAX = 9

#: How long a job must wait to gain one priority level. AN OPERATOR CONSTANT, NOT A MEASUREMENT --
#: it prices "how much urgency is one hour of waiting worth", which is a policy question about the
#: fleet's owners and not something this code can derive. It is a default, overridable per call, and
#: it is stated here rather than at a call site so that changing it is one visible edit.
DEFAULT_AGEING_SECONDS = 900.0


@dataclass(frozen=True, slots=True)
class Candidate:
    """A job plus the scheduling facts that are NOT properties of the job itself.

    WHY NOT FIELDS ON `Job`. A job's cost (`ram_gib`, `solo_seconds`) is intrinsic and travels with
    it; its priority and how long it has been waiting are properties of a QUEUE at a MOMENT. Putting
    them on the frozen job would mean rebuilding the job every tick just because time passed, and a
    job whose identity changed with the clock is a job whose claim key could change under a running
    claim.

    Attributes:
        job: the work itself. Its cost fields are read by `Box.blockers`, never here.
        priority: the human-declared urgency, in ``[PRIORITY_MIN, PRIORITY_MAX]``. Out of range
            raises, because the closed range is what makes the starvation bound true.
        ready_at: the epoch seconds at which this became runnable -- when the issue was filed, when
            a dependency was satisfied, when the freshness deadline lapsed. Age is measured from
            here, so a job that was blocked for a day does not arrive pre-aged past everything.
        results: the verdicts already recorded for it, oldest first. Passed to
            `admission.should_retry`; not interpreted here.
        max_retries: the retry budget, priced by whoever owns the queue.
    """

    job: Job
    priority: int = PRIORITY_MIN
    ready_at: float = 0.0
    results: tuple[str, ...] = ()
    max_retries: int = 3

    def __post_init__(self) -> None:
        if not isinstance(self.priority, int) or not (PRIORITY_MIN <= self.priority <= PRIORITY_MAX):
            msg = (
                f'priority must be an int in [{PRIORITY_MIN}, {PRIORITY_MAX}], got {self.priority!r}. '
                f'An open-ended priority makes the starvation bound unreachable.'
            )
            raise ValueError(msg)


def starvation_bound_s(priority: int, ageing_seconds: float = DEFAULT_AGEING_SECONDS) -> float:
    """How long a ``priority`` job can wait before it outranks ANY fresher job. THE DECLARED BOUND.

    Ageing is linear at one priority level per ``ageing_seconds``, so a job's score is
    ``priority + age / ageing_seconds``. The worst case is a queue into which a fresh
    ``PRIORITY_MAX`` job arrives forever: their score never exceeds ``PRIORITY_MAX``, so once this
    job's age passes ``(PRIORITY_MAX - priority) * ageing_seconds`` it outranks all of them.

    STRICTLY PAST, not at. AT the bound the scores are exactly equal and the tie-break -- a claim-key
    sort, deliberately unrelated to age -- decides, so the guarantee is stated for ``age > bound``.

    THIS FUNCTION EXISTS TO BE CONSULTED, NOT REMEMBERED. The bound and the ranking are computed from
    the same two numbers, so a change to the ageing law that broke the bound cannot leave a correct
    sentence behind in a docstring. The test asserts against this function's answer, not a literal.

    It is a bound on being PICKED, never on being DONE: the pick is followed by a claim this box can
    lose, and by an executor that can crash. What it rules out is the queue never OFFERING the job.
    """
    return (PRIORITY_MAX - priority) * ageing_seconds


def score(candidate: Candidate, *, now: float, ageing_seconds: float = DEFAULT_AGEING_SECONDS) -> float:
    """The aged priority of ``candidate``. Higher is picked sooner.

    Age is clamped at zero: a job whose ``ready_at`` is in the future has not started waiting, and a
    negative age would let a future-dated job be RANKED DOWN rather than held back -- which is a
    different thing from not being offered. `rank` excludes it outright.
    """
    age = max(0.0, now - candidate.ready_at)
    return candidate.priority + age / ageing_seconds


def rank(
    candidates: list[Candidate],
    box: Box,
    *,
    now: float,
    ageing_seconds: float = DEFAULT_AGEING_SECONDS,
) -> list[Job]:
    """Every job this box may start now, best first. EVERY one, deliberately.

    THE LOSER OF A CLAIM RACE NEEDS THE REST OF THE LIST. Returning only the winner would force a
    box that lost `try_claim` to recompute against a queue that has since moved, or -- far likelier
    -- to give up until the next tick, which converts a collision into idle time. The whole list is
    the same computation and makes the retry free.

    THE TIE-BREAK IS DETERMINISTIC, AND THAT IS A CHOICE WITH A REASON. Every box scoring the same
    queue reaches the SAME order, so two boxes DO collide on the top job -- and `store.try_claim` is
    a compare-and-swap that resolves it: one wins, the loser walks down this list. That path is
    exercised on every collision, so it is tested by ordinary operation. Random spreading would make
    collisions rare instead of correct, which means the arbitration code would run seldom enough to
    rot unnoticed -- and it would also destroy replay, since two identical queues could yield two
    different decisions with no record of why.

    The key is `Job.claim_key`, which already distinguishes kind, id and shard -- the same identity
    the store contends on, so the ordering cannot disagree with what is being claimed.

    ADMISSION AND THE RETRY BUDGET ARE CONSULTED, NEVER RE-DERIVED: `Box.blockers` and
    `admission.should_retry`. An unpriced job therefore stays schedulable exactly as far as
    admission says it does -- there is no price test here to add a second opinion.
    """
    eligible = [
        c
        for c in candidates
        if c.ready_at <= now and should_retry(list(c.results), c.max_retries) and not box.blockers(c.job)
    ]
    eligible.sort(key=lambda c: (-score(c, now=now, ageing_seconds=ageing_seconds), c.job.claim_key()))
    return [c.job for c in eligible]


def choose(
    candidates: list[Candidate],
    box: Box,
    *,
    now: float,
    ageing_seconds: float = DEFAULT_AGEING_SECONDS,
) -> Job | None:
    """The one job to attempt next, or ``None`` if this box may start none of them.

    ``None`` means "nothing right now", never "nothing ever" and never an error: a full box, an
    empty queue and a queue of exhausted retries are all legitimate, and the caller's response to
    all three is to tick again.
    """
    ordered = rank(candidates, box, now=now, ageing_seconds=ageing_seconds)
    return ordered[0] if ordered else None
