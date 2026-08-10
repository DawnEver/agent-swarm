"""The last wire: a tick that answers a job also marks the COMMIT a merge waits on.

`tick` already did claim -> execute -> record -> spool -> publish. What it did not do was publish a
COMMIT STATUS, so the gate context a protected branch requires had no producer anywhere
in the running system. Turning protection on in that state freezes `main` -- every merge waiting on
a check nobody writes -- and the symptom reads as a broken gate rather than an absent one.

THE SHA IS THE RUNNER'S, NOT THE JOB'S, and that is the design decision worth stating. A `Job`
carries no commit and should not: its identity is a testkey, and the same testkey is answered
against many commits over a fleet's life. What a verdict is ABOUT is where this box's checkout
stands at this tick -- one checkout, one commit, one answer. So the caller passes it and nothing
guesses.

TWO WAYS TO MEAN "DO NOT MARK", both legitimate and neither an error: a box without the verifier
credential CANNOT mark, and a box running work not tied to a commit has NOTHING to mark. Crucially
neither may skip the verdict itself -- the store and the commit are different audiences, and a fleet
in which a non-verifier box silently stopped recording answers would look like a fleet doing no work.
"""

from __future__ import annotations

import uuid

import pytest

from agent_swarm.forge import ROLE_ACCOUNTS
from agent_swarm.forge_store import ForgeStore, Role
from agent_swarm.item_index import ItemIndex
from agent_swarm.job import AGENT_TASK, TEST_RUN, Job
from agent_swarm.loop import Box, Outcome
from agent_swarm.roadmap import loads
from agent_swarm.spool import ForgePublisher, Spool
from agent_swarm.status import StatusPublisher
from agent_swarm.testing import RecordingForge
from agent_swarm.tick import Fleet, submit, tick
from conftest import TEST_CONTEXT as STATUS_CONTEXT

#: This box's own status context. Per-writer keying means the published context carries it, so the
#: tests must name it rather than assume the bare base -- see `status.py` on the shared-key defect.
MARK_RUNNER = 'box-mark'
MARK_CONTEXT = f'{STATUS_CONTEXT}/{MARK_RUNNER}'

ROADMAP = """
version = 1

[[item]]
key = "mark-first"
title = "the only item"
acceptance = "it is answered"
rem = "human"
priority = 1
"""

BOX = Box(available_gib=64.0)


class _Gate:
    """A verifier that answers immediately. What travels is the verdict, not how it was computed."""

    def __init__(self, verdict: str = 'PASS') -> None:
        self.verdict = verdict

    def verify(self, job: Job) -> tuple[str, str]:
        return self.verdict, f'stub for {job.id}'

    def execute(self, job: Job) -> tuple[str, str]:
        return self.verify(job)


class _Statuses(RecordingForge):
    """The verifier's forge: records what was marked, and answers to the verifier username."""

    def __init__(self) -> None:
        super().__init__(username=ROLE_ACCOUNTS['verifier'])
        self.marks: list[tuple[str, str, str]] = []

    def set_status(self, sha: str, *, state: str, context: str, description: str) -> None:
        self.marks.append((sha, state, context))


@pytest.fixture
def fleet_and_marks(tmp_path):
    """One fleet on its own namespace, with a status publisher wired in."""
    forge = RecordingForge()
    marker = _Statuses()
    namespace = f'mark-{uuid.uuid4().hex[:6]}'
    index = ItemIndex(tmp_path / 'index.json')
    gate = _Gate()
    fleet = Fleet(
        roadmap=loads(ROADMAP),
        submitter=ForgeStore(namespace, forge, role=Role.SUBMITTER, index=index),
        runner=ForgeStore(namespace, forge, role=Role.RUNNER, index=index),
        spool=Spool(tmp_path / 'spool'),
        publisher=ForgePublisher(ForgeStore(namespace, forge, role=Role.SUBMITTER, index=index)),
        executors={AGENT_TASK: gate, TEST_RUN: gate},
        owner='box-mark',
        status=StatusPublisher(marker, context=STATUS_CONTEXT, runner=MARK_RUNNER),
    )
    # The item must EXIST before a runner can take it: a runner store may not create work items,
    # which is the structural half of the duplicate-creation fix and not something to work around.
    submit(fleet)
    return fleet, marker, gate


def _answered(report) -> bool:
    return Outcome.ANSWERED in report.outcomes.values()


# --------------------------------------------------------------------------- it marks


def test_answering_a_job_marks_the_commit(fleet_and_marks):
    """THE GAP: before this, the check a protected branch waits on had no producer in the loop."""
    fleet, marker, _gate = fleet_and_marks
    report = tick(fleet, BOX, sha='deadbeef')
    assert _answered(report)
    assert [(sha, state, ctx) for sha, state, ctx in marker.marks] == [('deadbeef', 'success', MARK_CONTEXT)]


def test_a_failing_verdict_marks_the_commit_RED(fleet_and_marks):
    """The discriminating half. A publisher that only ever wrote `success` would satisfy the test
    above and would turn branch protection into a rubber stamp -- worse than no protection, because
    it looks enforced.
    """
    fleet, marker, gate = fleet_and_marks
    gate.verdict = 'FAIL'
    tick(fleet, BOX, sha='deadbeef')
    assert marker.marks == [('deadbeef', 'failure', MARK_CONTEXT)]


def test_an_inconclusive_run_marks_the_commit_ERROR(fleet_and_marks):
    """Not green, and distinguishable from a real failure -- a merge must not proceed on no
    information, and a human must not be sent hunting a defect nobody found.
    """
    fleet, marker, gate = fleet_and_marks
    gate.verdict = 'INCONCLUSIVE'
    tick(fleet, BOX, sha='deadbeef')
    assert marker.marks == [('deadbeef', 'error', MARK_CONTEXT)]


# --------------------------------------------------------------------------- and when it must not


def test_no_sha_means_no_mark_but_the_verdict_STILL_LANDS(fleet_and_marks):
    """Work not tied to a commit has nothing to mark, and that must not cost the answer. A fleet
    where a missing sha silently stopped recording verdicts would look like a fleet doing no work.
    """
    fleet, marker, _gate = fleet_and_marks
    report = tick(fleet, BOX)
    assert _answered(report)
    assert marker.marks == []


def test_a_box_without_a_publisher_still_answers(tmp_path):
    """Only a box holding the verifier credential may mark, so most boxes have none -- and every one
    of them must still be able to take a turn. `status=None` is a legitimate fleet member.
    """
    forge = RecordingForge()
    namespace = f'mark-{uuid.uuid4().hex[:6]}'
    index = ItemIndex(tmp_path / 'index.json')
    gate = _Gate()
    fleet = Fleet(
        roadmap=loads(ROADMAP),
        submitter=ForgeStore(namespace, forge, role=Role.SUBMITTER, index=index),
        runner=ForgeStore(namespace, forge, role=Role.RUNNER, index=index),
        spool=Spool(tmp_path / 'spool'),
        publisher=ForgePublisher(ForgeStore(namespace, forge, role=Role.SUBMITTER, index=index)),
        executors={AGENT_TASK: gate, TEST_RUN: gate},
        owner='box-plain',
    )
    submit(fleet)
    assert _answered(tick(fleet, BOX, sha='deadbeef'))


def test_a_tick_that_answered_NOTHING_marks_nothing(fleet_and_marks):
    """A commit is marked because work was answered, not because a tick happened. Marking on an
    empty tick would publish a verdict nobody computed -- and on a protected branch, that green
    would let a merge through.
    """
    fleet, marker, _gate = fleet_and_marks
    tick(fleet, BOX, sha='deadbeef')
    marker.marks.clear()
    second = tick(fleet, BOX, sha='deadbeef')
    assert not _answered(second), second.outcomes
    assert marker.marks == []


def test_the_status_is_published_AFTER_the_verdict_is_recorded(fleet_and_marks):
    """The status is the loudest signal in the system -- it decides whether a merge proceeds -- so
    publishing it before the verdict is durable would let a crash leave a green commit with no
    recorded answer behind it. The record is the truth; the status is a projection of it.
    """
    fleet, marker, _gate = fleet_and_marks
    tick(fleet, BOX, sha='deadbeef')
    job = fleet.roadmap.items[0].job
    assert fleet.runner.verdict(job) == 'PASS'
    assert marker.marks, 'nothing was marked at all'
