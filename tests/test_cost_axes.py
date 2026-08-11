"""Components that exist for COST, tested on the axis they exist on.

THE CLASS THIS FILE ANSWERS. The index bug was not a weak suite -- every assertion about it passed,
and they were all about CORRECTNESS. The index exists to remove a network read; correctness tests
pass identically whether it removes one or is completely inert, so the suite was not failing to
check carefully, **it was pointed at a different axis**. Worse, the in-process dict beside it was a
real cache, so even a naive timing on one store object would have agreed.

So: ask what axis a component exists on, then ask whether any test measures THAT axis. For a cache,
a pool, an index, a batcher or a lock the answer is usually no, and the suite is green about it.

Four things in this package exist purely to avoid work:

    ForgeStore._item_numbers   in-process item cache      -> counted here
    ForgeStore.index           cross-process item cache   -> counted in test_item_index.py
    GiteaForge._label_ids      label id cache             -> counted here
    GiteaForge._token          credential cache           -> counted here

EVERY ASSERTION BELOW COUNTS CALLS. Not "is the result right" -- the result is right either way,
which is exactly how the index stayed broken through a green suite. And none of these counts can be
satisfied by the layer above: each is taken at the boundary the cache exists to protect.

WHY THE INSTRUMENTED SUBCLASSES ARE NOT MONKEYPATCHING THE CODE UNDER TEST: the code under test is
the caching logic, and its boundary is the call it is trying to avoid. Replacing that boundary is
what makes the avoidance observable. Replacing the cache would be the other thing, and is not done.
"""

from __future__ import annotations


import pytest

from agent_swarm.forge import GiteaForge
from agent_swarm.forge_store import ForgeStore, Role
from agent_swarm.job import TEST_RUN, Job
from test_forge_store import RecordingForge, StaleListForge

JOB = Job(id='cost', kind=TEST_RUN)


class CountingGitea(GiteaForge):
    """A `GiteaForge` whose HTTP boundary is counted and canned.

    The cache under test sits ABOVE `_api`; this replaces what it is trying to avoid, which is the
    only way to see whether it avoided it.
    """

    def __init__(self) -> None:
        super().__init__('http://127.0.0.1:1', 'o/r', username='swarm-agent')
        self.api_calls: list[str] = []
        self.credential_calls = 0

    def _api(self, method: str, path: str, body: dict | None = None):
        self.api_calls.append(f'{method} {path}')
        if path.endswith('/labels?limit=100'):
            return [{'id': 7, 'name': 'verdict:pass'}]
        if '/labels' in path:
            return {'id': 7}
        return {}

    def _resolve_token(self, scheme: str, netloc: str, username: str) -> str | None:
        self.credential_calls += 1
        return 'not-a-real-token'


class TestTheInProcessItemCacheActuallyCaches:
    """It exists to spare a `list_work_items` per lookup, and nothing measured that."""

    def test_a_repeated_lookup_costs_ZERO_extra_list_calls(self):
        forge = StaleListForge(RecordingForge(), staleness=0.0)
        store = ForgeStore('ns', forge, role=Role.SUBMITTER)
        store.register(JOB)

        store.work_item_number(JOB)
        before = forge.list_calls
        for _ in range(10):
            store.work_item_number(JOB)
        assert forge.list_calls == before, 'the in-process cache is inert; every lookup paid again'

    def test_a_SEPARATE_store_does_pay_which_is_why_the_index_exists(self):
        """The complement, and the reason the on-disk index is a different component: an in-process
        cache is empty in every new process, and a fleet is made of new processes.
        """
        forge = StaleListForge(RecordingForge(), staleness=0.0)
        ForgeStore('ns', forge, role=Role.SUBMITTER).register(JOB)

        fresh = ForgeStore('ns', forge, role=Role.RUNNER)
        before = forge.list_calls
        fresh.work_item_number(JOB)
        assert forge.list_calls > before, 'a cold store must actually look; otherwise this proves nothing'


class TestTheLabelIdCacheActuallyCaches:
    """Gitea addresses labels by id, so every attach would otherwise cost a `GET /labels` to
    translate a name. The cache exists for that and only that."""

    def test_the_label_list_is_fetched_ONCE_across_many_attaches(self):
        forge = CountingGitea()
        for number in range(5):
            forge.add_label(number, 'verdict:pass')
        lookups = [call for call in forge.api_calls if 'labels?limit=100' in call]
        assert len(lookups) == 1, f'the label id cache is inert: {len(lookups)} lookups for 5 attaches'

    def test_a_DIFFERENT_label_is_looked_up_separately(self):
        """A cache that returned one id for every name would also pass the test above -- and would
        attach the wrong label to every verdict.
        """
        forge = CountingGitea()
        forge.add_label(1, 'verdict:pass')
        forge.add_label(1, 'verdict:fail')
        assert len([c for c in forge.api_calls if 'labels?limit=100' in c]) == 2


class TestTheCredentialCacheActuallyCaches:
    """It exists to avoid one credential lookup per request. With the call inlined there was no way
    to see whether it did; the seam was extracted so this could count instead of hope.

    THE LOOKUP IS NO LONGER A SUBPROCESS -- role tokens moved out of the operator's git credential
    store on 2026-08-11 -- and the cost argument survives that unchanged, because it was never
    specifically about `fork`. It was about a repeated lookup on a 7x24 fleet, and a file read per
    API call is still a cost worth not paying.
    """

    def test_the_lookup_runs_ONCE_across_many_requests(self):
        forge = CountingGitea()
        for _ in range(5):
            forge._credential()
        assert forge.credential_calls == 1, f'looked up {forge.credential_calls} times for 5 requests'

    def test_the_seam_EXISTS_so_this_can_be_counted_at_all(self):
        """The generalisation, as an assertion. If a later refactor inlines the lookup again, the
        cache silently becomes unobservable -- and unobservable is where the index bug lived.
        """
        assert hasattr(GiteaForge, '_resolve_token')


@pytest.mark.parametrize('name', ['_item_numbers', '_label_ids', '_token'])
def test_the_named_cost_attributes_still_exist(name):
    """The list of what exists for COST, kept beside the tests that measure it.

    If one is renamed or removed, its cost test above silently measures nothing -- and a new cache
    added WITHOUT a cost test is the index bug again. The only thing that catches that is a reader
    noticing this list is shorter than the code, which is why the list lives here rather than in a
    memory file.
    """
    holders = [
        vars(ForgeStore('ns', RecordingForge(), role=Role.SUBMITTER)),
        vars(GiteaForge('http://127.0.0.1:1', 'o/r', username='swarm-agent')),
    ]
    assert any(name in holder for holder in holders), f'{name} is gone; its cost test now measures nothing'
