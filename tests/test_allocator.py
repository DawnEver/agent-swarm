"""What the allocator must do, stated before it exists.

The load-bearing test in this file is `test_a_starved_job_is_eventually_picked`, which FAILS against
the obvious implementation (highest priority first). Everything else guards a property that would
otherwise be a docstring nobody checks.
"""

from __future__ import annotations

import pytest

from agent_swarm.allocator import (
    PRIORITY_MAX,
    Candidate,
    choose,
    rank,
    starvation_bound_s,
)
from agent_swarm.job import AGENT_TASK, TEST_RUN, Job
from agent_swarm.loop import Box

IDLE = Box(available_gib=64.0)


def _job(name: str, **kw) -> Job:
    return Job(id=name, kind=TEST_RUN, exclusivity='cheap', **kw)


def _cand(name: str, priority: int = 0, ready_at: float = 0.0, **kw) -> Candidate:
    return Candidate(job=_job(name), priority=priority, ready_at=ready_at, **kw)


class TestPurity:
    def test_the_same_inputs_give_the_same_answer(self) -> None:
        cands = [_cand('a', priority=3), _cand('b', priority=3)]
        first = choose(cands, IDLE, now=1000.0)
        for _ in range(20):
            assert choose(cands, IDLE, now=1000.0) is first

    def test_nothing_available_is_none_not_an_error(self) -> None:
        assert choose([], IDLE, now=0.0) is None

    def test_the_order_of_the_input_does_not_change_the_answer(self) -> None:
        cands = [_cand('a', priority=5), _cand('b', priority=5), _cand('c', priority=5)]
        answer = choose(cands, IDLE, now=0.0)
        assert choose(list(reversed(cands)), IDLE, now=0.0) == answer


class TestItDoesNotDuplicateAdmission:
    def test_a_job_the_box_refuses_is_never_returned(self) -> None:
        # The box holds an `expensive` lock, so nothing may start beside it. The allocator must
        # learn this from `Box.blockers`, not from a capacity rule of its own.
        busy = Box(available_gib=64.0, held={'expensive': True})
        assert choose([_cand('a', priority=9)], busy, now=0.0) is None

    def test_a_refused_high_priority_job_does_not_mask_an_admissible_low_one(self) -> None:
        tiny = Box(available_gib=8.0)
        big = Candidate(job=Job(id='big', kind=TEST_RUN, exclusivity='cheap', ram_gib=32.0), priority=9)
        small = Candidate(job=Job(id='small', kind=TEST_RUN, exclusivity='cheap', ram_gib=1.0), priority=0)
        assert choose([big, small], tiny, now=0.0) == small.job

    def test_unpriced_work_stays_schedulable(self) -> None:
        # `ram_gib is None` is the common case today. Admission prices it (12.5 GiB assumed); the
        # allocator must not add a rule of its own that filters unpriced work out of the ranking --
        # running it is how a price gets measured.
        unpriced = _cand('unpriced', priority=1)
        assert unpriced.job.ram_gib is None
        assert choose([unpriced], IDLE, now=0.0) == unpriced.job

    def test_a_job_out_of_retries_is_not_picked(self) -> None:
        # `admission.should_retry` already owns this budget; the allocator consumes it.
        dead = _cand('dead', priority=9, results=('INCONCLUSIVE',) * 5, max_retries=3)
        alive = _cand('alive', priority=0)
        assert choose([dead, alive], IDLE, now=0.0) == alive.job

    def test_an_answered_job_is_not_picked(self) -> None:
        answered = _cand('answered', priority=9, results=('PASS',))
        assert choose([answered], IDLE, now=0.0) is None


class TestStarvation:
    """The failure mode this module exists to prevent, and it is silent."""

    def test_priority_wins_when_ages_are_equal(self) -> None:
        low, high = _cand('low', priority=1), _cand('high', priority=8)
        assert choose([low, high], IDLE, now=0.0) == high.job

    def test_a_starved_job_is_eventually_picked(self) -> None:
        """FAILS on naive highest-priority-first: `naive` below never picks `low`, forever.

        The fleet looks busy, every individual decision looks correct, and one item is never done.
        """
        ageing = 60.0
        low = _cand('low', priority=0, ready_at=0.0)
        bound = starvation_bound_s(low.priority, ageing)

        def naive(cands):
            return max(cands, key=lambda c: c.priority).job

        picked_naive = picked_aged = None
        # A fresh top-priority job arrives on every tick -- the queue is never empty of urgent work.
        for tick in range(1, int(bound / ageing) + 5):
            now = tick * ageing
            cands = [low, _cand(f'urgent-{tick}', priority=PRIORITY_MAX, ready_at=now)]
            if picked_naive is None and naive(cands) == low.job:
                picked_naive = now
            if picked_aged is None and choose(cands, IDLE, now=now, ageing_seconds=ageing) == low.job:
                picked_aged = now

        assert picked_naive is None, 'the naive rule was supposed to starve `low` -- this test proves nothing now'
        assert picked_aged is not None, 'a low-priority job was starved forever'
        # AT the bound the scores tie and the claim-key sort decides, so the guarantee is "no later
        # than the bound plus one tick" -- never "exactly at it".
        assert picked_aged <= bound + ageing, f'picked at {picked_aged}s, later than the declared bound {bound}s'

    def test_the_declared_bound_is_the_one_the_ranking_uses(self) -> None:
        # A bound in prose is a bound nothing checks. At exactly the bound the scores TIE (so the
        # tie-break decides); strictly past it, the aged job outranks a fresh top-priority one.
        ageing = 100.0
        for priority in range(PRIORITY_MAX + 1):
            bound = starvation_bound_s(priority, ageing)
            old = _cand('old', priority=priority, ready_at=0.0)
            fresh = _cand('fresh', priority=PRIORITY_MAX, ready_at=bound + 1.0)
            assert choose([old, fresh], IDLE, now=bound + 1.0, ageing_seconds=ageing) == old.job

    def test_a_priority_outside_the_declared_range_raises(self) -> None:
        # An unbounded priority makes the starvation bound a lie: no finite wait overtakes a job
        # someone gave priority 10_000. The bound is only real because the range is closed.
        with pytest.raises(ValueError, match='priority'):
            _cand('impossible', priority=PRIORITY_MAX + 1)


class TestTieBreak:
    def test_a_tie_is_broken_deterministically_not_randomly(self) -> None:
        a, b = _cand('aaa', priority=4), _cand('bbb', priority=4)
        # Same score, same age: every box in the fleet must reach the SAME answer, so the collision
        # happens on the claim (a real CAS, already built and tested) rather than being made rare.
        assert choose([a, b], IDLE, now=0.0) == choose([b, a], IDLE, now=0.0)

    def test_the_tie_break_key_is_the_claim_key(self) -> None:
        # Two jobs, same id, different kinds -- distinct work, and the order must not depend on
        # which one the caller listed first.
        agent = Candidate(job=Job(id='x', kind=AGENT_TASK, exclusivity='cheap'), priority=4)
        test = Candidate(job=Job(id='x', kind=TEST_RUN, exclusivity='cheap'), priority=4)
        ordered = rank([test, agent], IDLE, now=0.0)
        assert [j.claim_key() for j in ordered] == sorted(j.claim_key() for j in ordered)


class TestRank:
    def test_rank_returns_every_admissible_job_so_a_claim_loser_can_re_pick(self) -> None:
        # The loser of a claim race must not have to recompute: it takes the next one down.
        cands = [_cand('a', priority=1), _cand('b', priority=7), _cand('c', priority=4)]
        ordered = rank(cands, IDLE, now=0.0)
        assert [j.id for j in ordered] == ['b', 'c', 'a']
        assert choose(cands, IDLE, now=0.0) == ordered[0]

    def test_a_job_not_yet_ready_is_not_offered(self) -> None:
        future = _cand('future', priority=9, ready_at=100.0)
        assert rank([future], IDLE, now=99.0) == []
        assert rank([future], IDLE, now=100.0) == [future.job]
