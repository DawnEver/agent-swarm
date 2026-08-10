"""Runners pick different work without talking to each other -- and none of them starves.

WHY THIS EXISTS, in numbers measured against the live forge on 2026-08-10. Claim arbitration is
the only per-job cost that grows with the fleet. On ONE contended item it elects exactly one winner
per round, and the round costs 297 ms at 2 racers, 667 ms at 8, 1373 ms at 16 -- linear, about
85 ms per extra contender. So a contended item caps its whole group at one job per round however
many agents join, while agents on DISTINCT items scale against the per-call floor (~60 ms p50).
Every loser paid a full round to learn it lost.

That is not repaired by a faster protocol. It is repaired by fewer racers per item, and the only
way to get that without a coordinator -- a coordinator would need its own claim -- is for each
runner to start at a different offset it can compute alone.

THE TWO FAILURE MODES THIS SITS BETWEEN, and the tests below are one per side:

* Everyone takes `jobs[0]`. Maximum contention; N-1 runners do nothing per round.
* Everyone takes a PARTITION, `jobs[i::n]`. Zero contention and STARVATION: three items, ten
  runners, seven get an empty shard and idle while work sits visible. Worse than the first, because
  idle-with-work-available is invisible -- nothing errors, the queue just drains slowly.

A permutation is the shape that has neither. Every job stays reachable by every owner, so a runner
that loses its first race walks the rest and the system degrades to the old behaviour rather than
to starvation.
"""

from __future__ import annotations

import pytest

from agent_swarm.forge_store import Claimable
from agent_swarm.job import TEST_RUN, Job


def _claimable(n: int) -> Claimable:
    return Claimable(jobs=tuple(Job(id=f'group/j{i}', kind=TEST_RUN) for i in range(n)))


def _owners(n: int) -> list[str]:
    return [f'runner-{i}' for i in range(n)]


# --------------------------------------------------------------------------- it spreads


#: (fleet size, items in flight AT LEAST, worst crowd AT MOST) -- MEASURED OFFLINE AND CONFIRMED
#: LIVE: the 16-runner row reproduced verbatim against the real forge (7 in flight, worst crowd 4).
#:
#: WHAT DID NOT SURVIVE: "~N jobs per round". The real figure is ~0.44N -- 3 of 8 and 7 of 16 --
#: which is the balls-in-bins expectation, not a defect. The claim was refuted the first time it
#: was measured live, and the bound below is the measurement rather than the hope.
#:
#: The status quo is the row nobody writes: every fleet size puts ALL of its runners on item 0 and
#: keeps ONE item in flight. These bounds are what the rotation actually delivers, with a little
#: slack, and they are deliberately not the theoretical optimum: n hashes into n bins is the
#: balls-in-bins problem, whose expected distinct count is ~0.63n and whose expected max load grows
#: like ln n / ln ln n. Demanding "all distinct" would be demanding a perfect hash, and a test that
#: demands one gets satisfied by hard-coding an assignment -- which needs the coordination this
#: whole mechanism exists to avoid.
_SPREAD = ((8, 3, 4), (16, 6, 5), (32, 12, 6))


@pytest.mark.parametrize(('n', 'least_in_flight', 'worst_crowd'), _SPREAD)
def test_the_fleet_spreads_over_many_items_instead_of_one(n: int, least_in_flight: int, worst_crowd: int):
    """THE POINT, in the two quantities that map onto the measurement.

    ITEMS IN FLIGHT is throughput: one contended item completes one job per round however many
    agents watch it, so k distinct leaders is k jobs progressing at once.

    WORST CROWD is waste: every racer that is not the winner paid a full round to learn it lost.

    IT IS A THROUGHPUT WIN AND NOT A LATENCY ONE, measured live 2026-08-10 and stated here because
    the first version of this docstring claimed the opposite. Round WALL CLOCK barely moves (N=16:
    2194 ms contended vs 2495 ms spread -- the spread arm was slightly SLOWER), because the wall is
    dominated by the aggregate API-call volume both arms issue, not by per-item contention. What
    moves is jobs completed per round: 1 -> 7 at N=16, 1 -> 3 at N=8, i.e. 0.5 -> 2.8 and
    1.2 -> 4.2 jobs/s. The next ceiling is the forge's aggregate throughput under concurrency, not
    contention.
    """
    work = _claimable(n)
    firsts = [work.preferred(owner)[0].id for owner in _owners(n)]
    in_flight = len(set(firsts))
    busiest = max(firsts.count(job) for job in set(firsts))
    assert in_flight >= least_in_flight, f'only {in_flight} of {n} items in flight: {firsts}'
    assert busiest <= worst_crowd, f'{busiest} of {n} runners still contend on one item: {firsts}'


def test_a_tiny_fleet_is_NOT_claimed_to_spread_well():
    """The honest boundary. At n=4 the measured worst crowd is 3 of 4 -- collisions dominate at small
    n and no hash fixes that. Recorded as a KNOWN limit rather than left for a reader to assume the
    mechanism helps everywhere: below roughly eight runners, contention is barely reduced, and the
    reason it does not matter is that a 3-way race costs ~300 ms, not 1373 ms.
    """
    work = _claimable(4)
    firsts = [work.preferred(owner)[0].id for owner in _owners(4)]
    assert max(firsts.count(job) for job in set(firsts)) == 3


# --------------------------------------------------------------------------- nobody starves


@pytest.mark.parametrize(('items', 'runners'), [(1, 10), (3, 10), (10, 3)])
def test_every_runner_can_still_reach_every_job(items: int, runners: int):
    """THE ANTI-PARTITION HALF, and the one that matters more.

    `jobs[i::n]` would give seven of ten runners an empty list when there are three items -- zero
    contention and seven idle agents, with nothing to report it. A permutation cannot do that.
    """
    work = _claimable(items)
    for owner in _owners(runners):
        assert {job.id for job in work.preferred(owner)} == {job.id for job in work.jobs}


def test_one_item_is_offered_to_everyone():
    """The degenerate case a partition gets exactly wrong: the last job in the queue must not
    belong to one runner while the rest idle.
    """
    work = _claimable(1)
    assert all(len(work.preferred(owner)) == 1 for owner in _owners(10))


def test_no_job_is_dropped_or_duplicated():
    """It is a permutation, asserted as one -- a rotation implemented with the wrong slice bound
    silently loses the boundary element, and the loss would look like a job nobody picked up.
    """
    work = _claimable(7)
    for owner in _owners(20):
        got = [job.id for job in work.preferred(owner)]
        assert sorted(got) == sorted(job.id for job in work.jobs)
        assert len(set(got)) == len(got)


# --------------------------------------------------------------------------- it is stable


def test_the_same_owner_gets_the_same_order_every_time():
    """A retry must race the item it just lost, not a fresh one -- otherwise a losing runner walks
    the queue forever and the last item is never taken.
    """
    work = _claimable(9)
    assert [j.id for j in work.preferred('runner-3')] == [j.id for j in work.preferred('runner-3')]


def test_the_order_does_not_depend_on_the_process():
    """`hash()` is salted per process, so two runs of the same runner would prefer different items.
    Pinned as a VALUE rather than as "it is deterministic", which a salted hash also satisfies
    within one process.
    """
    work = _claimable(4)
    assert [job.id for job in work.preferred('runner-0')] == [
        'group/j3',
        'group/j0',
        'group/j1',
        'group/j2',
    ]


def test_an_empty_result_is_empty_rather_than_an_error():
    """A runner asking when there is nothing visible is the ordinary idle tick, not a failure."""
    assert Claimable(jobs=()).preferred('runner-0') == ()


def test_it_does_not_resurrect_the_banned_truthiness():
    """`Claimable.__bool__` raises on purpose; a helper that internally tested the wrapper for
    truth would reintroduce the "no work visible" / "no work exists" confusion inside the class
    that exists to forbid it.
    """
    with pytest.raises(TypeError):
        bool(_claimable(3))
