"""The second executor, and therefore the first real test of "one loop, two kinds".

A protocol with one implementation is a description of that implementation. Until this file existed
the claim that conversational-versus-deterministic is an attribute of the EXECUTOR was unfalsified
rather than verified -- so the assertions that matter most here are the ones about what did NOT have
to change: no new `Job` field, no branch in `run_one`, no second vocabulary.

THE FAILURE MODE A DETERMINISTIC RUNNER DOES NOT HAVE is a session that produces something
plausible and wrong. A gate runner cannot lie about its exit code; a language model can write a
fluent, confident, entirely incorrect completion report in the same register as a correct one. Most
of this file is about refusing to believe it.
"""

from __future__ import annotations

import io
import tokenize
from pathlib import Path

import pytest

from agent_swarm import agent_executor as agent_executor_module
from agent_swarm.admission import WHOLE_BOX
from agent_swarm.agent_executor import (
    AgentTaskExecutor,
    Brief,
    SessionOutcome,
    SessionRunner,
    StaticBrief,
    Verifier,
    Workspace,
)
from agent_swarm.job import AGENT_TASK, TEST_RUN, Job
from agent_swarm.loop import Box, Executor, Outcome, run_one
from agent_swarm.store import VERDICTS, InMemoryStore

TASK = Job(id='42', kind=AGENT_TASK, ram_gib=0.2, exclusivity=WHOLE_BOX, solo_seconds=60.0)


class FakeSession:
    """A session transport under the test's control. It is NOT a stand-in for the executor.

    The code under test is the executor's judgement about what a session produced; a real LLM would
    make that judgement untestable, since the interesting cases -- confidently wrong, silently
    inert, killed mid-edit -- cannot be summoned on demand from a real model.
    """

    def __init__(
        self,
        *,
        completed: bool = True,
        self_report: str = 'done!',
        writes: str | None = None,
        raises: Exception | None = None,
    ) -> None:
        self.completed = completed
        self.self_report = self_report
        self.writes = writes
        self.raises = raises
        self.briefs: list[str] = []
        self.workspace: FakeWorkspace | None = None

    def run(self, brief: str, *, job: Job) -> SessionOutcome:
        self.briefs.append(brief)
        if self.raises:
            raise self.raises
        if self.writes is not None and self.workspace is not None:
            self.workspace.state = self.writes
        return SessionOutcome(completed=self.completed, transcript='...', self_report=self.self_report)


class FakeWorkspace:
    def __init__(self, state: str = 'before') -> None:
        self.state = state

    def fingerprint(self) -> str:
        return self.state


class FakeVerifier:
    """Stands in for gate.py. Records that it was asked, because "was the gate even run" is itself
    an assertion this file needs to make."""

    def __init__(self, verdict: str = 'PASS', detail: str = '10646 passed') -> None:
        self.verdict = verdict
        self.detail = detail
        self.asked: list[str] = []

    def verify(self, job: Job) -> tuple[str, str]:
        self.asked.append(job.claim_key())
        return self.verdict, self.detail


def _executor(session: FakeSession, verifier: FakeVerifier, workspace: FakeWorkspace) -> AgentTaskExecutor:
    session.workspace = workspace
    return AgentTaskExecutor(session=session, verifier=verifier, workspace=workspace, brief=StaticBrief())


class TestTheVerdictIsEARNEDNotAsserted:
    def test_a_session_that_says_DONE_but_fails_the_gate_is_a_FAIL(self):
        """THE DISCRIMINATING TEST OF THIS WHOLE FILE. The model is fluent and wrong; the gate is
        neither. An executor that read `self_report` would return PASS here, and the transcript
        would read entirely convincingly in the incident review.
        """
        session = FakeSession(self_report='All tests pass. Task complete.', writes='after')
        verifier = FakeVerifier(verdict='FAIL', detail='3 failed, 10643 passed')
        verdict, detail = _executor(session, verifier, FakeWorkspace()).execute(TASK)
        assert verdict == 'FAIL'
        assert '3 failed' in detail

    def test_the_session_self_report_is_KEPT_but_labelled(self):
        """Discarding it would destroy the evidence a reviewer needs; presenting it unlabelled is
        how a confident summary comes to be read as a result.
        """
        session = FakeSession(self_report='I refactored the solver.', writes='after')
        _, detail = _executor(session, FakeVerifier(), FakeWorkspace()).execute(TASK)
        assert 'I refactored the solver.' in detail
        assert 'NOT the verdict' in detail

    def test_the_gate_evidence_comes_FIRST(self):
        """A reader scanning the top of a report must meet the measured result before the model's
        account of itself.
        """
        session = FakeSession(self_report='done!', writes='after')
        _, detail = _executor(session, FakeVerifier(detail='10646 passed'), FakeWorkspace()).execute(TASK)
        assert detail.index('10646 passed') < detail.index('the session said')

    def test_a_silent_session_that_passes_the_gate_is_still_a_PASS(self):
        """The verifier decides, so a model that says nothing at all is not penalised for it."""
        session = FakeSession(self_report='', writes='after')
        verdict, _ = _executor(session, FakeVerifier(verdict='PASS'), FakeWorkspace()).execute(TASK)
        assert verdict == 'PASS'

    @pytest.mark.parametrize('word', sorted(VERDICTS))
    def test_every_verifier_word_is_passed_through_unchanged(self, word):
        session = FakeSession(writes='after')
        verdict, _ = _executor(session, FakeVerifier(verdict=word), FakeWorkspace()).execute(TASK)
        assert verdict == word

    def test_a_FOURTH_word_from_the_verifier_RAISES(self):
        """Not coerced into INCONCLUSIVE. Laundering a broken verifier into a legal verdict is the
        invented state the vocabulary exists to refuse, and it would be invisible afterwards.
        """
        session = FakeSession(writes='after')
        with pytest.raises(ValueError, match='verifier'):
            _executor(session, FakeVerifier(verdict='ERROR'), FakeWorkspace()).execute(TASK)


class TestTheTwoRefusalsADeterministicRunnerDoesNotNeed:
    def test_a_session_that_changed_NOTHING_is_INCONCLUSIVE_not_PASS(self):
        """THE QUIET ONE. Run an agent that does nothing against an already-green tree and the gate
        says PASS -- truthfully, about a tree nobody touched. Attributing that to the task is how a
        backlog empties itself without any work being done.
        """
        session = FakeSession(self_report='Nothing to do, it already works.', writes=None)
        verifier = FakeVerifier(verdict='PASS')
        verdict, detail = _executor(session, verifier, FakeWorkspace()).execute(TASK)
        assert verdict == 'INCONCLUSIVE'
        assert 'changed nothing' in detail

    def test_and_the_gate_is_not_even_ASKED_in_that_case(self):
        """Because its answer would be about the wrong thing. Asking and then discarding would leave
        a PASS in the gate's own logs for a task nobody worked on.
        """
        session = FakeSession(writes=None)
        verifier = FakeVerifier(verdict='PASS')
        _executor(session, verifier, FakeWorkspace()).execute(TASK)
        assert verifier.asked == []

    def test_an_INCOMPLETE_session_is_INCONCLUSIVE_even_if_the_gate_is_green(self):
        """A crash or timeout leaves work half-applied, and a half-applied change that happens to
        pass is the most dangerous green available. INCONCLUSIVE is also the word that means
        re-runnable, which is what a killed session actually needs.
        """
        session = FakeSession(completed=False, writes='after')
        verifier = FakeVerifier(verdict='PASS')
        verdict, detail = _executor(session, verifier, FakeWorkspace()).execute(TASK)
        assert verdict == 'INCONCLUSIVE'
        assert 'did not complete' in detail
        assert verifier.asked == [], 'the gate was asked about a half-applied tree'

    def test_a_session_that_RAISES_is_INCONCLUSIVE_and_does_not_escape(self):
        """A dead provider is an outcome, not a crash of the loop. Letting it propagate would abort
        the tick and leave the claim held until its lease expired.
        """
        session = FakeSession(raises=TimeoutError('provider timed out'))
        verdict, detail = _executor(session, FakeVerifier(), FakeWorkspace()).execute(TASK)
        assert verdict == 'INCONCLUSIVE'
        assert 'TimeoutError' in detail

    def test_a_changed_tree_IS_verified(self):
        """The complement of the refusals: real work must actually reach the gate."""
        session = FakeSession(writes='after')
        verifier = FakeVerifier()
        _executor(session, verifier, FakeWorkspace()).execute(TASK)
        assert verifier.asked == [TASK.claim_key()]


class TestItIsTheSAMELoopNotASecondOne:
    """The founding claim, as assertions about what did NOT have to change."""

    def test_the_executor_protocol_is_satisfied(self):
        assert isinstance(_executor(FakeSession(), FakeVerifier(), FakeWorkspace()), Executor)

    def test_an_agent_task_needs_NO_new_job_field(self):
        """If it did, the single scheduler would already be two. `test_job.py` guards the model; this
        guards the claim that the second executor did not force it open.
        """
        assert Job(id='42', kind=AGENT_TASK).claim_key() == 'agent-task/42'

    def test_run_one_schedules_it_with_no_special_casing(self):
        """The same `run_one` that drives the gate runner: same claim, same lease, same store."""
        store = InMemoryStore()
        session = FakeSession(writes='after')
        executor = _executor(session, FakeVerifier(verdict='PASS'), FakeWorkspace())
        outcome = run_one(TASK, executor=executor, store=store, owner='box-1', box=Box(available_gib=64.0))
        assert outcome is Outcome.ANSWERED
        assert store.verdict(TASK) == 'PASS'

    def test_the_claim_is_RELEASED_after_an_agent_task(self):
        store = InMemoryStore()
        session = FakeSession(writes='after')
        executor = _executor(session, FakeVerifier(), FakeWorkspace())
        run_one(TASK, executor=executor, store=store, owner='box-1', box=Box(available_gib=64.0))
        assert store.claim_owner(TASK) is None

    def test_an_agent_task_and_a_test_run_of_ONE_id_do_not_contend(self):
        """The two kinds share an id space and are different work -- which is exactly why `kind` is
        in the claim key, and why one loop can carry both.
        """
        store = InMemoryStore()
        assert store.try_claim(Job(id='42', kind=AGENT_TASK), owner='a') is True
        assert store.try_claim(Job(id='42', kind=TEST_RUN), owner='b') is True

    def test_the_vocabulary_is_the_same_three_words(self):
        assert VERDICTS == {'PASS', 'FAIL', 'INCONCLUSIVE'}


class TestTheBriefReachesTheSession:
    def test_the_job_is_named_in_the_brief(self):
        session = FakeSession(writes='after')
        _executor(session, FakeVerifier(), FakeWorkspace()).execute(TASK)
        assert TASK.claim_key() in session.briefs[0]

    def test_the_default_brief_forbids_editing_the_TESTS(self):
        """It cannot enforce that -- see the module docstring on attacking the verifier -- but a
        brief that does not even ask has given the cheapest possible answer away for free.
        """
        assert 'not modify tests' in StaticBrief().for_job(TASK)

    def test_a_brief_is_a_seam_not_a_string(self):
        assert isinstance(StaticBrief(), Brief)


class TestTheSeamsAreSeams:
    def test_the_fakes_satisfy_the_protocols(self):
        assert isinstance(FakeSession(), SessionRunner)
        assert isinstance(FakeVerifier(), Verifier)
        assert isinstance(FakeWorkspace(), Workspace)

    def test_a_session_outcome_offers_NO_verdict_field(self):
        """Deliberate. A session reports on itself and cannot be the judge of itself, so the type
        gives an executor nothing it could mistake for an answer.
        """
        fields = SessionOutcome.__slots__
        assert not any('verdict' in name for name in fields)
        assert 'self_report' in fields


class TestTheSeamDoesNotLeakL0Vocabulary:
    """The layering rule, as a check. L1 says issue/branch/gate; L0 says session/turn/node/pid.

    The seam was written before the transport existed, so this guards the direction that actually
    goes wrong later: a `provider=` or a `node=` appearing here would mean fabric's vocabulary had
    climbed into the scheduler, and the next backend would need a branch rather than an adapter.
    """

    def test_no_vendor_or_transport_name_appears_in_the_executor(self):
        source = Path(agent_executor_module.__file__).read_text(encoding='utf-8')
        code = [
            token.string
            for token in tokenize.generate_tokens(io.StringIO(source).readline)
            if token.type not in (tokenize.STRING, tokenize.COMMENT)
        ]
        banned = {'fabric', 'mcp', 'node', 'provider', 'pid', 'subprocess', 'socket', 'claude', 'codex'}
        offenders = sorted({t for t in code if t.lower() in banned})
        assert not offenders, f'L0 vocabulary leaked into the executor: {offenders}'

    def test_this_package_did_not_grow_a_session_layer(self):
        """Fabric already spawns and drives sessions. A second one here would be the duplicated
        scheme this project names first -- and the copy would be the one nobody maintains.
        """
        assert not hasattr(agent_executor_module, 'subprocess')
        assert not hasattr(agent_executor_module, 'socket')
