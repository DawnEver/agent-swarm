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

import pytest

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


# --------------------------------------------------------------------------- the way in


class TestItIsRunnableAndCannotSurpriseYou:
    """UNTIL 2026-08-12 THIS MODULE HAD NO ENTRY POINT AT ALL -- no `__main__`, no console script, no
    importer. Everything above was reachable only from this file.

    That is the sharpest form of "available, not enforced": a tool whose entire purpose is to stop
    someone optimising toward a plausible answer could not be run by the person about to do it. The
    number then comes from memory, and this project has an instance from that same night -- a 60 ms
    figure that measurement moved to 36.4 ms.

    So these test the ENTRY, and the two guards that keep it off somebody's live tracker.
    """

    def test_a_dry_run_is_the_DEFAULT_and_touches_no_forge(self, capsys, monkeypatch):
        """THE DISCRIMINATING ASSERTION. This tool WRITES -- real items, real comments, on whatever
        `--repo` names -- and it cannot delete them afterwards. So the dry default is the safety
        property, and the first command an operator types must not be the one that surprises them.

        **IT IS ASSERTED RATHER THAN TRUSTED BECAUSE A DRY RUN IN THIS FLEET ALREADY LIED.** Measured
        2026-08-11: `ci_tick`'s `--dry-run` returned before the claim, while FOUR writes above the
        return line had already reached the forge -- and its help said "run nothing". A dry-run flag
        is a claim about every line above it, which is exactly the kind of scope claim that needs its
        own test rather than a reading.

        THE SHAPE IS ASSERTED AT THE DOOR, not by counting requests. `default_forge` is the only way
        to the network from here, so a run that never reaches it cannot have written -- and unlike a
        request counter, that stays true for a write somebody adds tomorrow.
        """
        import agent_swarm.forge as forge_module

        monkeypatch.setattr(forge_module, 'default_forge', lambda *a, **k: pytest.fail('a dry run reached the forge'))
        assert bench.main(['--repo', 'o/r']) == 0
        out = capsys.readouterr().out
        assert 'DRY RUN' in out

    def test_the_dry_run_states_the_COST_as_a_quantity(self, capsys, monkeypatch):
        """An operator confirms a MEASUREMENT, not an intention -- the shape `prune-issues` already
        settles in this package. "This creates real work items" is the KIND of harm; "48 items" is
        the size, which is the part that decides whether to run it against a live repo.
        """
        import agent_swarm.forge as forge_module

        monkeypatch.setattr(forge_module, 'default_forge', lambda *a, **k: pytest.fail('reached the forge'))
        bench.main(['--repo', 'o/r', '--concurrency', '16', '--repeats', '3'])
        out = capsys.readouterr().out
        assert '48 work items created' in out and '16 comments' in out

    def test_the_repo_is_REQUIRED_and_never_defaulted(self):
        """Same reasoning as `default_forge`'s missing default, and it matters more here because this
        one WRITES: a package that defaulted the project would fill a stranger's issue tracker.
        """
        with pytest.raises(SystemExit) as caught:
            bench.main([])
        assert caught.value.code == 2

    def test_a_degenerate_concurrency_is_refused_rather_than_run(self, capsys):
        """`--concurrency 0` makes `ThreadPoolExecutor(max_workers=0)` raise from inside the timing
        loop, where it would be caught as a failed SAMPLE and reported as an error rate -- a
        configuration error rendered as a measurement.
        """
        assert bench.main(['--repo', 'o/r', '--concurrency', '0']) == 2
        assert '--concurrency' in capsys.readouterr().err

    def test_a_real_run_reports_all_three_probes_and_says_what_it_left_behind(self, capsys, monkeypatch):
        """`--yes` against a recording double. Three probes are the whole design -- one blended
        throughput would actively mislead, since their repairs are unrelated -- so a run that
        silently dropped one must fail here.

        **THIS IS ALSO THE CONTROL FOR THE DRY-RUN TESTS ABOVE, and without it they prove nothing.**
        An entry point that is permanently inert reads EXACTLY like one that is safely dry: both
        reach no forge, both exit 0, and the dry assertions pass for a `main` that does nothing at
        all under any flag. Something has to prove `--yes` opens the door.

        SO IT ASSERTS THE FORGE'S STATE, not the headings. Printed section titles would still appear
        for a run whose probes all silently no-opped; `forge.items` and `forge.comments` are what a
        write actually reaching the forge looks like. The instrument is checked on the axis it exists
        to measure, which is the same reason `test_cost_axes` counts calls rather than results.
        """
        import agent_swarm.forge as forge_module

        forge = RecordingForge()
        monkeypatch.setattr(forge_module, 'default_forge', lambda *a, **k: forge)
        assert bench.main(['--repo', 'o/r', '--concurrency', '2', '--repeats', '1', '--yes']) == 0

        # 2 writes + 1 contended item; 2 comments on that item. The DOOR IS OPEN.
        assert len(forge.items) == 3, f'--yes reached the forge but created {len(forge.items)} items'
        contended = max(forge.items)
        assert len(forge.comments(contended)) == 2, 'the arbitration probe contended on nothing'

        out = capsys.readouterr().out
        assert 'WRITE RATE' in out and 'CLAIM ARBITRATION' in out and 'READ COST' in out
        assert 'NOT removed' in out, 'the items it created are not cleaned up; silence would imply they were'

    def test_an_INCONCLUSIVE_run_EXITS_NONZERO(self, capsys, monkeypatch):
        """A partial run's throughput looks like a fast one. The methodology already refuses to print
        a smaller number; this carries the same refusal into the exit code, so a script cannot read a
        failed measurement as a good one.
        """
        import agent_swarm.forge as forge_module

        class DeadForge(RecordingForge):
            def create_work_item(self, **_kwargs):
                msg = 'forge down'
                raise RuntimeError(msg)

        monkeypatch.setattr(forge_module, 'default_forge', lambda *a, **k: DeadForge())
        assert bench.main(['--repo', 'o/r', '--concurrency', '2', '--repeats', '1', '--yes']) == 1
        assert 'INCONCLUSIVE' in capsys.readouterr().out


def test_it_matches_rem_bridges_entry_idiom_rather_than_inventing_a_second():
    """ONE IDIOM FOR A RUNNABLE MODULE. `rem_bridge` is the other one, and it is `main(argv)` plus a
    `__main__` guard with no console_script. Two spellings of "how you run a thing in this package"
    is the duplicated-scheme defect wearing a launcher costume.
    """
    from pathlib import Path

    source = Path(bench.__file__).read_text(encoding='utf-8')
    assert 'def main(argv: list[str] | None = None) -> int:' in source
    assert "if __name__ == '__main__':" in source
    assert 'raise SystemExit(main())' in source
