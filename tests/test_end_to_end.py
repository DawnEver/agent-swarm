"""The rehearsal: the WHOLE loop, against the real Gitea, for both kinds of job.

WHY THIS FILE IS WORTH MORE THAN THE 344 TESTS BESIDE IT. Every component here was proven in
isolation and several were proven WRONG in integration on the same day: the claim protocol passed
four barrier rounds and still had two holes that appeared only when something was built around it;
`_item_number` was correct and unsafe; the offline suite certified a live duplicate-execution bug as
fixed. **Integration is where this system's defects live**, and a green unit suite is not evidence
that the loop closes.

WHAT IS REAL HERE: the forge (real Gitea, `probe-e2e-` prefixed and purged), the claim protocol, the
spool on real disk, the item index, the allocator, the roadmap, and -- for the agent kind -- a real
fabric session on a real node.

WHAT IS STUBBED, AND WHY THAT IS HONEST: the gate. `GateExecutor` lives in motronics and running a
25-minute gate inside a rehearsal would make this untestable rather than more true; what this file
must prove is that a verdict TRAVELS, not that a gate computes one. The stub is a `Verifier`, which
is the same seam the real gate plugs into, so the wiring under test is the shipping wiring.

THE SESSION WORKS IN A TEMP DIRECTORY. Pointing a live LLM at a working checkout is a genuine
hazard; another lane is in that tree.
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path

import pytest

from agent_swarm.agent_executor import AgentTaskExecutor, StaticBrief
from agent_swarm.fabric import FabricSessionRunner, SessionTransportUnavailable
from agent_swarm.forge import default_forge
from agent_swarm.forge_store import ForgeStore, Role
from agent_swarm.item_index import ItemIndex
from agent_swarm.job import AGENT_TASK, TEST_RUN, Job
from agent_swarm.loop import Box, Outcome, run_one
from agent_swarm.provenance import running_provenance
from agent_swarm.roadmap import loads
from agent_swarm.spool import ForgePublisher, Spool
from agent_swarm.tick import Fleet, submit, tick
from conftest import LIVE_REPO

ROADMAP = """
version = 1

[[item]]
key = "probe-e2e-first"
title = "The first rehearsal item"
acceptance = "the verdict travels"
rem = "human"
priority = 3

[[item]]
key = "probe-e2e-second"
title = "A dependent item"
acceptance = "it does not start before the first"
rem = "human"
priority = 3
needs = ["probe-e2e-first"]
"""

# NOTE FOR THE READER: every roadmap item is an AGENT_TASK -- `roadmap.py` hard-codes the kind, and
# a job's id is a CONTENT HASH of key/title/acceptance rather than the key. Both are deliberate
# there, and both are things this rehearsal discovered by running rather than by reading: a test
# written against the key would have looked right and matched nothing.


class StubGate:
    """Stands in for `gate.py`. A `Verifier`, which is the seam the real gate plugs into.

    NOT A STUB OF THE THING UNDER TEST. What this file must show is that a verdict travels from an
    executor to a board; whether a gate can compute one is `motronics`' question and is answered by
    its own suite. Running a 25-minute gate here would not make the rehearsal truer, it would make
    it unrunnable -- and an unrunnable rehearsal is the one that never catches anything.
    """

    def __init__(self, verdict: str = 'PASS', detail: str = '10646 passed') -> None:
        self.verdict = verdict
        self.detail = detail
        self.asked: list[str] = []

    def verify(self, job: Job) -> tuple[str, str]:
        self.asked.append(job.claim_key())
        return self.verdict, self.detail

    # A deterministic runner is also an Executor in its own right.
    def execute(self, job: Job) -> tuple[str, str]:
        return self.verify(job)


class CrashingExecutor:
    def execute(self, job: Job) -> tuple[str, str]:
        msg = 'the runner died mid-gate'
        raise RuntimeError(msg)


class TempWorkspace:
    """Fingerprints a directory tree, so "did the session change anything" is answerable."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def fingerprint(self) -> str:
        return repr(sorted((p.name, p.stat().st_size) for p in self.root.rglob('*') if p.is_file()))


@pytest.fixture
def rehearsal(tmp_path):
    """A whole fleet on a namespace nobody else owns, purged afterwards."""
    namespace = f'probe-e2e-{uuid.uuid4().hex[:8]}'
    index = ItemIndex(tmp_path / 'index.json')
    fleet = Fleet(
        roadmap=loads(ROADMAP),
        submitter=ForgeStore(namespace, default_forge(repo=LIVE_REPO), role=Role.SUBMITTER, index=index),
        runner=ForgeStore(namespace, default_forge(repo=LIVE_REPO), role=Role.RUNNER, index=index),
        spool=Spool(tmp_path / 'spool'),
        publisher=ForgePublisher(
            ForgeStore(namespace, default_forge(repo=LIVE_REPO), role=Role.SUBMITTER, index=index)
        ),
        # The roadmap only ever yields AGENT_TASK; TEST_RUN is configured too because a fleet
        # that could not run both kinds would not be testing the design's founding claim.
        executors={AGENT_TASK: StubGate(), TEST_RUN: StubGate()},
        owner='box-rehearsal',
    )
    try:
        yield fleet
    finally:
        fleet.submitter.purge_namespace()


def _fleet_on(namespace: str, tmp_path, *, index_name: str) -> Fleet:
    """A second fleet on an EXISTING namespace, with a cold in-process cache and a warm index.

    The distinction this exists to preserve: an in-process dict and an on-disk index are two caches
    in front of one another, and timing the outer one tells you nothing about the inner one.
    """
    index = ItemIndex(tmp_path / index_name)
    return Fleet(
        roadmap=loads(ROADMAP),
        submitter=ForgeStore(namespace, default_forge(repo=LIVE_REPO), role=Role.SUBMITTER, index=index),
        runner=ForgeStore(namespace, default_forge(repo=LIVE_REPO), role=Role.RUNNER, index=index),
        spool=Spool(tmp_path / 'spool2'),
        publisher=ForgePublisher(
            ForgeStore(namespace, default_forge(repo=LIVE_REPO), role=Role.SUBMITTER, index=index)
        ),
        executors={AGENT_TASK: StubGate(), TEST_RUN: StubGate()},
        owner='box-rehearsal',
    )


BOX = Box(available_gib=64.0)


@pytest.mark.live_forge
class TestTheLoopCloses:
    def test_one_full_cycle_and_the_verdict_comes_BACK_OUT(self, rehearsal):
        """THE REHEARSAL. Roadmap to board, and the assertion is on the way OUT, not on the way in.

        Reading back through `verdict()` is the path a human or a board column uses, so a system
        that recorded correctly and published somewhere unreadable would fail here -- which is
        exactly the class of defect a component suite cannot see.
        """
        created = submit(rehearsal)
        assert created == ['probe-e2e-first', 'probe-e2e-second']

        report = tick(rehearsal, BOX)
        first = rehearsal.roadmap.by_key['probe-e2e-first'].job

        assert report.outcomes[first.claim_key()] is Outcome.ANSWERED
        assert report.published, 'the spool did not publish'
        assert rehearsal.runner.verdict(first) == 'PASS'
        assert '10646 passed' in rehearsal.runner.verdict_detail(first)

    def test_a_DEPENDENT_item_does_not_start_before_its_dependency(self, rehearsal):
        """The `needs` graph is enforced rather than described. Before the first item passes, the
        second must not even be considered -- not merely ranked lower.
        """
        submit(rehearsal)
        report = tick(rehearsal, BOX)
        second = rehearsal.roadmap.by_key['probe-e2e-second'].job
        assert second.claim_key() not in report.considered

        tick(rehearsal, BOX)  # now the first is done, the second unblocks
        assert rehearsal.runner.verdict(rehearsal.roadmap.by_key['probe-e2e-second'].job) == 'PASS'

    def test_an_ANSWERED_item_is_not_re_run(self, rehearsal):
        """The claim alone does not give this: a claim is released the moment work finishes, so
        sixteen boxes would serialise into sixteen correct, duplicated runs. The recorded VERDICT is
        what makes a job done.

        FOUND BY RUNNING IT: the guard fires at TWO layers, and via the roadmap it is the OUTER one.
        An item with a PASS drops out of `Roadmap.candidates` through `done`, so the loop never sees
        it and `run_one`'s own `ALREADY_ANSWERED` never fires -- I had asserted the inner one and it
        was simply not reached. Both are checked below, because the outer guard only covers jobs
        that came from a roadmap and CI candidates do not.
        """
        submit(rehearsal)
        tick(rehearsal, BOX)
        first = rehearsal.roadmap.by_key['probe-e2e-first'].job
        gate = rehearsal.executors[AGENT_TASK]

        before = list(gate.asked)
        again = tick(rehearsal, BOX)
        assert first.claim_key() not in again.considered, 'an answered item was still offered'
        assert first.claim_key() not in gate.asked[len(before) :], 'an answered job was executed again'

        # The inner guard, reached directly -- the path a CI candidate takes, where nothing upstream
        # filters on `done`.
        assert run_one(first, executor=gate, store=rehearsal.runner, owner='box-b', box=BOX) is Outcome.ALREADY_ANSWERED

    def test_a_SECOND_runner_racing_the_same_item_is_refused(self, rehearsal):
        """Not a second execution. The claim is held for the duration of the work, so a competing
        box must be told CLAIMED_BY_ANOTHER and move down its ranked list.
        """
        submit(rehearsal)
        first = rehearsal.roadmap.by_key['probe-e2e-first'].job
        assert rehearsal.runner.try_claim(first, owner='box-a') is True

        report = tick(rehearsal, BOX)
        assert report.outcomes[first.claim_key()] is Outcome.CLAIMED_BY_ANOTHER
        rehearsal.runner.release(first, owner='box-a')

    def test_a_CRASHED_executor_releases_the_claim_and_records_INCONCLUSIVE(self, rehearsal):
        """The failure that must not leave a job locked. INCONCLUSIVE is also the word that means
        re-runnable, so the next tick can retry it -- asserted here rather than assumed.
        """
        submit(rehearsal)
        first = rehearsal.roadmap.by_key['probe-e2e-first'].job
        rehearsal.executors[AGENT_TASK] = CrashingExecutor()

        report = tick(rehearsal, BOX)
        assert report.outcomes[first.claim_key()] is Outcome.CRASHED
        assert rehearsal.runner.claim_owner(first) is None, 'the claim was left held'
        assert rehearsal.runner.verdict(first) == 'INCONCLUSIVE'

        rehearsal.executors[AGENT_TASK] = StubGate()
        retried = tick(rehearsal, BOX, retry=True)
        assert retried.outcomes[first.claim_key()] is Outcome.ANSWERED
        assert rehearsal.runner.verdict(first) == 'PASS'

    def test_the_verdict_survives_the_SPOOL_being_drained_later(self, rehearsal, tmp_path):
        """The durability path, integrated. The verdict is on disk before it is anywhere else, so a
        forge that was unreachable at the moment of the answer costs a republish, not the answer.
        """
        submit(rehearsal)
        first = rehearsal.roadmap.by_key['probe-e2e-first'].job
        rehearsal.spool.record(first, verdict='FAIL', detail='3 failed, 10643 passed')
        assert rehearsal.spool.pending(), 'precondition: it is on disk and not yet published'

        rehearsal.spool.drain(rehearsal.publisher)
        assert rehearsal.runner.verdict(first) == 'FAIL'

    def test_the_INDEX_resolves_the_item_by_number_afterwards(self, rehearsal, tmp_path):
        """The read a board makes on the next tick, through a cold-ish path: a fresh store that
        never saw the creation still finds the verdict, because the index remembered the number and
        `GET /issues/{number}` is the read that is fresh on both forges.
        """
        submit(rehearsal)
        first = rehearsal.roadmap.by_key['probe-e2e-first'].job
        tick(rehearsal, BOX)

        fresh = ForgeStore(
            rehearsal.runner.namespace,
            default_forge(repo=LIVE_REPO),
            role=Role.RUNNER,
            index=ItemIndex(tmp_path / 'index.json'),
        )
        assert fresh.verdict(first) == 'PASS'


@pytest.mark.live_forge
class TestTheCostOfOneCycle:
    def test_one_cycle_is_timed_and_reported(self, rehearsal, tmp_path, capsys):
        """A WALL-CLOCK NUMBER, because at 7x24 the per-job overhead decides how many jobs a box can
        turn -- and nothing else in this suite measures the LOOP rather than a call.
        """
        submit_start = time.perf_counter()
        submit(rehearsal)
        submit_ms = (time.perf_counter() - submit_start) * 1000

        tick_start = time.perf_counter()
        tick(rehearsal, BOX)
        tick_ms = (time.perf_counter() - tick_start) * 1000

        # A FRESH FLEET, because the label has to be true. Calling `submit` again on the SAME
        # fleet measures the in-process dict, not the index -- it reported 0 ms and I nearly quoted
        # it as the index figure. That is the same defect as an untested cache wearing a different
        # hat: a NAME asserting a property the measurement does not have, and the working cache
        # alibiing the one under test. Exactly the mechanism that hid the index bug, in the file
        # that reports the index's cost.
        cold_fleet = _fleet_on(rehearsal.runner.namespace, tmp_path, index_name='index.json')
        warm_start = time.perf_counter()
        submit(cold_fleet)
        warm_ms = (time.perf_counter() - warm_start) * 1000

        with capsys.disabled():
            # THE PROVENANCE PRINTS WITH THE NUMBERS, ALWAYS. A wall-clock figure measured against a
            # working tree, quoted beside an interpreter pinned commits behind it, LOOKS reproducible
            # and is not -- the same defect as an install landing mid-gate, aimed at a number rather
            # than a verdict. The rule that follows is "always quote the two together", and a rule
            # someone has to remember is one they will eventually not.
            print()
            print(f'  {running_provenance()}')
            print(f'  submit, cold (creates, 2 items):   {submit_ms:.0f} ms')
            print(f'  submit, warm INDEX (fresh store):  {warm_ms:.0f} ms')
            print(f'  one full tick:                {tick_ms:.0f} ms')
        assert tick_ms > 0


@pytest.mark.live_forge
@pytest.mark.live_fabric
class TestTheAGENTKindEndToEnd:
    """THE LEG THAT CANNOT BE REHEARSED HONESTLY YET, and the two reasons why.

    Both were found by running it, and neither is visible from any component's tests because each
    component is correct on its own.

    **1. `project` is a NAMED ALIAS, not a path.** `node/spawn` answers
    `unknown project alias "<tmpdir>" ... Available: motronics-studio`. So a session can only run in
    a pre-registered project, and the only one registered is the working checkout another lane is
    in. There is no scratch-project provisioning anywhere in the system; that is missing
    infrastructure, not a test problem, and pointing the rehearsal at `motronics-studio` to make it
    pass would be exactly the hazard I was told to avoid.

    **2. The session runs on a REMOTE node; the workspace fingerprint is LOCAL.** This is the
    deeper one. `AgentTaskExecutor` refuses to call a session successful unless the workspace
    changed -- and `Workspace.fingerprint()` reads a local directory while `FabricSessionRunner`
    spawns on whichever node fabric picked. Run without a project alias, a write-enabled session
    reported creating a file that does not exist anywhere on this box. The guard therefore compares
    a tree the session never touched: it can only ever answer "changed nothing", so an agent task
    can never reach PASS through this path.

    THE SECOND ONE IS ARCHITECTURAL. The no-change guard is the thing standing between a fluent
    "done!" and an unearned PASS, and it is measuring the wrong filesystem. Options are a
    node-side fingerprint returned by the transport, or running the session on the same box that
    judges it -- both are design decisions above this lane.

    What the tests below DO establish: the transport works end to end from a library process, and
    the executor's refusal is correct against a REAL session rather than a fake one.
    """

    def test_a_real_session_runs_end_to_end_through_the_executor(self, tmp_path):
        """The transport half, proven with the real thing: spawn, turn, close, and a verdict comes
        back through `AgentTaskExecutor` rather than from the session's own words.
        """
        workspace = tmp_path / 'scratch'
        workspace.mkdir()
        gate = StubGate(verdict='PASS', detail='the stub gate approved the change')
        executor = AgentTaskExecutor(
            session=FabricSessionRunner(provider='codex', write=False),
            verifier=gate,
            workspace=None,
            brief=StaticBrief(template='Reply with the single word ACK. Task {key}.'),
        )
        verdict, detail = executor.execute(Job(id='probe-e2e-agent', kind=AGENT_TASK))

        assert verdict == 'INCONCLUSIVE', f'expected the no-evidence refusal, got {verdict}: {detail[:300]}'
        assert 'absence of evidence' in detail
        assert gate.asked == [], 'the gate was consulted about a tree the session never touched'

    def test_a_scratch_project_alias_does_not_EXIST_which_is_the_blocker(self, tmp_path):
        """Named rather than worked around. A temp directory is not a project as far as fabric is
        concerned, and until scratch projects can be provisioned an agent task has nowhere safe to
        run -- the only registered alias is a checkout another lane is working in.
        """
        runner = FabricSessionRunner(provider='codex', write=True, project=str(tmp_path))
        with pytest.raises(SessionTransportUnavailable, match='unknown project alias'):
            runner.run('touch a file', job=Job(id='probe-e2e-agent', kind=AGENT_TASK))

    def test_a_write_without_a_project_is_REFUSED_before_anything_is_written(self):
        """The guard that came out of this rehearsal. A write-enabled session with no alias wrote a
        file to an undefined directory on an undefined node -- one stray file is a curiosity, and at
        a hundred agents it is an unbounded set of writes nobody can enumerate or delete."""
        with pytest.raises(ValueError, match='undefined directory'):
            FabricSessionRunner(provider='codex', write=True)
