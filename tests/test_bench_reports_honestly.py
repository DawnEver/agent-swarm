"""The measurement harness must not report a number that flatters a failure.

WHY TEST A BENCHMARK. Every optimisation after this point is aimed by what it prints, and its
failure modes are all the quiet kind: a partial run looks like a fast one, a mean hides a queue, and
a configuration where everything errored has no latency at all -- which formats as 0.000s, the best
possible result, for total failure.

THE THREE PROBES ARE SEPARATE ON PURPOSE. Write rate, claim arbitration and read cost need
unrelated repairs, and optimising the wrong one is worse than doing nothing: batching writes when
the real cost is contention makes contention WORSE, because more runners arrive at the same instant.
A harness that reported one blended "throughput" would actively mislead.
"""

from __future__ import annotations


from agent_swarm import bench
from agent_swarm.testing import RecordingForge


def test_a_failed_operation_is_counted_not_dropped():
    """An error rate that rises with concurrency IS the finding. Dropping failures would report the
    surviving subset as the throughput -- the more it breaks, the better it looks."""

    def boom(i: int) -> None:
        if i % 2 == 0:
            msg = 'forge said no'
            raise RuntimeError(msg)

    result = bench.measure('half-fail', boom, concurrency=4)
    assert result.failures == 2
    assert len(result.samples) == 4


def test_a_run_where_everything_failed_is_INCONCLUSIVE_not_fast():
    """The dangerous format. With no successful sample, a percentile of 0.0 would read as
    instantaneous -- the fastest possible answer, printed for a total failure."""

    def always_boom(_i: int) -> None:
        msg = 'down'
        raise RuntimeError(msg)

    result = bench.measure('dead', always_boom, concurrency=3)
    assert result.percentile(50) is None
    assert 'INCONCLUSIVE' in result.report()
    assert '0.000' not in result.report()


def test_the_report_states_the_concurrency():
    """A rate is not a property of a forge; it is a property of a forge AT SOME NUMBER OF WRITERS.
    A figure quoted without it will be compared against one taken at a different width."""
    result = bench.measure('noop', lambda _i: None, concurrency=7)
    assert '@ 7x' in result.report()


def test_the_report_carries_a_TAIL_not_only_a_middle():
    """A queue's mean is the number that hides the queue."""
    result = bench.measure('noop', lambda _i: None, concurrency=4)
    assert 'p95' in result.report()


def test_repeats_expose_the_spread_rather_than_averaging_it():
    """Never compare two configurations from one run each: a single pair cannot tell a difference
    from the load somebody else put on the box."""
    runs = bench.repeat('noop', lambda _i: None, concurrency=2, repeats=3)
    assert len(runs) == 3
    assert 'across 3 repeats' in bench.spread(runs)


def test_the_spread_of_a_dead_configuration_is_INCONCLUSIVE():
    def always_boom(_i: int) -> None:
        msg = 'down'
        raise RuntimeError(msg)

    runs = bench.repeat('dead', always_boom, concurrency=2, repeats=2)
    assert 'INCONCLUSIVE' in bench.spread(runs)


# --------------------------------------------------------------------------- the probes are apart


def test_the_claim_probe_contends_on_ONE_item():
    """Giving each worker its own item would measure the write rate again under another name, and
    the answer would say nothing about arbitration."""
    forge = RecordingForge()
    number = forge.create_work_item(title='[swarm] bench/contended', body='b')
    bench.bench_claim(forge, number, concurrency=5)
    assert len(forge.comments(number)) == 5


def test_the_read_probe_asks_for_OPEN_items():
    """It must measure what `claimable` actually pays, not a cheaper or more expensive question."""
    asked: list[str] = []
    forge = RecordingForge()
    original = forge.list_work_items
    forge.list_work_items = lambda *, state='all': (asked.append(state), original(state=state))[1]  # type: ignore[method-assign]
    bench.bench_list(forge, concurrency=2)
    assert set(asked) == {'open'}


def test_the_three_probes_are_distinct_callables():
    """One blended "throughput" number would aim every later optimisation at an average of three
    unrelated costs."""
    names = {bench.bench_create.__name__, bench.bench_claim.__name__, bench.bench_list.__name__}
    assert len(names) == 3
