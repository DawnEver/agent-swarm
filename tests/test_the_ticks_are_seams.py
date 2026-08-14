"""The two seams on `tick` are real, not decorative: a consumer without a roadmap can drive the
one-pass dispatch with its own discovery and its own ordering, and the default reproduces the
roadmap / single-queue shape exactly (so `fleet_cli` is unchanged).

WHY THESE TESTS PLANT THE SHAPE AND CALL THE REAL GUARD. A seam that exists only in a docstring is
the project's dominant defect class -- a declaration the code does not consult. So this file builds
a real `Fleet` and calls the real `tick.tick` with a caller-supplied schedule and a caller-supplied
ordering, and asserts the custom shape is what decided the pass.
"""

from __future__ import annotations

import uuid

from agent_swarm.allocator import Candidate
from agent_swarm.forge_store import ForgeStore, Role
from agent_swarm.item_index import ItemIndex
from agent_swarm.job import AGENT_TASK, TEST_RUN, Job
from agent_swarm.loop import Box, Outcome, run_one
from agent_swarm.roadmap import loads
from agent_swarm.spool import ForgePublisher, Spool
from agent_swarm.testing import RecordingForge
from agent_swarm.tick import Fleet, submit, tick

ROADMAP = """
version = 1

[[item]]
key = "first"
title = "the low-priority item"
acceptance = "a"
rem = "human"
priority = 1

[[item]]
key = "second"
title = "the high-priority item"
acceptance = "b"
rem = "human"
priority = 5
"""

BOX = Box(available_gib=64.0)


class _Gate:
    """A verifier that answers immediately."""

    def execute(self, job: Job) -> tuple[str, str]:
        return 'PASS', f'stub for {job.id}'


def _fleet(tmp_path) -> Fleet:
    forge = RecordingForge()
    namespace = f'seam-{uuid.uuid4().hex[:6]}'
    index = ItemIndex(tmp_path / 'index.json')
    gate = _Gate()
    fleet = Fleet(
        roadmap=loads(ROADMAP),
        submitter=ForgeStore(namespace, forge, role=Role.SUBMITTER, index=index),
        runner=ForgeStore(namespace, forge, role=Role.RUNNER, index=index),
        spool=Spool(tmp_path / 'spool'),
        publisher=ForgePublisher(ForgeStore(namespace, forge, role=Role.SUBMITTER, index=index)),
        executors={AGENT_TASK: gate, TEST_RUN: gate},
        owner='box-seam',
    )
    # A runner store may not create work items; the item must exist before it can be taken.
    submit(fleet)
    return fleet


def _candidates(fleet: Fleet) -> list[Candidate]:
    """The roadmap's two jobs as a caller-supplied schedule, priced by their declared priority."""
    return [Candidate(job=item.job, priority=item.priority, ready_at=0.0) for item in fleet.roadmap.items]


def _answered(report) -> list[str]:
    return [k for k, o in report.outcomes.items() if o is Outcome.ANSWERED]


def test_the_default_reproduces_the_roadmap_shape(tmp_path):
    """The seam's default must be the existing behaviour, not a second scheduler. With the roadmap
    discovery and `allocator.rank`, the higher-priority item is answered first.
    """
    fleet = _fleet(tmp_path)
    report = tick(fleet, BOX)
    assert _answered(report) == [fleet.roadmap.items[1].job.claim_key()]


def test_a_custom_picker_is_honored_over_the_default(tmp_path):
    """`allocator.rank` prefers the higher priority; a caller-supplied picker that prefers the LOWER
    priority must win. If the low-priority job is answered, the picker -- not the default -- decided.
    """
    fleet = _fleet(tmp_path)

    def lowest_first(cands, box, *, now):
        return [c.job for c in sorted(cands, key=lambda c: c.priority)]

    report = tick(fleet, BOX, candidates=_candidates(fleet), picker=lowest_first)
    assert _answered(report) == [fleet.roadmap.items[0].job.claim_key()]


def test_custom_candidates_bypass_the_roadmap_discovery(tmp_path):
    """The discovery seam: a caller with no roadmap supplies its own schedule. An EMPTY schedule must
    run nothing even though the roadmap has items -- otherwise the caller's discovery was ignored.
    """
    fleet = _fleet(tmp_path)
    report = tick(fleet, BOX, candidates=[])
    assert report.considered == []
    assert not report.outcomes


def test_a_custom_record_redirects_where_the_verdict_lands(tmp_path):
    """The record seam on `run_one`: a consumer whose verdict lives elsewhere -- refs, a CAS, a result
    store -- supplies its own recorder and keeps the admit -> claim -> execute -> release ordering.
    The default (`store.record_verdict`) writes to the work item; here the verdict goes to the caller.
    """
    forge = RecordingForge()
    namespace = f'rec-{uuid.uuid4().hex[:6]}'
    index = ItemIndex(tmp_path / 'index.json')
    job = Job(id='x', kind=TEST_RUN)
    # A runner store may not create a work item; the submitter registers it exactly once.
    ForgeStore(namespace, forge, role=Role.SUBMITTER, index=index).register(job)
    store = ForgeStore(namespace, forge, role=Role.RUNNER, index=index)
    captured: list[tuple[str, str, str]] = []

    def capture(job_, verdict, detail):
        captured.append((job_.id, verdict, detail))

    outcome = run_one(job, executor=_Gate(), store=store, owner='box-rec', box=BOX, record=capture)
    assert outcome is Outcome.ANSWERED
    assert captured == [('x', 'PASS', 'stub for x')]
    # And the store itself was NOT the recorder: the verdict never reached the work item.
    assert store.verdict(job) is None
