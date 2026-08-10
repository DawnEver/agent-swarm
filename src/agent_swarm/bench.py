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
"""

from __future__ import annotations

import statistics
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
