"""The store contract, and the one property that makes it worth replacing what came before.

WHY THIS INTERFACE EXISTS. motronics' `ci_tick.claim()` is **push-then-arbitrate**, not
compare-and-swap: every runner pushes its own ref, then all of them re-read the full claim set and
apply a deterministic winner rule. Its own docstring is candid -- "pushes succeed and the push no
longer decides anything" -- and the correctness argument holds only if every runner observes the
FULL set at resolve time. Runner A can push, read `{A}` before B's push is visible, and declare
itself the winner while B reads `{A, B}` and computes a different one. Analysed and left
unverified in `open-the-claim-resolve-window-deferred-until-a-real-trace.md`; the cost is duplicate
execution, not a wrong verdict, which is why it was deferred rather than chased.

THE DESIGN'S ANSWER IS NOT A BETTER ARBITRATION, IT IS TO STOP ARBITRATING: a store that offers an
atomic compare-and-swap (an Issue assignee set only if unset; a ref created only if absent) removes
the window structurally. So `Store.try_claim` is specified as CAS and NOTHING ELSE -- an
implementation that pushes-then-arbitrates does not satisfy it.

THIS FILE IS THE CONTRACT, NOT AN IMPLEMENTATION TEST. It runs against an in-memory store whose
only job is to be honest about atomicity; a Gitea/GitHub adapter must pass the same suite. That is
the point of writing it before the adapter exists -- an adapter authored first would define the
contract by whatever it happened to do.
"""

from __future__ import annotations

import threading

import pytest

from agent_swarm.job import TEST_RUN, Job
from agent_swarm.store import InMemoryStore, Store


@pytest.fixture
def store() -> Store:
    return InMemoryStore()


JOB = Job(id='j1', kind=TEST_RUN)


class TestClaimIsCompareAndSwap:
    def test_an_unclaimed_job_can_be_claimed(self, store):
        assert store.try_claim(JOB, owner='runner-a') is True

    def test_a_SECOND_claimant_is_refused(self, store):
        """THE WHOLE POINT. Not "resolves to one winner eventually" -- refused, at the call."""
        store.try_claim(JOB, owner='runner-a')
        assert store.try_claim(JOB, owner='runner-b') is False

    def test_the_owner_is_readable_after_the_claim(self, store):
        store.try_claim(JOB, owner='runner-a')
        assert store.claim_owner(JOB) == 'runner-a'

    def test_reclaiming_by_the_SAME_owner_is_refused_too(self, store):
        """A runner that lost track of its own claim must not "re-take" it and reset the lease --
        that is how a hung run keeps a job locked forever.
        """
        store.try_claim(JOB, owner='runner-a')
        assert store.try_claim(JOB, owner='runner-a') is False

    def test_a_released_job_can_be_claimed_again(self, store):
        store.try_claim(JOB, owner='runner-a')
        store.release(JOB, owner='runner-a')
        assert store.try_claim(JOB, owner='runner-b') is True

    def test_only_the_OWNER_may_release(self, store):
        """Otherwise a stranger frees a live claim and two runners proceed -- the failure the CAS
        was adopted to remove, reintroduced through the back door.
        """
        store.try_claim(JOB, owner='runner-a')
        store.release(JOB, owner='runner-b')
        assert store.claim_owner(JOB) == 'runner-a'

    def test_two_KINDS_of_one_id_do_not_contend(self, store):
        """An issue and its test run share an id and are different work."""
        agent = Job(id='j1', kind=Job(id='x', kind=TEST_RUN).kind)  # same kind, control
        assert store.try_claim(JOB, owner='a') is True
        assert store.try_claim(agent, owner='b') is False  # same kind AND id -> same work


class TestItIsATOMICUnderRealConcurrency:
    def test_exactly_one_of_many_threads_wins(self, store):
        """THE DISCRIMINATING TEST, and the reason this contract exists at all. A push-then-
        arbitrate implementation can let two callers each believe they won; a CAS cannot. Racing
        real threads is the only way to say so -- a sequential test passes for both designs.
        """
        winners: list[str] = []
        lock = threading.Lock()
        barrier = threading.Barrier(16)

        def attempt(n: int) -> None:
            barrier.wait()  # release them together, so the calls genuinely overlap
            if store.try_claim(JOB, owner=f'runner-{n}'):
                with lock:
                    winners.append(f'runner-{n}')

        threads = [threading.Thread(target=attempt, args=(i,)) for i in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(winners) == 1, f'{len(winners)} runners each believed they won: {winners}'
        assert store.claim_owner(JOB) == winners[0]


class TestVerdictsAreRecorded:
    def test_a_verdict_round_trips(self, store):
        store.record_verdict(JOB, verdict='PASS', detail='10646 passed')
        assert store.verdict(JOB) == 'PASS'

    def test_an_unanswered_job_has_NO_verdict(self, store):
        """`None`, never a default of PASS -- an unanswered job read as green is the unearned green
        this whole system exists to prevent.
        """
        assert store.verdict(JOB) is None

    @pytest.mark.parametrize('word', ['PASS', 'FAIL', 'INCONCLUSIVE'])
    def test_the_vocabulary_is_the_GATE_vocabulary(self, store, word):
        """One verdict vocabulary for both kinds: gate.py's three values are the acceptance
        interface for everything -- the worker's definition of done, the runner's report, the
        lead's merge input, the board's column driver.
        """
        store.record_verdict(JOB, verdict=word, detail='')
        assert store.verdict(JOB) == word

    def test_a_word_OUTSIDE_the_vocabulary_is_refused(self, store):
        """INCONCLUSIVE is not a soft FAIL and 'ERROR' is not a fourth state. A store that accepts
        any string lets a caller invent a verdict nothing knows how to act on.
        """
        with pytest.raises(ValueError, match='verdict'):
            store.record_verdict(JOB, verdict='ERROR', detail='')
