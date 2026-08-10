"""A sweep must not get slower every day the system works correctly.

THE COST THIS PINS. A closed work item is never deleted: this Gitea deployment's API refuses it
(recorded at `GiteaForge.retire_work_item`) and GitHub has no endpoint for it at all. So the pile of
closed items only grows, and `list_work_items()` with the default `state='all'` pages through every
job the swarm has ever run -- on every sweep, on every runner. At a hundred agents that is the
dominant read, and it degrades in proportion to how much work has SUCCEEDED.

WHY A TEST AND NOT A COMMENT. This is exactly the kind of property that is true when written and
quietly false a month later: someone adds a sweep, reaches for the obvious call, and nothing goes
red -- the system just gets slower. Nobody bisects a slope.

WHAT IS ASSERTED IS THE VENDOR REQUEST, not a wall-clock time. A timing here would be a measurement
of this box under whatever else it is doing; what actually decides the cost is how many items the
forge was asked to send, and that is exact, reproducible, and the thing that changes if someone
regresses it.

THE DEFAULT STAYS `'all'` DELIBERATELY, and this file pins that too: a caller who needs closed items
and forgets to ask would silently conclude "no such item" and create a duplicate. The expensive
direction is slow; the cheap direction is WRONG.
"""

from __future__ import annotations

import pytest

from agent_swarm.forge_store import ForgeStore, Role
from agent_swarm.job import TEST_RUN, Job
from agent_swarm.testing import RecordingForge


class _CountingForge(RecordingForge):
    """Records what each caller ASKED FOR, which is what decides the cost."""

    def __init__(self) -> None:
        super().__init__()
        self.list_calls: list[str] = []
        self.items_sent = 0

    def list_work_items(self, *, state: str = 'all'):
        self.list_calls.append(state)
        out = super().list_work_items(state=state)
        self.items_sent += len(out)
        return out


@pytest.fixture
def forge():
    return _CountingForge()


@pytest.fixture
def store(forge):
    return ForgeStore('ns', forge, role=Role.SUBMITTER)


def _bury(store, forge, n: int) -> None:
    """`n` finished jobs, exactly as a working system accumulates them."""
    for i in range(n):
        job = Job(id=f'group/done-{i}', kind=TEST_RUN)
        store.register(job)
        store.record_verdict(job, verdict='PASS', detail='done')


def test_a_claimable_sweep_does_not_page_through_finished_work(store, forge):
    """THE SCALE PROPERTY. With 50 jobs answered and one outstanding, the sweep must be charged for
    roughly the outstanding one -- not for the history."""
    _bury(store, forge, 50)
    live = Job(id='group/live', kind=TEST_RUN)
    store.register(live)

    forge.items_sent = 0
    assert live in store.claimable(TEST_RUN).jobs
    assert forge.items_sent <= 2, (
        f'the sweep was sent {forge.items_sent} items for one piece of outstanding work; '
        'it is paying for history, and the bill grows every time a job succeeds'
    )


def test_the_sweep_asks_the_vendor_to_filter(store, forge):
    """Filtering after the fact would transmit the whole pile and then discard it -- the bytes and
    the pagination are the cost, not the loop."""
    _bury(store, forge, 5)
    forge.list_calls.clear()
    store.claimable(TEST_RUN)
    assert forge.list_calls == ['open'], forge.list_calls


def test_the_cost_is_FLAT_in_the_number_of_finished_jobs(store, forge):
    """Two populations, one measurement each would prove nothing about the slope. This compares the
    same sweep at two history sizes: flat is the claim, so flat is what is asserted."""
    live = Job(id='group/live', kind=TEST_RUN)
    store.register(live)
    _bury(store, forge, 10)
    forge.items_sent = 0
    store.claimable(TEST_RUN)
    small = forge.items_sent

    _bury(store, forge, 90)
    forge.items_sent = 0
    store.claimable(TEST_RUN)
    assert forge.items_sent == small, (
        f'{small} items at 10 finished jobs, {forge.items_sent} at 100 -- the read scales with '
        'history, which is the defect this file exists to prevent'
    )


def test_the_title_lookup_still_sees_CLOSED_items(store, forge):
    """The discriminating half, and the reason `'all'` is still the default.

    A CORRECTION TO THIS TEST'S FIRST FORM, which asserted that re-registering an answered job
    returns its old item. It does not, and it should not: `record_verdict` closes the item, and a
    closed item cannot be claimed -- a retry that reused it would produce work no runner can take.
    The code was right; the assertion was an assumption I had not checked.

    What actually depends on seeing closed items is the title lookup underneath
    `reconcile_duplicates`: a duplicate that has since been retired must still be FOUND, or the
    sweep reports a clean tracker while the duplicate sits there. Switching THAT call to open-only
    is the mistake this pins.
    """
    job = Job(id='group/retried', kind=TEST_RUN)
    number = store.register(job)
    store.record_verdict(job, verdict='PASS', detail='done')
    assert forge.state(number) == 'closed'

    title = next(i.title for i in forge.list_work_items() if i.number == number)
    assert store._lowest_numbered(title) == number, (
        'a closed item is invisible to the title lookup; a retired duplicate would answer no '
        'lookup, and the reconcile sweep would report a tracker it cannot actually see'
    )


def test_the_default_is_still_everything(forge):
    """A caller that forgets to ask must get the safe answer, not the cheap one."""
    number = forge.create_work_item(title='[swarm] ns/x', body='b')
    forge.close_work_item(number)
    assert [i.number for i in forge.list_work_items()] == [number]
    assert forge.list_work_items(state='open') == []


# ---------------------------------------------------------------------------
# The VENDOR REQUEST. Everything above runs against a double, and two mutations
# survived it: the Gitea query ignoring `state`, and the default flipped to
# 'open'. A cost control that the double honours and the real client drops is a
# saving that exists only in the test suite.


def _gitea_queries(state: str | None = None) -> list[str]:
    """Every API path a real `GiteaForge` would request, with `_api` stubbed at the boundary."""
    from agent_swarm.forge import GiteaForge

    forge = GiteaForge('http://forge.test:9000', 'owner/repo')
    seen: list[str] = []

    def fake_api(method: str, path: str, body=None):
        seen.append(path)
        return []

    forge._api = fake_api  # type: ignore[method-assign]
    forge.list_work_items() if state is None else forge.list_work_items(state=state)
    return seen


def test_the_gitea_client_actually_sends_the_state_it_was_given():
    """The request string, not the double's filter. Asked for open, it must ASK for open."""
    assert any('state=open' in path for path in _gitea_queries('open')), _gitea_queries('open')


def test_the_gitea_client_defaults_to_everything():
    """A default of `open` would make the title lookup silently miss retired duplicates -- the
    cheap direction is the WRONG one, so it must not be the default."""
    assert any('state=all' in path for path in _gitea_queries()), _gitea_queries()


def test_the_two_states_produce_DIFFERENT_requests():
    """Guards the shape both mutations had in common: a parameter that is accepted and discarded."""
    assert _gitea_queries('open') != _gitea_queries('all')
