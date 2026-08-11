"""Measure the three candidate bottlenecks SEPARATELY, because their fixes are unrelated.

WHY THIS EXISTS BEFORE THE CREDENTIALS DO. The remaining question for "tens to hundreds of agents"
is a throughput number nobody has, and the temptation is to optimise toward the plausible answer.
The three candidates need three different repairs:

* **write rate** -- the forge's cost per created item. Fixed by batching, or by not creating an item
  per job at all.
* **claim arbitration** -- the comment round-trips a runner spends discovering it lost a race. Fixed
  by sharding the claim space so fewer runners contend for one item.
* **read cost** -- what a sweep pays to find work. Already bounded to open items; measured here so
  the bound is demonstrated on a real deployment rather than asserted from a double.

Optimising the wrong one is not merely wasted: batching writes when the cost is arbitration makes
contention WORSE, since more runners arrive at the same instant.

THE METHODOLOGY IS THE POINT, and it is this repo's, not this file's invention:

* **Never compare two configurations from one run each.** Every measurement takes `repeats` and
  reports the spread. A single pair of numbers cannot distinguish a difference from the load
  somebody else put on the box.
* **Report percentiles, not a mean.** A queue's mean is the number that hides the queue.
* **State the concurrency with every figure.** "0.95 push/s" is not a property of a forge; it is a
  property of a forge at sixteen writers.
* **A run that could not finish reports INCONCLUSIVE**, never a smaller number. A partial run's
  throughput looks like a fast one.

Nothing here decides anything. It measures, and prints what it measured, so the optimisation that
follows is aimed at a bottleneck somebody saw.

    python -m agent_swarm.bench --repo OWNER/NAME          what it WOULD do, and what it would cost
    python -m agent_swarm.bench --repo OWNER/NAME --yes    actually measure

**IT IS RUNNABLE, AND UNTIL 2026-08-12 IT WAS NOT.** No `__main__`, no console script, no importer:
the only way to reach any of it was from its own test. A measurement tool nobody can invoke is worse
than an absent one, because the number it would have produced gets supplied from memory instead --
and this project has an instance from that same night, a 60 ms figure that measurement moved to
36.4 ms. The file that exists to stop someone optimising toward a plausible answer could not be run
by the person about to do it.
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover -- typing only
    from collections.abc import Callable, Sequence

    from agent_swarm.forge import Forge


@dataclass(frozen=True)
class Sample:
    """One timed operation. `ok` is False for an operation that RAISED, and those are counted rather
    than dropped: an error rate that rises with concurrency IS the finding, and discarding failures
    would report the surviving subset as the throughput."""

    seconds: float
    ok: bool


@dataclass
class Measurement:
    """What one configuration produced. Carries the spread, never a lone number."""

    name: str
    concurrency: int
    samples: list[Sample] = field(default_factory=list)

    @property
    def failures(self) -> int:
        return sum(1 for s in self.samples if not s.ok)

    @property
    def durations(self) -> list[float]:
        return sorted(s.seconds for s in self.samples if s.ok)

    def percentile(self, p: float) -> float | None:
        """The p-th percentile of successful operations, or None if there are none.

        None rather than 0.0: a configuration in which everything failed has no latency, and a zero
        would read as instantaneous -- the fastest possible result, reported for total failure.
        """
        values = self.durations
        if not values:
            return None
        index = min(len(values) - 1, int(round((p / 100.0) * (len(values) - 1))))
        return values[index]

    def report(self) -> str:
        if not self.durations:
            return f'{self.name} @ {self.concurrency}x: INCONCLUSIVE -- {self.failures} failed, 0 succeeded'
        p50, p95 = self.percentile(50), self.percentile(95)
        total = sum(self.durations)
        rate = len(self.durations) / total * self.concurrency if total else float('inf')
        return (
            f'{self.name} @ {self.concurrency}x: '
            f'n={len(self.durations)} fail={self.failures} '
            f'p50={p50:.3f}s p95={p95:.3f}s ~{rate:.2f}/s aggregate'
        )


def measure(name: str, op: Callable[[int], None], *, concurrency: int, per_worker: int = 1) -> Measurement:
    """Run `op(i)` on `concurrency` threads, timing each call.

    THREADS, NOT PROCESSES: the thing under test is a network round trip, so the GIL is released for
    the part that matters, and processes would add a startup cost to every figure.
    """
    out = Measurement(name=name, concurrency=concurrency)

    def one(i: int) -> Sample:
        start = time.perf_counter()
        try:
            op(i)
        except Exception:  # noqa: BLE001 -- an error rate under load IS the measurement
            return Sample(time.perf_counter() - start, ok=False)
        return Sample(time.perf_counter() - start, ok=True)

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        out.samples = list(pool.map(one, range(concurrency * per_worker)))
    return out


def repeat(name: str, op: Callable[[int], None], *, concurrency: int, repeats: int = 3) -> list[Measurement]:
    """The same configuration, several times.

    NEVER COMPARE TWO CONFIGURATIONS FROM ONE RUN EACH -- a single pair cannot tell a difference
    from the load somebody else put on the box. Returning the list rather than an average keeps the
    spread visible to whoever reads it.
    """
    return [measure(f'{name}#{r + 1}', op, concurrency=concurrency) for r in range(repeats)]


def spread(runs: Sequence[Measurement]) -> str:
    """The p50 across repeats, as a range. A single number here would re-hide what `repeat` exposed."""
    values = [m.percentile(50) for m in runs]
    good = [v for v in values if v is not None]
    if not good:
        return 'INCONCLUSIVE -- no repeat produced a successful operation'
    return (
        f'p50 across {len(runs)} repeats: {min(good):.3f}s .. {max(good):.3f}s (median {statistics.median(good):.3f}s)'
    )


# --------------------------------------------------------------------------- the three probes


def bench_create(forge: Forge, *, concurrency: int, prefix: str) -> Measurement:
    """WRITE RATE: cost of creating one work item, under contention."""
    return measure(
        'create',
        lambda i: forge.create_work_item(title=f'{prefix}/create-{i}', body='bench'),
        concurrency=concurrency,
    )


def bench_claim(forge: Forge, number: int, *, concurrency: int) -> Measurement:
    """CLAIM ARBITRATION: many runners contending for ONE item.

    Deliberately one item rather than one each: the question is what contention costs, and giving
    each worker its own item measures the write rate again under a different name.
    """
    return measure('claim', lambda i: forge.add_comment(number, f'claim by w{i}'), concurrency=concurrency)


def bench_list(forge: Forge, *, concurrency: int) -> Measurement:
    """READ COST: what a sweep pays. Open-only, which is what `claimable` actually asks for."""
    return measure('list-open', lambda _i: forge.list_work_items(state='open'), concurrency=concurrency)


# --------------------------------------------------------------------------- the way in


def cost_of_a_run(*, concurrency: int, repeats: int) -> str:
    """What `--yes` will do to the forge, in items and comments. PRINTED BEFORE IT IS DONE.

    THIS IS NOT A DISCLAIMER, IT IS THE CONFIRMABLE QUANTITY. `prune-issues` in this package already
    settles the shape: an operator confirms a MEASUREMENT, not an intention. A benchmark that says
    "this creates real work items" tells you the kind of harm; saying "48 items and 16 comments on
    OWNER/NAME" tells you the size, which is the part that decides whether to run it on a live repo.

    THEY ARE NOT CLEANED UP, and that is stated rather than fixed. Deleting them needs owner or admin
    rights the role accounts do not have -- measured 2026-08-10, the four role credentials get 403 on
    issue deletion -- so a `--cleanup` flag would fail for exactly the identity running this. It is
    `swarmctl prune-issues` afterwards, or a scratch repo.
    """
    return (
        f'{concurrency * repeats} work items created, {concurrency} comments on one of them, '
        f'{concurrency * repeats} list sweeps'
    )


def main(argv: list[str] | None = None) -> int:
    """`python -m agent_swarm.bench --repo OWNER/NAME` -- measure the three bottlenecks apart.

    DRY RUN BY DEFAULT, and `--repo` has NO DEFAULT. Two separate guards for two separate mistakes:

    * `--repo` is required for the reason `default_forge`'s is -- a package that defaulted the
      project would write to a stranger's issue tracker, and the whole point of this tool is that it
      WRITES. A host may be defaulted, because a host is where the swarm lives; a repo is whose
      tracker you are filling.
    * `--yes` is required because the dry run is the honest default for anything irreversible on a
      shared server. It prints the cost and exits 0, so the first invocation an operator types
      cannot be the one that surprises them.

    EXIT 1 ON INCONCLUSIVE, and it is not pedantry. A run that could not finish reports INCONCLUSIVE
    rather than a smaller number, because a partial run's throughput looks like a fast one -- the
    file's own methodology, made reachable by the exit code so a script cannot read a failed
    measurement as a good one.
    """
    parser = argparse.ArgumentParser(
        prog='python -m agent_swarm.bench',
        description=(
            'Measure write rate, claim arbitration and read cost SEPARATELY, against a real forge. '
            'IT CREATES REAL WORK ITEMS AND COMMENTS on --repo and does not remove them '
            '(deletion needs owner/admin rights the role accounts lack). Use a scratch repo, or '
            'clean up afterwards with `swarmctl prune-issues`. Dry run unless --yes.'
        ),
    )
    parser.add_argument('--repo', required=True, help='OWNER/NAME -- required, never defaulted: this WRITES')
    parser.add_argument('--concurrency', type=int, default=8, help='simultaneous workers (default: 8)')
    parser.add_argument('--repeats', type=int, default=3, help='repeats per configuration (default: 3)')
    parser.add_argument('--base-url', default=None, help="forge base URL (default: this swarm's)")
    parser.add_argument('--role', default='agent', help='which role to authenticate as (default: agent)')
    parser.add_argument('--yes', action='store_true', help='actually run it (default is a dry run)')
    args = parser.parse_args(argv)

    if args.concurrency < 1 or args.repeats < 1:
        sys.stderr.write('--concurrency and --repeats must be at least 1\n')
        return 2

    cost = cost_of_a_run(concurrency=args.concurrency, repeats=args.repeats)
    sys.stdout.write(f'{args.repo} at {args.concurrency}x, {args.repeats} repeats\n  would cost: {cost}\n')
    if not args.yes:
        sys.stdout.write('  DRY RUN. Re-run with --yes, after reading the line above.\n')
        return 0

    # IMPORTED HERE, not at module scope, and it is the one lazy import this file may have: `forge`
    # reaches the network and this module is otherwise pure arithmetic that a test imports freely.
    from agent_swarm.forge import default_forge  # noqa: PLC0415

    forge = default_forge(args.role, repo=args.repo, **({'base_url': args.base_url} if args.base_url else {}))
    prefix = f'[swarm] bench {int(time.time())}'

    writes = repeat(
        'create',
        lambda i: forge.create_work_item(title=f'{prefix}/w{i}', body='bench'),
        concurrency=args.concurrency,
        repeats=args.repeats,
    )
    sys.stdout.write('\nWRITE RATE -- cost per created item\n')
    for run in writes:
        sys.stdout.write(f'  {run.report()}\n')
    sys.stdout.write(f'  {spread(writes)}\n')

    # ONE item, contended, because that is what claim arbitration IS. Giving each worker its own
    # item would measure the write rate again under a different name.
    #
    # THE SETUP CALL IS GUARDED, and the first version of this was not -- caught by the INCONCLUSIVE
    # test below, which is the case it exists for. This `create` is OUTSIDE `measure`, so a forge
    # that refuses it raised straight out of `main` with a traceback. That is this file's own defect
    # class wearing a setup costume: a run that could not finish must report INCONCLUSIVE, and a
    # crash is not that -- it is a number nobody gets, from a tool whose entire job is to produce one
    # even when the answer is "the forge could not do this".
    try:
        contended = forge.create_work_item(title=f'{prefix}/contended', body='bench')
    except Exception as exc:  # noqa: BLE001 -- a forge that cannot seed the probe is a RESULT
        arbitration = Measurement(name='claim', concurrency=args.concurrency)
        sys.stdout.write(
            f'\nCLAIM ARBITRATION -- not measured: the item to contend for could not be created\n  {exc}\n'
        )
    else:
        arbitration = bench_claim(forge, contended, concurrency=args.concurrency)
        sys.stdout.write(f'\nCLAIM ARBITRATION -- {args.concurrency} runners, ONE item\n  {arbitration.report()}\n')

    reads = repeat(
        'list-open', lambda _i: forge.list_work_items(state='open'), concurrency=args.concurrency, repeats=args.repeats
    )
    sys.stdout.write('\nREAD COST -- what a sweep pays\n')
    for run in reads:
        sys.stdout.write(f'  {run.report()}\n')
    sys.stdout.write(f'  {spread(reads)}\n')

    sys.stdout.write(f'\nItems created under "{prefix}" were NOT removed. `swarmctl prune-issues` clears them.\n')
    inconclusive = [m for m in (*writes, arbitration, *reads) if not m.durations]
    if inconclusive:
        sys.stdout.write(f'INCONCLUSIVE: {len(inconclusive)} configuration(s) produced no successful operation\n')
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
