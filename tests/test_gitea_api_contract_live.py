"""The three API shapes every round-trip optimisation rests on, checked against a REAL Gitea.

WHY THIS FILE EXISTS. Three reductions were made from the API docs and verified only against a
double: registration folds the label into the create (2 calls -> 1), the verdict folds the label
into the close (4 -> 3), and the sweep reads labels from the listing (N+1 -> 1). A double agrees
with whatever it was written to agree with, so all three are ASSUMPTIONS until a server answers.

AND ONE OF THEM FAILS CATASTROPHICALLY AND SILENTLY IF WRONG. If `GET /issues` does not carry
labels inline, every item comes back with an empty label tuple, `claimable` finds nothing handed
over, and **the whole swarm quietly does nothing** -- no error, no red test, a fleet that looks
healthy and idle. That is the exact shape this repo calls a declaration that lies, and it is why
the assertion below is on a label that was definitely applied rather than on the field existing.

RUN IT THE MOMENT CREDENTIALS EXIST:

    pytest -m live_forge tests/test_gitea_api_contract_live.py

It creates items in a throwaway namespace and retires them, so it is safe against the real
deployment. Deselected by default like every live test -- see conftest.
"""

from __future__ import annotations

import os
import secrets

import pytest

from agent_swarm.forge import DEFAULT_GITEA_BASE_URL, GiteaForge
from agent_swarm.forge_store import READY_LABEL, VERDICT_LABELS, ForgeStore, Role
from agent_swarm.job import TEST_RUN, Job


@pytest.fixture
def live():
    """A real client against a throwaway namespace, cleaned up afterwards."""
    repo = os.environ.get('SWARM_REPO') or 'Tianjie-Zou-Team/motronics-studio'
    forge = GiteaForge(os.environ.get('SWARM_BASE_URL') or DEFAULT_GITEA_BASE_URL, repo, username='swarm-agent')
    namespace = f'contract-{secrets.token_hex(3)}'
    store = ForgeStore(namespace, forge, role=Role.SUBMITTER)
    yield forge, store, namespace
    store.purge_namespace()


@pytest.mark.live_forge
def test_the_listing_carries_labels_inline(live):
    """THE ONE THAT FAILS SILENTLY. Asserted via a label we KNOW was applied, not via the field
    being present: an empty list and a missing field are the same catastrophe, and a schema check
    would pass on a server that returns `labels: []` for everything."""
    forge, store, namespace = live
    job = Job(id='group/listing', kind=TEST_RUN)
    number = store.register(job)

    listed = [i for i in forge.list_work_items(state='open') if i.number == number]
    assert listed, 'the item this test just created is not in the listing'
    assert READY_LABEL in listed[0].labels, (
        'the listing does not carry labels inline. `claimable` would find NOTHING handed over and '
        'the swarm would quietly do nothing -- revert the sweep to a per-item fetch.'
    )


@pytest.mark.live_forge
def test_create_applies_labels_in_the_same_call(live):
    """Registration's 2 -> 1. If the create ignores `labels`, items arrive unclaimable."""
    forge, store, _ = live
    number = store.register(Job(id='group/create', kind=TEST_RUN))
    assert READY_LABEL in forge.labels(number)


@pytest.mark.live_forge
def test_close_replaces_labels_in_the_same_call(live):
    """The verdict's 4 -> 3, and BOTH halves of the replacement semantics: the verdict label must
    arrive, and the handover label must survive. A server whose `labels` on PATCH appended instead
    of replacing would accumulate verdict labels across retries."""
    forge, store, _ = live
    job = Job(id='group/close', kind=TEST_RUN)
    number = store.register(job)
    store.record_verdict(job, verdict='PASS', detail='contract probe')

    after = set(forge.labels(number))
    assert VERDICT_LABELS['PASS'] in after, 'the verdict label did not arrive with the close'
    assert READY_LABEL in after, 'the close stripped a label it was told to keep'
    assert forge.state(number) == 'closed', 'the state did not change in the same call'


@pytest.mark.live_forge
def test_a_sweep_really_is_one_request(live):
    """The cost claim itself, against the server. Counted at the `_api` boundary, which is where a
    round trip actually is -- an assertion on `list_work_items` alone would miss a per-item fetch
    reintroduced anywhere below it."""
    forge, store, _ = live
    for i in range(3):
        store.register(Job(id=f'group/sweep-{i}', kind=TEST_RUN))

    calls: list[str] = []
    original = forge._api
    forge._api = lambda method, path, body=None: (calls.append(path), original(method, path, body))[1]
    ForgeStore(store.namespace, forge, role=Role.RUNNER).claimable(TEST_RUN)
    assert len(calls) == 1, f'the sweep made {len(calls)} requests for 3 open items: {calls}'


# ---------------------------------------------------------------------------
# A live test that only runs live ROTS. This one runs always.


def test_every_name_the_live_tests_use_actually_exists():
    """CAUGHT A REAL DEFECT THE MOMENT IT WAS WRITTEN: the cleanup called `store.purge()`, which
    does not exist -- the method is `purge_namespace`. An AttributeError in a fixture teardown that
    only executes against a real server would have surfaced on the ONE run that mattered, in the
    middle of onboarding, and read as "the API contract check is broken" rather than as a typo.

    Collection catches import errors; it does not catch a name reached at runtime. So the names are
    asserted here, in a test that runs on every ordinary suite, against the real classes.
    """
    from agent_swarm.forge import GiteaForge
    from agent_swarm.forge_store import ForgeStore

    for name in ('purge_namespace', 'register', 'record_verdict', 'claimable'):
        assert hasattr(ForgeStore, name), f'ForgeStore has no {name!r}; the live contract test would die'
    for name in ('list_work_items', 'labels', 'state', '_api'):
        assert hasattr(GiteaForge, name), f'GiteaForge has no {name!r}; the live contract test would die'


def test_the_live_tests_are_marked_so_they_do_not_run_by_accident():
    """They CREATE ITEMS on the real deployment. An unmarked one would fire in every ordinary run
    and leave debris in the tracker -- which is how two stray probe issues got there before."""
    import inspect

    module = inspect.getmodule(test_the_listing_carries_labels_inline)
    live = [n for n, f in vars(module).items() if n.startswith('test_') and hasattr(f, 'pytestmark')]
    for name in (
        'test_the_listing_carries_labels_inline',
        'test_create_applies_labels_in_the_same_call',
        'test_close_replaces_labels_in_the_same_call',
        'test_a_sweep_really_is_one_request',
    ):
        assert name in live, f'{name} would run against the real forge in every ordinary suite'
