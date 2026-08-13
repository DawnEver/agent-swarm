"""The integrator is a KIND the allocator schedules, not a box somebody nominated.

THE DECISION THIS FILE PINS. The integrator runs on ANY IDLE CAPABLE MACHINE. There is no dedicated
integration host, and consequently no parallel scheduling path: an integration pass is an ordinary
`Job`, ranked by `allocator.rank`, claimed by `store.try_claim`, run by `loop.run_one`. The bar
`JobKind` sets for a new member is that it costs this package nothing but an enum member, and
`test_the_allocator_needs_no_case_for_this_kind` is what holds that to it.

AND THE EXCLUSIVITY DECISION, WHICH IS THE PART WORTH READING. There is deliberately NO lock. The
claim gives at most one integrator per batch and that is BEST-EFFORT; correctness comes from
`advance`'s compare-and-swap, which is unconditional.
`test_a_lost_race_costs_a_run_and_corrupts_nothing` is the discriminating assertion: it lets two
integrators race the SAME batch on purpose and asserts the trunk is exactly one of the two judged
trees, never a mixture and never an unjudged one.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from agent_swarm import loop, refs
from agent_swarm.integration import (
    INTEGRATED,
    INTEGRATOR_CAPABILITY,
    OUTCOME_FILENAME,
    REJECTED,
    Integrator,
    NotAnIntegrator,
    batch_key,
    disposed_ordinals,
    integration_job,
    integrator_blockers,
    open_ordinals,
    trunk_commit,
)
from agent_swarm.job import COMPUTE, INTEGRATION, TEST_RUN, Job, JobKind
from agent_swarm import refstore
from agent_swarm.refstore import GitRefStore
from agent_swarm.shards import FAIL, INCONCLUSIVE, PASS
from agent_swarm.store import InMemoryStore
from agent_swarm.submission import Submission, publish

TRUNK = 'trunk'
CAPABLE = frozenset({INTEGRATOR_CAPABILITY})


def _never() -> bool:
    return False


def _git(root: Path, *args: str) -> str:
    out = subprocess.run(['git', '-C', str(root), *args], capture_output=True, text=True, check=True, timeout=60)
    return out.stdout.strip()


@pytest.fixture
def store(tmp_path: Path) -> GitRefStore:
    bare = tmp_path / 'remote.git'
    subprocess.run(['git', 'init', '--bare', '-q', str(bare)], check=True, timeout=60)
    work = tmp_path / 'work'
    subprocess.run(['git', 'init', '-q', '-b', TRUNK, str(work)], check=True, timeout=60)
    _git(work, 'config', 'user.email', 'x@example.com')
    _git(work, 'config', 'user.name', 'x')
    (work / 'shared.txt').write_text('base\n', encoding='utf-8')
    _git(work, 'add', '-A')
    _git(work, 'commit', '-qm', 'trunk base')
    _git(work, 'remote', 'add', 'upstream', str(bare))
    return GitRefStore(work, 'upstream', withhold_writes=_never, identity=refstore.ambient_identity)


def _submit(store: GitRefStore, ordinal: int, name: str, files: dict[str, str]) -> Submission:
    """A real branch with real commits, published at `ordinal`."""
    root = store.root
    base = _git(root, 'rev-parse', TRUNK)
    _git(root, 'checkout', '-q', '-b', name, TRUNK)
    for path, text in files.items():
        (root / path).write_text(text, encoding='utf-8')
    _git(root, 'add', '-A')
    _git(root, 'commit', '-qm', f'work on {name}')
    head = _git(root, 'rev-parse', 'HEAD')
    _git(root, 'checkout', '-q', TRUNK)
    sub = Submission(
        ordinal=ordinal,
        participant=name,
        base=base,
        head=head,
        intent=f'{name} does its half',
        declared_paths=tuple(files),
    )
    publish(store, sub)
    return sub


def _disposition(store: GitRefStore, ordinal: int) -> str:
    """Read a submission's recorded disposition back off the remote, the way a reader would."""
    ref = refs.outcome_ref(ordinal)
    store.run('fetch', store.remote, ref, timeout=60)
    return json.loads(store.text('cat-file', '-p', f'FETCH_HEAD:{OUTCOME_FILENAME}'))['disposition']


def _integrator(store: GitRefStore, tmp_path: Path, verdict: str | object, **kwargs) -> Integrator:
    """An integrator whose verdict function returns `verdict`, or calls it with the tree sha."""
    called = verdict if callable(verdict) else (lambda _tree: verdict)
    return Integrator(
        store=store,
        trunk=TRUNK,
        verdict_of=called,
        workdir=tmp_path / f'merge-{id(called)}',
        capabilities=CAPABLE,
        **kwargs,
    )


# --------------------------------------------------------------------------- the kind


class TestTheKindCostsOneEnumMember:
    def test_the_kind_exists_and_spells_itself_in_the_claim_key(self):
        assert JobKind.INTEGRATION is INTEGRATION
        job = integration_job(TRUNK, [1, 2])
        assert job.claim_key() == f'integration/{job.id}'
        assert job.id.startswith(f'{TRUNK}@')

    def test_the_allocator_needs_no_case_for_this_kind(self):
        """THE BAR `JobKind` SETS, asserted rather than asserted-about. An integration job is ranked
        beside the other kinds by the ordinary rules, with nothing in `allocator` reading `kind`."""
        from agent_swarm.allocator import Candidate, rank

        box = loop.Box(available_gib=64.0)
        integration = integration_job(TRUNK, [1], ram_gib=1.0, exclusivity='CHEAP')
        other = Job(id='t', kind=TEST_RUN, ram_gib=1.0, exclusivity='CHEAP')
        ordered = rank([Candidate(job=other, priority=1), Candidate(job=integration, priority=9)], box, now=0.0)
        assert ordered[0] is integration, 'the integration job did not win on priority alone'
        assert len(ordered) == 2

    def test_a_box_with_no_capacity_refuses_it_through_the_ordinary_door(self):
        job = integration_job(TRUNK, [1], ram_gib=200.0)
        assert loop.Box(available_gib=1.0).blockers(job), 'admission did not price an integration job'


class TestABoxThatCannotServeItSaysSo:
    def test_an_undeclared_box_is_refused_AT_CONSTRUCTION(self, store, tmp_path):
        """BEFORE ANY CLAIM. Refusing inside `execute` would be swallowed by `loop._run`'s executor
        handler into an INCONCLUSIVE, so a permanently incapable box would take the job, say nothing
        actionable, and take it again next tick -- silently taking nothing, which is the shape the
        brief for this work names."""
        with pytest.raises(NotAnIntegrator) as caught:
            Integrator(store=store, trunk=TRUNK, verdict_of=lambda _t: PASS, workdir=tmp_path, capabilities=frozenset())
        assert INTEGRATOR_CAPABILITY in str(caught.value)

    def test_the_refusal_is_askable_without_constructing_anything(self):
        assert integrator_blockers(CAPABLE) == []
        assert len(integrator_blockers({'femm-shaped-token'})) == 1
        assert 'nothing' in integrator_blockers(set())[0], 'an empty declaration is not named as empty'

    def test_it_refuses_a_job_for_a_DIFFERENT_TRUNK(self, store, tmp_path):
        """Guessing here would push a judged tree onto the wrong branch."""
        integrator = _integrator(store, tmp_path, PASS)
        with pytest.raises(ValueError, match='advances one trunk'):
            integrator.execute(integration_job('some-other-trunk', [1]))

    def test_it_refuses_a_job_of_another_KIND(self, store, tmp_path):
        with pytest.raises(ValueError, match='not an integration job'):
            _integrator(store, tmp_path, PASS).execute(Job(id='x', kind=COMPUTE))


# --------------------------------------------------------------------------- the batch identity


class TestTheJobIdIsTheBATCHAndNotTheTrunk:
    def test_two_different_batches_are_two_different_jobs(self):
        """AN ID OF `main` WOULD STALL THE PLANE SILENTLY: `loop.run_one` will not re-run a job that
        already has a verdict, so the first pass would be the last one ever scheduled."""
        assert batch_key(TRUNK, [1, 2]) != batch_key(TRUNK, [1, 2, 3])
        assert batch_key(TRUNK, [1]) != batch_key('other', [1])

    def test_two_boxes_listing_the_queue_differently_ORDERED_compute_ONE_id(self):
        """Otherwise two boxes would contend for nothing and both do the work."""
        assert batch_key(TRUNK, [3, 1, 2]) == batch_key(TRUNK, [1, 2, 3]) == batch_key(TRUNK, [1, 2, 3, 3])

    def test_an_empty_batch_is_not_a_pass(self):
        with pytest.raises(ValueError, match='no open submission'):
            batch_key(TRUNK, [])
        with pytest.raises(ValueError):
            integration_job(TRUNK, [])

    def test_a_queue_that_moved_between_scheduling_and_running_is_INCONCLUSIVE(self, store, tmp_path):
        """NOT "integrate whatever is open now". That would record a verdict under an id describing
        different work -- a declaration that lies, with a content address on it."""
        _submit(store, 1, 'a', {'a.txt': 'a\n'})
        stale = integration_job(TRUNK, [1])
        _submit(store, 2, 'b', {'b.txt': 'b\n'})

        verdict, detail = _integrator(store, tmp_path, PASS).execute(stale)

        assert verdict == INCONCLUSIVE
        assert 'the queue moved' in detail
        assert trunk_commit(store, TRUNK) == store.text('rev-parse', TRUNK), 'the trunk was touched anyway'
        assert open_ordinals(store) == (1, 2), 'a submission was disposed of by a pass that did not run'


# --------------------------------------------------------------------------- one pass, really run


class TestOnePassEndToEnd:
    def test_a_PASS_advances_the_trunk_to_the_tree_that_was_judged(self, store, tmp_path):
        before = trunk_commit(store, TRUNK)
        _submit(store, 1, 'a', {'a.txt': 'a\n'})
        _submit(store, 2, 'b', {'b.txt': 'b\n'})
        judged: list[str] = []

        verdict, detail = _integrator(store, tmp_path, lambda tree: judged.append(tree) or PASS).execute(
            integration_job(TRUNK, [1, 2])
        )

        assert verdict == PASS, detail
        after = trunk_commit(store, TRUNK)
        assert after != before
        assert judged == [store.text('rev-parse', f'{after}^{{tree}}')], 'the trunk carries a tree nobody judged'
        assert open_ordinals(store) == (), 'the batch was not disposed of'
        assert '1, 2' in detail

    def test_a_FAIL_disposes_of_the_batch_and_leaves_the_trunk_alone(self, store, tmp_path):
        before = trunk_commit(store, TRUNK)
        _submit(store, 1, 'a', {'a.txt': 'a\n'})

        verdict, detail = _integrator(store, tmp_path, FAIL).execute(integration_job(TRUNK, [1]))

        assert verdict == FAIL, detail
        assert trunk_commit(store, TRUNK) == before
        assert open_ordinals(store) == (), 'a rejected submission stayed open and would be re-judged forever'

    def test_an_INCONCLUSIVE_run_leaves_the_batch_OPEN_and_the_id_UNCHANGED(self, store, tmp_path):
        """THE ONE CASE WHERE RETRYING THE IDENTICAL WORK IS CORRECT, and it falls out of the id
        rather than out of a retry rule written anywhere."""
        sub = _submit(store, 1, 'a', {'a.txt': 'a\n'})
        job = integration_job(TRUNK, [sub.ordinal])

        verdict, _ = _integrator(store, tmp_path, INCONCLUSIVE).execute(job)

        assert verdict == INCONCLUSIVE
        assert open_ordinals(store) == (1,)
        assert integration_job(TRUNK, open_ordinals(store)).id == job.id, 'the retry would be a different job'

    def test_it_runs_through_the_ORDINARY_loop(self, store, tmp_path):
        """No integration-specific entry point exists, and this is the proof: `run_one` admits,
        claims, executes, records and releases an integration pass exactly as it does a test run."""
        _submit(store, 1, 'a', {'a.txt': 'a\n'})
        job = integration_job(TRUNK, [1], ram_gib=1.0, exclusivity='CHEAP')
        memory = InMemoryStore()

        outcome = loop.run_one(
            job,
            executor=_integrator(store, tmp_path, PASS),
            store=memory,
            owner='box-a',
            box=loop.Box(available_gib=64.0),
        )

        assert outcome is loop.Outcome.ANSWERED
        assert memory.verdict(job) == PASS
        assert memory.claim_owner(job) is None, 'the claim outlived the pass'


# --------------------------------------------------------------------------- the exclusivity decision


class TestAtMostOneIntegratorPerTrunkAtATime:
    def test_the_claim_alone_gives_the_exclusion_with_no_second_lock(self, store, tmp_path):
        """The mechanism is `store.try_claim`, which already exists and is already a compare-and-swap.
        Nothing in `integration.py` locks anything, and `test_nothing_in_this_plane_holds_a_lock`
        below is what keeps that true."""
        _submit(store, 1, 'a', {'a.txt': 'a\n'})
        job = integration_job(TRUNK, [1])
        memory = InMemoryStore()

        assert memory.try_claim(job, owner='box-a') is True
        assert memory.try_claim(job, owner='box-b') is False, 'two integrators held one batch'

    def test_two_boxes_with_the_same_view_of_the_queue_contend(self, store, tmp_path):
        """The digest is the exclusion. Two boxes that see the same open set compute one claim key."""
        _submit(store, 1, 'a', {'a.txt': 'a\n'})
        _submit(store, 2, 'b', {'b.txt': 'b\n'})
        seen_by_a = integration_job(TRUNK, open_ordinals(store))
        seen_by_b = integration_job(TRUNK, reversed(open_ordinals(store)))
        assert seen_by_a.claim_key() == seen_by_b.claim_key()

    def test_a_lost_race_costs_a_run_and_corrupts_nothing(self, store, tmp_path):
        """**THE DISCRIMINATING ASSERTION FOR THE NO-LOCK DECISION.** Two integrators are pointed at
        the same batch with no claim between them at all -- the worst case a missing lock allows.

        The second one's `advance` finds the trunk moved, so it lands NOTHING and answers
        INCONCLUSIVE. The cost is one wasted verdict; the trunk carries a tree that really was judged.
        A lock would prevent the waste and would serialise the plane whose concurrency is the entire
        reason the fan-out exists, which is the wrong trade against a failure that corrupts nothing.
        """
        _submit(store, 1, 'a', {'a.txt': 'a\n'})
        job = integration_job(TRUNK, [1])
        judged: list[str] = []

        def watching(tree: str) -> str:
            judged.append(tree)
            return PASS

        first = _integrator(store, tmp_path / 'one', watching).execute(job)
        second = _integrator(store, tmp_path / 'two', watching).execute(job)

        assert first[0] == PASS
        # The second box re-reads the queue first, so it discovers the batch is gone before it spends
        # a verdict at all -- cheaper than the race it is allowed to lose, and still not a lock.
        assert second[0] == INCONCLUSIVE, second[1]
        assert len(judged) == 1
        assert store.text('rev-parse', f'{TRUNK}^{{tree}}') == judged[0], 'the trunk carries an unjudged tree'

    def test_a_TRUNK_THAT_MOVED_UNDER_A_PASS_is_INCONCLUSIVE_and_not_a_crash(self, store, tmp_path):
        """`TrunkMoved` is an ordinary race with a specific answer. Letting it reach `loop._run`'s
        broad handler would record the same word while reading, to anyone looking, as a box that
        fell over -- and "the fleet is politely racing" must not look like "the fleet is breaking"."""
        _submit(store, 1, 'a', {'a.txt': 'a\n'})

        def moves_the_trunk_while_judging(tree: str) -> str:
            _git(store.root, 'commit', '-q', '--allow-empty', '-m', 'somebody else landed first')
            return PASS

        verdict, detail = _integrator(store, tmp_path, moves_the_trunk_while_judging).execute(
            integration_job(TRUNK, [1])
        )

        assert verdict == INCONCLUSIVE
        assert 'advanced' in detail and TRUNK in detail
        assert open_ordinals(store) == (1,), 'a submission was closed by a pass that landed nothing'

    def test_nothing_in_this_plane_holds_a_lock(self):
        """THE CONTROL FOR THE DECISION, and it is a source assertion because the property is an
        ABSENCE. A future editor reaching for a lock to close the duplicate-work gap trips this and
        has to read the reasoning in `Integrator`'s docstring first."""
        source = Path(__import__('agent_swarm.integration', fromlist=['x']).__file__).read_text(encoding='utf-8')
        for forbidden in ('threading.Lock', 'filelock', 'flock', 'msvcrt.locking'):
            assert forbidden not in source, f'a lock ({forbidden}) appeared beside a correct CAS'
        assert 'NOT CORRECTNESS' in source, 'the reason there is no lock is no longer stated where it is decided'


class TestTheDispositionsAreTheEXISTINGOnes:
    def test_a_pass_records_INTEGRATED_and_a_fail_records_REJECTED(self, store, tmp_path):
        """The runner introduces no fourth disposition; it drives the ones the plane already has."""
        _submit(store, 1, 'a', {'a.txt': 'a\n'})
        _integrator(store, tmp_path / 'p', PASS).execute(integration_job(TRUNK, [1]))
        _submit(store, 2, 'b', {'b.txt': 'b\n'})
        _integrator(store, tmp_path / 'f', FAIL).execute(integration_job(TRUNK, [2]))

        assert disposed_ordinals(store) == {1, 2}
        assert _disposition(store, 1) == INTEGRATED
        assert _disposition(store, 2) == REJECTED
        assert open_ordinals(store) == (), 'both passes should have disposed of their batch'
