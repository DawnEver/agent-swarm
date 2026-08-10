"""The driver: one pass of the whole loop, for both kinds of job.

WHY THIS FILE EXISTS AT ALL -- IT IS THE COMPONENT NOBODY SPECIFIED
==================================================================

Every part of this system was built and tested in isolation and they do not, on their own, form a
loop. Writing the first end-to-end rehearsal surfaced the wiring that had no home, and rather than
absorb it into a test fixture -- where it would have been invisible, untested and unshippable --
it lives here. If the loop needs a driver, the driver is a deliverable.

Two gaps in particular were found by trying to run the thing rather than by reading it:

1. **`loop.run_one` writes verdicts straight to the store, bypassing the spool.** The spool exists
   so a 25-minute gate cannot produce nothing when a POST fails -- and nothing connected it to the
   path that actually records verdicts. `spool.SpooledStore` is the adapter that closes it, and
   without it the durability work was real but unreachable.
2. **Nothing turned recorded verdicts back into `done` for the roadmap.** `Roadmap.candidates` takes
   `done` and correctly refuses to read a store; `ForgeStore` correctly knows nothing about
   roadmaps. The join belonged to neither and therefore to nobody.

WHAT THIS DELIBERATELY DOES NOT DO: decide anything. Ordering is `allocator.rank`, admission is
`Box.blockers`, retry policy is `admission.should_retry` through the allocator, verdicts are the
executor's. This file calls them in an order and holds nothing back for itself. A `tick` that
started choosing would be the second scheduler the design refuses.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from agent_swarm.allocator import rank
from agent_swarm.forge_store import ForgeStore, Role
from agent_swarm.job import Job, JobKind
from agent_swarm.loop import Box, Executor, Outcome, run_one
from agent_swarm.roadmap import Roadmap
from agent_swarm.spool import Publisher, Spool, SpooledStore


@dataclass(frozen=True, slots=True)
class Fleet:
    """Everything one box needs to take a turn. Assembled by the operator, never guessed here.

    THE TWO STORES ARE THE POINT. `submitter` may create work items; `runner` may not, and that
    refusal is what makes concurrent creation unreachable rather than merely unlikely. Handing one
    store to both roles would put the whole fleet back in the configuration that produced eight
    duplicate items per round.
    """

    roadmap: Roadmap
    submitter: ForgeStore
    runner: ForgeStore
    spool: Spool
    publisher: Publisher
    executors: dict[JobKind, Executor]
    owner: str

    def __post_init__(self) -> None:
        if self.submitter.role is not Role.SUBMITTER or self.runner.role is not Role.RUNNER:
            msg = 'Fleet needs a SUBMITTER store and a RUNNER store; the roles are not interchangeable'
            raise ValueError(msg)


@dataclass(slots=True)
class TickReport:
    """What one pass did. `outcomes` is keyed by claim key so a caller can attribute every decision."""

    submitted: list[str] = field(default_factory=list)
    considered: list[str] = field(default_factory=list)
    outcomes: dict[str, Outcome] = field(default_factory=dict)
    published: list[str] = field(default_factory=list)
    drain_failures: list[tuple[str, str]] = field(default_factory=list)


def submit(fleet: Fleet) -> list[str]:
    """Ensure every roadmap item has a work item. THE SINGLE-WRITER HALF OF THE LOOP.

    Called by whoever owns the roadmap, exactly once per item -- not by runners, and not on every
    tick by every box. `register` uses the creation response and asks no list anything, so this is
    safe on a forge whose list lags; what is NOT safe is two processes calling it concurrently for
    the same item, which is the named residual and is a deployment contract rather than a check.
    """
    created: list[str] = []
    for item in fleet.roadmap.items:
        if fleet.submitter.work_item_number(item.job):
            continue
        fleet.submitter.register(item.job)
        created.append(item.key)
    return created


def completed_keys(fleet: Fleet) -> frozenset[str]:
    """Roadmap keys whose job has a PASS on record. THE JOIN THAT BELONGED TO NOBODY.

    `Roadmap.candidates` needs `done` and must not read a store; the store must not know what a
    roadmap is. Both refusals are right, and the consequence is that this join had no home until the
    loop was assembled.

    ONLY `PASS` COUNTS AS DONE. A FAIL is answered but not finished, and an INCONCLUSIVE is not even
    answered -- treating either as done would let a dependent item start on top of work that did not
    land, which is the one thing the `needs` graph exists to prevent.
    """
    return frozenset(item.key for item in fleet.roadmap.items if fleet.runner.verdict(item.job) == 'PASS')


def tick(fleet: Fleet, box: Box, *, now: float | None = None, retry: bool = False) -> TickReport:
    """One pass: rank what is runnable, take the first thing this box may have, answer it, publish.

    THE ORDER IS THE ONLY THING THIS FUNCTION CONTRIBUTES, and each step is somebody else's decision:

        completed_keys   -> which items are done          (the store's verdicts)
        roadmap.candidates -> which are unblocked         (the human's `needs` graph)
        allocator.rank   -> which order, and what this box may start (admission)
        run_one          -> claim, execute, record, release
        spool.drain      -> publish what was recorded

    IT WALKS THE RANKED LIST RATHER THAN TAKING THE HEAD. `rank` returns every runnable job on
    purpose: losing a claim race is ordinary, and a box that gave up on a collision would convert
    contention into idle time. It stops at the first job it actually RUNS -- one job per tick keeps
    the box's own admission arithmetic honest, since `Box` describes capacity at one instant.

    VERDICTS GO THROUGH THE SPOOL, never straight to the forge: `run_one` is handed a
    `SpooledStore`, so the verdict is on disk before anything is published and a failed POST costs a
    republish rather than the answer.
    """
    moment = time.time() if now is None else now
    report = TickReport()

    done = completed_keys(fleet)
    results = {item.key: (fleet.runner.verdict(item.job),) for item in fleet.roadmap.items}
    candidates = fleet.roadmap.candidates(
        done=done,
        results={key: tuple(v for v in verdicts if v) for key, verdicts in results.items()},
    )
    spooled = SpooledStore(fleet.runner, fleet.spool)

    for job in rank(candidates, box, now=moment):
        report.considered.append(job.claim_key())
        outcome = run_one(
            job,
            executor=_executor_for(fleet, job),
            store=spooled,
            owner=fleet.owner,
            box=box,
            retry=retry,
        )
        report.outcomes[job.claim_key()] = outcome
        if outcome in (Outcome.ANSWERED, Outcome.CRASHED):
            break

    drained = fleet.spool.drain(fleet.publisher)
    report.published = drained.published
    report.drain_failures = drained.failed
    return report


def _executor_for(fleet: Fleet, job: Job) -> Executor:
    """The executor for this KIND. A missing one RAISES rather than skipping the job.

    Skipping would leave the job claimed-and-abandoned until its lease expired, then repeat forever
    -- a fleet that looks busy and completes nothing. A fleet configured without an executor for a
    kind it schedules is a configuration error and must say so at the first tick.
    """
    executor = fleet.executors.get(job.kind)
    if executor is None:
        msg = f'no executor configured for {job.kind.value!r}; this fleet cannot run {job.claim_key()}'
        raise KeyError(msg)
    return executor
