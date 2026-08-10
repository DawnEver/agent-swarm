"""Claim arbitration costs a CONSTANT number of round trips per contender. Measured, not assumed.

WHY THIS IS WORTH A TEST RATHER THAN A NOTE. "Tens to hundreds of agents" has three candidate
bottlenecks -- write rate, claim arbitration, read cost -- and their repairs are unrelated. Sharding
the claim space is expensive and invasive, and it is the WRONG repair unless arbitration is
superlinear. So the useful thing to know is whether it is, and that is answerable today: API round
trips are exact and network-independent, so a double counts them as truthfully as a live forge.

MEASURED 2026-08-10, against `RecordingForge`:

    contenders   total calls   per contender
             2             7             3.5
             4            15             3.8
             8            31             3.9
            16            63             3.9
            32           127             4.0
            64           255             4.0

Exactly `4n - 1`, with exactly one winner at every width. **Arbitration is LINEAR and is not the
scaling defect** -- which removes one of the three candidates and leaves write rate and read cost.
That is a negative result and it is the most useful kind here: it stops a costly optimisation being
aimed at the wrong thing.

WHAT WOULD MAKE IT SUPERLINEAR, and therefore what this guards: any loser that re-reads the full
comment list after being beaten, or a retry that walks the list again per attempt. Both are natural
things to add -- "just re-check whether it is still claimed" -- and neither would fail a
correctness test. The cost would surface only as a fleet that gets slower as it grows, which nobody
bisects.

THE COUNT IS OF FORGE CALLS, not of seconds. A timing would measure this box under whatever else it
is doing; the number of round trips is the thing that actually scales, and it is reproducible.
"""

from __future__ import annotations

import pytest

from agent_swarm.forge_store import _LABEL_TO_VERDICT, READY_LABEL, ForgeStore, Role
from agent_swarm.job import TEST_RUN, Job
from agent_swarm.testing import RecordingForge

#: Every method that costs a round trip against a real forge. Named explicitly rather than "every
#: public method": a helper that only reads local state must not inflate the count and make a
#: regression look like an improvement.
_IO_METHODS = frozenset({
    'add_comment', 'comments', 'delete_comment', 'labels', 'add_label', 'remove_label',
    'list_work_items', 'work_item', 'state', 'create_work_item', 'update_comment',
    'close_work_item', 'retire_work_item', 'set_status',
})


class _Counting(RecordingForge):
    """Counts round trips. Wrapping by name rather than by subclass override so a method added to
    the protocol later is counted the moment it is listed above, instead of silently free."""

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def __getattribute__(self, name: str):
        attr = object.__getattribute__(self, name)
        if name in _IO_METHODS and callable(attr):
            def wrapped(*args, **kwargs):
                object.__setattr__(self, 'calls', object.__getattribute__(self, 'calls') + 1)
                return attr(*args, **kwargs)

            return wrapped
        return attr


def _contend(n: int) -> tuple[int, int]:
    """`n` runners race for one job. Returns (round trips spent, winners)."""
    forge = _Counting()
    submitter = ForgeStore('bench', forge, role=Role.SUBMITTER)
    job = Job(id='group/contended', kind=TEST_RUN)
    submitter.register(job)
    forge.calls = 0  # registration is the submitter's cost, not arbitration's
    runners = [ForgeStore('bench', forge, role=Role.RUNNER) for _ in range(n)]
    winners = sum(1 for i, r in enumerate(runners) if r.try_claim(job, owner=f'w{i}'))
    return forge.calls, winners


@pytest.mark.parametrize('n', [2, 4, 8, 16, 32, 64])
def test_exactly_one_runner_wins_at_every_width(n: int):
    """The correctness half. A cost figure for a protocol that admitted two winners would be a
    measurement of the wrong thing."""
    _calls, winners = _contend(n)
    assert winners == 1


@pytest.mark.parametrize('n', [2, 4, 8, 16, 32, 64])
def test_the_cost_per_contender_is_BOUNDED(n: int):
    """Constant per contender, with headroom for a protocol change that is still linear. The
    measured value is ~4; the bar is 6, so this fails on a regression in KIND (a re-read per loser,
    a per-attempt list walk) rather than on a refactor that adds one call."""
    calls, _winners = _contend(n)
    assert calls / n <= 6, f'{calls} round trips for {n} contenders is {calls / n:.1f} each'


def test_the_cost_does_not_GROW_with_the_field():
    """The discriminating shape. A per-contender bound alone would pass for an O(n log n) protocol
    at these widths; comparing the two ends catches a cost that rises with the crowd."""
    small, _ = _contend(4)
    large, _ = _contend(64)
    assert large / 64 <= (small / 4) * 1.5, (
        f'{small / 4:.1f} calls each at 4 contenders, {large / 64:.1f} at 64 -- arbitration is '
        'scaling with the field, and sharding the claim space becomes the right repair'
    )


def test_the_counter_actually_counts():
    """A guard that cannot fire is this repo's most-recorded defect: if the wrapper missed the
    methods, every assertion above would pass by counting zero."""
    calls, _ = _contend(8)
    assert calls > 0


# ---------------------------------------------------------------------------
# WRITE AMPLIFICATION. The other measurable-without-a-network axis, and the one
# I regressed myself.


def _register_cost(n: int) -> float:
    forge = _Counting()
    store = ForgeStore('bench', forge, role=Role.SUBMITTER)
    forge.calls = 0
    for i in range(n):
        store.register(Job(id=f'g/j{i}', kind=TEST_RUN))
    return forge.calls / n


@pytest.mark.parametrize('n', [1, 10, 50])
def test_registering_a_job_costs_ONE_round_trip(n: int):
    """MEASURED AND REGRESSED BY ME. Adding the handover label as a separate `add_label` took
    registration from 1.0 calls per job to 2.0 -- at the aggregate write rate this deployment
    sustains, half the registration throughput spent on a label, on the write path the whole fleet
    shares. Folding it into the create payload put it back to 1.0.

    The bar is exactly 1.0, not "small": any second call here is a doubling, and a bound of 2 would
    accept the regression this test exists to have caught.
    """
    assert _register_cost(n) == 1.0, 'registration costs more than one round trip per job'


def test_the_write_cost_does_not_grow_with_the_number_registered():
    """A per-job constant, not an amortised average: a cache that helps only after the first item
    would still make the hundredth agent's registration cheap and the first fleet's expensive."""
    assert _register_cost(1) == _register_cost(50)


def _verdict_cost() -> int:
    forge = _Counting()
    store = ForgeStore('bench', forge, role=Role.SUBMITTER)
    job = Job(id='g/v', kind=TEST_RUN)
    store.register(job)
    forge.calls = 0
    store.record_verdict(job, verdict='PASS', detail='d')
    return forge.calls


def test_recording_a_verdict_costs_THREE_round_trips():
    """Closing is the last act of EVERY job, so this is a write path the whole fleet traverses.

    It was 4 -- comment, read labels, add label, close -- and 5 when a reopened item still carried a
    stale verdict label. The label read stays, and that is not an oversight: only the current set
    says which stale labels to drop, and an "add this one" verb cannot drop them, so it would need a
    second call regardless. What collapsed is add+close into one PATCH, which is what Gitea's issue
    edit actually offers.
    """
    assert _verdict_cost() == 3


def test_the_verdict_close_PRESERVES_every_other_label():
    """The thing this optimisation could silently break, and the reason it is asserted separately.

    `close_work_item(labels=...)` REPLACES the set. Passing only the verdict label would strip the
    handover label and anything a human attached -- and the loss would surface not as an error but
    as work that quietly stopped being offered, which is the failure mode this whole session keeps
    finding.
    """
    forge = RecordingForge()
    store = ForgeStore('bench', forge, role=Role.SUBMITTER)
    job = Job(id='g/keep', kind=TEST_RUN)
    number = store.register(job)
    forge.add_label(number, 'human:please-review')

    store.record_verdict(job, verdict='PASS', detail='d')
    after = set(forge.labels(number))
    assert 'human:please-review' in after, 'a human label was stripped by recording a verdict'
    assert READY_LABEL in after, 'the handover label was stripped by recording a verdict'


def test_a_STALE_verdict_label_is_replaced_not_accumulated():
    """The reopened case, which is why the read exists. Two verdict labels on one item would make
    `verdict()` answer by whichever the forge happened to list first."""
    forge = RecordingForge()
    store = ForgeStore('bench', forge, role=Role.SUBMITTER)
    job = Job(id='g/reopened', kind=TEST_RUN)
    number = store.register(job)

    store.record_verdict(job, verdict='FAIL', detail='first')
    store.record_verdict(job, verdict='PASS', detail='second')
    verdict_labels = [name for name in forge.labels(number) if name in _LABEL_TO_VERDICT]
    assert len(verdict_labels) == 1, f'accumulated {verdict_labels}'


def _sweep_cost(open_items: int) -> int:
    forge = _Counting()
    submitter = ForgeStore('bench', forge, role=Role.SUBMITTER)
    for i in range(open_items):
        submitter.register(Job(id=f'g/j{i}', kind=TEST_RUN))
    runner = ForgeStore('bench', forge, role=Role.RUNNER)
    forge.calls = 0
    runner.claimable(TEST_RUN)
    return forge.calls


@pytest.mark.parametrize('n', [1, 5, 20, 100])
def test_a_sweep_costs_ONE_round_trip_however_much_work_is_open(n: int):
    """THE SCALING DEFECT OF THE SESSION, and I had it backwards.

    I measured read cost against CLOSED items, found it flat, and said read cost does not grow. It
    does -- against OPEN ones. `claimable` fetched labels per candidate: 101 calls for 100 open
    items, per runner, per sweep. Open work grows WITH fleet size, so at a hundred agents that is
    ten thousand calls a round to re-ask what the first response already contained.

    The listing endpoint carries labels inline on Gitea, so the answer is one call at every size.
    The bar is exactly 1: any per-item work here is N+1 again, and a bound like "under 10" would
    pass for a sweep that is linear in the fleet.
    """
    assert _sweep_cost(n) == 1


def test_the_sweep_cost_does_not_move_between_one_item_and_a_hundred():
    """The shape, not the constant: a per-item cost that happened to be cheap would still be the
    defect, and comparing the ends is what says so."""
    assert _sweep_cost(1) == _sweep_cost(100)


# ---------------------------------------------------------------------------
# THE 7x24 AXIS: call COUNT is not payload SIZE, and I had only been counting calls.


class _Weighing(RecordingForge):
    """Counts items TRANSFERRED, not requests made. One request for a thousand items is one call
    and a thousand items of bandwidth, pagination and server work."""

    def __init__(self) -> None:
        super().__init__()
        self.transferred = 0

    def list_work_items(self, *, state: str = 'all'):
        out = super().list_work_items(state=state)
        self.transferred += len(out)
        return out


def _fresh_runner_claim_payload(history: int) -> int:
    """Items transferred when a FRESH process claims one job, with `history` finished jobs behind
    it. Fresh matters: the in-memory index makes the second lookup free and hides this entirely."""
    forge = _Weighing()
    submitter = ForgeStore('bench', forge, role=Role.SUBMITTER)
    for i in range(history):
        old = Job(id=f'g/old{i}', kind=TEST_RUN)
        submitter.register(old)
        submitter.record_verdict(old, verdict='PASS', detail='d')
    live = Job(id='g/live', kind=TEST_RUN)
    submitter.register(live)

    runner = ForgeStore('bench', forge, role=Role.RUNNER)
    forge.transferred = 0
    runner.try_claim(live, owner='w0')
    return forge.transferred


def test_the_claim_payload_is_measured_at_all():
    """The control: a probe that transferred nothing would make the xfail below meaningless."""
    assert _fresh_runner_claim_payload(0) >= 1


@pytest.mark.parametrize('history', [10, 50, 200])
def test_a_fresh_runners_claim_does_not_transfer_the_whole_history(history: int):
    """WAS A STRICT XFAIL FOR ONE COMMIT, and the strictness is what closed it: bounding the query
    made it XPASS, which FAILED the suite and forced this conversion instead of letting a green
    assertion sit under a "known failure" label.

    MEASURED before: 201 items transferred behind 200 finished jobs, 51 behind 50 -- one REQUEST,
    the whole history in its body. The lookup now asks for open items first and falls back to
    everything only when that misses, so the hot path (claiming, deduping against live work) is
    bounded by OPEN work rather than by everything ever run.

    Compared against the empty-history cost rather than a constant: what must not happen is GROWTH,
    and a fixed bar would silently accept a payload that got bigger but stayed under it.
    """
    assert _fresh_runner_claim_payload(history) == _fresh_runner_claim_payload(0)


def test_a_CLOSED_item_is_still_findable_from_a_fresh_process():
    """The correctness half of the narrowing, and the reason it is open-FIRST rather than open-ONLY.
    A retired duplicate must stay findable, or `reconcile_duplicates` reports a clean tracker it
    cannot actually see -- trading a bandwidth bill for a correctness hole."""
    forge = _Weighing()
    submitter = ForgeStore('bench', forge, role=Role.SUBMITTER)
    job = Job(id='g/done', kind=TEST_RUN)
    number = submitter.register(job)
    submitter.record_verdict(job, verdict='PASS', detail='d')

    fresh = ForgeStore('bench', forge, role=Role.RUNNER)
    title = next(i.title for i in forge.list_work_items() if i.number == number)
    assert fresh._lowest_numbered(title) == number
