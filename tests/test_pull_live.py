"""The pull surface and its CLI, against a REAL forge, plus the per-call latency floor.

TWO GAPS THIS CLOSES, both named in reports before they were closed:

* **THE CLI HAD NO LIVE COVERAGE AT ALL.** Every CLI test injects a `Workbench` over
  `RecordingForge`, which is right for the parser, the dispatch and the exit codes and says nothing
  about whether `take` works against a server. A double is read-after-write fresh by construction;
  the whole claim protocol depends on a property only a deployment can have.
* **THE PER-CALL FLOOR WAS A PROJECTION.** `pull.available`'s cost argument scales ~60 ms p50 --
  a figure measured on 2026-08-10 for a DIFFERENT operation -- up to "~30 s at 500 items". Since
  this tier pays for real calls anyway, the floor is measurable here for free, and the projection
  either firms up or moves.

    pytest -m live_forge tests/test_pull_live.py

Throwaway namespace, purged in teardown so it runs on a FAILING test too.
"""

from __future__ import annotations

import os
import secrets
import statistics
import sys
import time

import pytest

from agent_swarm.forge import DEFAULT_GITEA_BASE_URL, GiteaForge
from agent_swarm.forge_store import ForgeStore, Role
from agent_swarm.job import TEST_RUN, Job
from agent_swarm.pull import Workbench
from agent_swarm.workbench_cli import Exit, main

#: How many single calls the latency probe times. Twenty is enough for a p50 to mean something and
#: small enough that the probe is a rounding error against the ~180 calls this tier already makes.
LATENCY_SAMPLES = 20


@pytest.fixture
def live():
    """A real client and a runner/submitter pair in a throwaway namespace, purged afterwards.

    THE PURGE IS AFTER `yield`, so it runs when the test FAILS as well -- which is the run that most
    needs the tracker left clean, because it is the one somebody will come back to.
    """
    repo = os.environ.get('SWARM_REPO') or 'Tianjie-Zou-Team/motronics-studio'
    forge = GiteaForge(os.environ.get('SWARM_BASE_URL') or DEFAULT_GITEA_BASE_URL, repo, username='swarm-agent')
    namespace = f'pull-{secrets.token_hex(3)}'
    submitter = ForgeStore(namespace, forge, role=Role.SUBMITTER)
    yield forge, namespace, submitter
    submitter.purge_namespace()


@pytest.mark.live_forge
def test_the_CLI_takes_runs_and_reports_against_a_real_forge(live):
    """THE WHOLE LOOP THROUGH `main()`: claim, beat, run a child, record the verdict, release.

    Run through the CLI entry point rather than through `Workbench`, because the entry point is what
    a person types and it is the layer with no live coverage. The child is this interpreter exiting
    0, so what is under test is the forge round trip and not the command.
    """
    forge, namespace, submitter = live
    job = Job(id='live/cli', kind=TEST_RUN)
    submitter.register(job)

    bench = Workbench(
        ForgeStore(namespace, forge, role=Role.RUNNER, lease_seconds=300.0),
        owner=f'live-probe-{secrets.token_hex(2)}',
        capabilities=(),
    )
    code = main(['take', job.claim_key(), '--', sys.executable, '-c', 'pass'], workbench=bench)

    assert code == Exit.OK, f'the CLI exited {code!r} against the real forge'
    assert bench.store.verdict(job) == 'PASS'
    assert bench.store.claim_owner(job) is None, 'the claim was not given back after the verdict'


@pytest.mark.live_forge
def test_a_SECOND_taker_is_refused_by_the_real_server(live):
    """The refusal, live. The offline suite races this sixteen ways; what only a server can say is
    that its comment ids arbitrate the way the protocol assumes.
    """
    forge, namespace, submitter = live
    job = Job(id='live/contended', kind=TEST_RUN)
    submitter.register(job)
    runner = ForgeStore(namespace, forge, role=Role.RUNNER)
    assert runner.try_claim(job, owner='first') is True

    bench = Workbench(ForgeStore(namespace, forge, role=Role.RUNNER), owner='second', capabilities=())
    code = main(['take', job.claim_key(), '--', sys.executable, '-c', 'pass'], workbench=bench)

    assert code == Exit.LOST_THE_RACE
    assert runner.claim_owner(job) == 'first'


@pytest.mark.live_forge
def test_the_PER_CALL_FLOOR_is_measured_rather_than_projected(live, capsys):
    """MEASURE THE FLOOR the `available()` cost argument is scaled from.

    ONE UNCONTENDED CALL AT A TIME, deliberately: the floor is what a single sequential request
    costs, and anything concurrent measures contention instead. `comments()` is the call
    `available()`'s claimed-filter actually makes, so this is the same operation rather than a proxy
    for it -- the 2026-08-10 figure this replaces was measured on a different one.

    THE ASSERTION IS A GENEROUS CEILING, not the number: a tight bound here would fail on a busy
    box and teach everyone to rerun it, and a timing taken under someone else's load is not a
    property of the server. The NUMBER is what this test is for, and it is printed.
    """
    forge, namespace, submitter = live
    job = Job(id='live/latency', kind=TEST_RUN)
    number = submitter.register(job)

    samples: list[float] = []
    for _ in range(LATENCY_SAMPLES):
        started = time.perf_counter()
        forge.comments(number)
        samples.append((time.perf_counter() - started) * 1000.0)

    samples.sort()
    p50 = statistics.median(samples)
    p95 = samples[min(len(samples) - 1, int(0.95 * (len(samples) - 1)))]
    with capsys.disabled():
        print(
            f'\n[live] GET comments x{LATENCY_SAMPLES}, uncontended: '
            f'p50 {p50:.1f} ms, p95 {p95:.1f} ms, min {samples[0]:.1f}, max {samples[-1]:.1f}'
        )
        print(f'[live] available() N+1 projection at this floor: {p50 * 500 / 1000:.1f} s for 500 open items')
    assert p50 < 5000, f'a single comment read took {p50:.0f} ms at the median; something is very wrong'


@pytest.mark.live_forge
def test_the_real_server_assigns_comment_ids_the_way_the_double_does(live, capsys):
    """AUDIT THE DOUBLE AGAINST REALITY, which is the point of paying for this tier.

    `RecordingForge` hands out ids from a counter under a lock: unique, monotonic, gapless. The
    first two are what the protocol needs; **gaplessness is a property of the double that reality
    need not share**, since other repos on this deployment consume ids too. Asserting only the two
    that matter, and PRINTING the gap, is how a divergence gets recorded without being called a bug.
    """
    forge, namespace, submitter = live
    job = Job(id='live/ids', kind=TEST_RUN)
    number = submitter.register(job)

    posted = [forge.add_comment(number, f'probe {i}') for i in range(5)]
    with capsys.disabled():
        span = posted[-1] - posted[0]
        print(f'\n[live] comment ids for 5 posts: {posted} (span {span}, gapless would be 4)')

    assert len(set(posted)) == len(posted), f'the server REUSED a comment id: {posted}'
    assert posted == sorted(posted), f'ids were not monotonic: {posted}'
