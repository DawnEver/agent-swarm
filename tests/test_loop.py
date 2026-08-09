"""ONE loop, two executors. This file is what stops the design's founding claim from being prose.

    work item -> atomic claim -> execute on a box with capacity -> verdict -> record to store

`test_job.py` asserted the two kinds share a SHAPE. That is necessary and not sufficient: two
identically-shaped jobs can still be driven by two hand-written loops that drift. So the assertions
here are about the loop's BEHAVIOUR being independent of the kind -- the same `run_one` drives an
LLM worker and a deterministic runner, and the test proves it by handing it two executors that
differ in nothing but what they do.

WHY AN `Executor` PROTOCOL AND NOT A `kind` BRANCH. A branch inside the loop is the second
scheduler wearing a disguise: it starts as `if kind is AGENT_TASK` and accretes, and by the time it
is two systems no commit ever said so. A protocol makes the divergence a TYPE ERROR instead.

THE ORDER OF OPERATIONS IS THE CONTRACT, and every assertion below is really about ordering:

* admission is checked BEFORE the claim -- claiming then discovering there is no RAM leaves the job
  locked by a runner that is not running it, which is indistinguishable from a hung run;
* the claim is taken BEFORE execution -- the reverse is the duplicate-execution bug this whole
  layer was extracted to remove;
* the claim is released AFTER the verdict is recorded -- release-then-record leaves a window where
  the job is unclaimed and unanswered, so a second runner takes work that is already done;
* and the release happens even when the executor RAISES, because a crashed executor that keeps its
  claim is a job nothing will ever retry.
"""

from __future__ import annotations

import threading

import pytest

from agent_swarm.job import AGENT_TASK, TEST_RUN, Job
from agent_swarm.loop import Box, Outcome, run_one
from agent_swarm.store import InMemoryStore


class RecordingExecutor:
    """A test double that answers, and remembers that it was asked."""

    def __init__(self, verdict: str = 'PASS', detail: str = '') -> None:
        self.verdict = verdict
        self.detail = detail
        self.calls: list[Job] = []

    def execute(self, job: Job) -> tuple[str, str]:
        self.calls.append(job)
        return self.verdict, self.detail


class ExplodingExecutor:
    def __init__(self) -> None:
        self.calls: list[Job] = []

    def execute(self, job: Job) -> tuple[str, str]:
        self.calls.append(job)
        msg = 'the box fell over'
        raise RuntimeError(msg)


@pytest.fixture
def store() -> InMemoryStore:
    return InMemoryStore()


CHEAP_JOB = Job(id='j1', kind=TEST_RUN, exclusivity='cheap', ram_gib=0.1)

#: An idle box with room. PASSED EXPLICITLY at every call site, and there is no default: a `Box`
#: the caller forgot to measure would mean "unlimited", which is the safe-looking answer that is
#: exactly wrong. `capacity_blocker` refuses on unknown memory for the same reason.
IDLE = Box(available_gib=32.0)


class TestTheLoopIsTheSameForBothKinds:
    @pytest.mark.parametrize('kind', [AGENT_TASK, TEST_RUN])
    def test_a_job_of_either_kind_runs_through_the_same_call(self, store, kind):
        """THE FOUNDING CLAIM, as a test. Not "both kinds exist" -- both kinds are DRIVEN by one
        function, with no argument naming the kind.
        """
        job = Job(id='j', kind=kind, exclusivity='cheap', ram_gib=0.1)
        ex = RecordingExecutor('PASS', 'done')
        outcome = run_one(job, executor=ex, store=store, owner='box-1', box=IDLE)
        assert outcome is Outcome.ANSWERED
        assert store.verdict(job) == 'PASS'
        assert ex.calls == [job]

    def test_the_loop_never_INSPECTS_the_kind(self):
        """DISCRIMINATING, and the reason the parametrized test above is not enough: a loop can
        branch on the kind and still pass both legs today. The branch is the second scheduler in
        its first commit -- cheap to refuse now, invisible once it has accreted.
        """
        import ast
        import inspect

        from agent_swarm import loop

        # AN AST WALK, NOT A SUBSTRING SCAN. Grepping the source also reads the DOCSTRINGS, and
        # the prose there says "nothing here reads `job.kind`" -- so a plain scan fails on the
        # very sentence promising the property, and would be "fixed" by deleting the explanation.
        # The parse looks at CODE, which is what the claim is actually about.
        tree = ast.parse(inspect.getsource(loop))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                assert node.attr != 'kind', 'the loop reads a kind it must not care about'
            if isinstance(node, ast.Name):
                assert node.id not in {'AGENT_TASK', 'TEST_RUN', 'JobKind'}, (
                    f'the loop names {node.id} -- it is deciding by kind'
                )


class TestOrdering:
    def test_the_claim_is_taken_BEFORE_the_executor_runs(self, store):
        """The reverse ordering is the duplicate-execution bug this layer exists to remove."""
        seen: list[str | None] = []

        class Observer(RecordingExecutor):
            def execute(self, job):
                seen.append(store.claim_owner(job))
                return super().execute(job)

        run_one(CHEAP_JOB, executor=Observer(), store=store, owner='box-1', box=IDLE)
        assert seen == ['box-1']

    def test_a_job_ALREADY_claimed_is_not_executed(self, store):
        store.try_claim(CHEAP_JOB, owner='box-other')
        ex = RecordingExecutor()
        outcome = run_one(CHEAP_JOB, executor=ex, store=store, owner='box-1', box=IDLE)
        assert outcome is Outcome.CLAIMED_BY_ANOTHER
        assert ex.calls == []

    def test_a_refused_job_is_not_CLAIMED_either(self, store):
        """Claim-then-discover-no-capacity leaves the job locked by a runner that is not running
        it -- indistinguishable from a hung run, and it only clears when the lease expires.
        """
        huge = Job(id='j', kind=TEST_RUN, exclusivity='cheap', ram_gib=10_000.0)
        ex = RecordingExecutor()
        outcome = run_one(huge, executor=ex, store=store, owner='box-1', box=IDLE)
        assert outcome is Outcome.REFUSED
        assert ex.calls == []
        assert store.claim_owner(huge) is None

    def test_the_verdict_is_recorded_BEFORE_the_claim_is_released(self, store):
        """Release-then-record opens a window in which the job is unclaimed AND unanswered, so a
        second runner picks up work that is already finished. Nothing errors; it just costs a run.
        """
        order: list[str] = []
        real_record = store.record_verdict
        real_release = store.release

        def record(job, **kw):
            order.append('record')
            real_record(job, **kw)

        def release(job, **kw):
            order.append('release')
            real_release(job, **kw)

        store.record_verdict = record  # type: ignore[method-assign]
        store.release = release  # type: ignore[method-assign]
        run_one(CHEAP_JOB, executor=RecordingExecutor(), store=store, owner='box-1', box=IDLE)
        assert order == ['record', 'release']

    def test_the_claim_is_released_when_the_job_is_done(self, store):
        run_one(CHEAP_JOB, executor=RecordingExecutor(), store=store, owner='box-1', box=IDLE)
        assert store.claim_owner(CHEAP_JOB) is None


class TestAnExecutorThatFails:
    def test_a_RAISING_executor_still_releases_the_claim(self, store):
        """A crashed executor holding its claim is a job nothing will ever retry -- the failure
        mode that looks like a slow run forever.
        """
        run_one(CHEAP_JOB, executor=ExplodingExecutor(), store=store, owner='box-1', box=IDLE)
        assert store.claim_owner(CHEAP_JOB) is None

    def test_a_RAISING_executor_records_INCONCLUSIVE_not_FAIL(self, store):
        """The crash says nothing about the code under test. Recording FAIL would convert a fallen-
        over box into evidence against a diff, which is the unearned RED that mirrors the unearned
        green.
        """
        run_one(CHEAP_JOB, executor=ExplodingExecutor(), store=store, owner='box-1', box=IDLE)
        assert store.verdict(CHEAP_JOB) == 'INCONCLUSIVE'

    def test_the_crash_DETAIL_survives_into_the_record(self, store):
        """An INCONCLUSIVE with no reason is indistinguishable from a runner that never started."""
        run_one(CHEAP_JOB, executor=ExplodingExecutor(), store=store, owner='box-1', box=IDLE)
        assert 'the box fell over' in store._verdicts[CHEAP_JOB.claim_key()][1]

    def test_the_outcome_reports_that_it_CRASHED(self, store):
        """The caller must be able to tell "ran and was inconclusive" from "the executor died" --
        a log here would be the forbidden shape: a warning on an unchanged success return.
        """
        outcome = run_one(CHEAP_JOB, executor=ExplodingExecutor(), store=store, owner='box-1', box=IDLE)
        assert outcome is Outcome.CRASHED

    def test_an_executor_returning_a_BAD_verdict_is_refused_loudly(self, store):
        """A fourth verdict word is a state nothing knows how to act on. It must not reach the
        store, and it must not be silently coerced into one of the three either.
        """
        with pytest.raises(ValueError, match='verdict'):
            run_one(CHEAP_JOB, executor=RecordingExecutor('GREEN'), store=store, owner='box-1', box=IDLE)
        assert store.claim_owner(CHEAP_JOB) is None, 'a bad verdict must not strand the claim'


class TestAnAnsweredJobIsDone:
    """THE CLAIM IS A LEASE, NOT A DONE-MARKER, and conflating the two costs whole runs silently.

    A claim is released the instant the work finishes. So sixteen boxes racing for one job do not
    collide -- they QUEUE, and each in turn claims it legitimately and runs it again. Every claim
    is correct, the total is fifteen wasted runs, and nothing errors anywhere. Only the recorded
    verdict can say the job is finished.
    """

    def test_a_job_with_a_verdict_is_NOT_re_executed(self, store):
        store.record_verdict(CHEAP_JOB, verdict='PASS', detail='')
        ex = RecordingExecutor()
        outcome = run_one(CHEAP_JOB, executor=ex, store=store, owner='box-1', box=IDLE)
        assert outcome is Outcome.ALREADY_ANSWERED
        assert ex.calls == []

    def test_an_answered_job_is_not_CLAIMED_either(self, store):
        """Claiming it to discover it is done holds a lease over work nobody is doing."""
        store.record_verdict(CHEAP_JOB, verdict='PASS', detail='')
        run_one(CHEAP_JOB, executor=RecordingExecutor(), store=store, owner='box-1', box=IDLE)
        assert store.claim_owner(CHEAP_JOB) is None

    def test_an_explicit_RETRY_overrides_it(self, store):
        """INCONCLUSIVE is the verdict worth re-running. The policy for WHEN is the caller's --
        `admission.should_retry` already owns it, and a second copy here is how the two disagree.
        """
        store.record_verdict(CHEAP_JOB, verdict='INCONCLUSIVE', detail='node down')
        ex = RecordingExecutor()
        outcome = run_one(CHEAP_JOB, executor=ex, store=store, owner='box-1', box=IDLE, retry=True)
        assert outcome is Outcome.ANSWERED
        assert ex.calls == [CHEAP_JOB]


class TestUnderRealConcurrency:
    def test_exactly_one_of_many_boxes_executes_a_job(self, store):
        """The end-to-end statement of the property `test_store.py` proves for the claim alone:
        with the loop, admission and the store composed, one job runs ONCE. Sequential tests pass
        for a broken design here too, so the threads are the test.
        """
        ex = RecordingExecutor()
        ex_lock = threading.Lock()
        barrier = threading.Barrier(16)

        class Serialised:
            def execute(self, job):
                with ex_lock:
                    return ex.execute(job)

        def attempt(n: int) -> None:
            barrier.wait()
            run_one(CHEAP_JOB, executor=Serialised(), store=store, owner=f'box-{n}', box=IDLE)

        threads = [threading.Thread(target=attempt, args=(i,)) for i in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(ex.calls) == 1, f'the job ran {len(ex.calls)} times'
