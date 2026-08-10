"""Work is agent work only when it has been HANDED OVER, and the handover can be taken back.

WHAT THIS ADDS THAT THE TITLE PREFIX DID NOT. `claimable` already ignores anything whose title is
not `[swarm] <namespace>/...`, so an issue a human writes was never at risk. The gap was the other
direction: **there was no way to stop a job already registered.** Closing the item is the only lever
that existed, and closing is also how work is ANSWERED -- so "I want this back" and "this is done"
were the same gesture, and a scheduler cannot tell them apart.

Removing one label is now that lever: no CLI, no credential, no race with a runner. The next
`claimable` simply stops offering it, and anything already claimed finishes or times out normally.

WHY DEFAULT-DENY RATHER THAN A `swarm:hold` LABEL. Both express the same wish; they differ in who
pays for a lapse of memory. Absent-by-default charges forgetting to the item that sits still.
Present-by-default charges it to a human who meant to keep an item, did not say so, and now has an
agent editing the same files. Forgetting is the normal case, so the default belongs on the cheap
side. Two labels with opposite meanings would also be a state machine nobody maintains.

WHY STORE-CREATED ITEMS ARE LABELLED AT BIRTH. The swarm creating work for itself must not need a
human in the loop -- that would make the CI transport depend on somebody clicking. The asymmetry IS
the design: machine-made work is handed over automatically, human-made work is handed over by
adding one label.
"""

from __future__ import annotations

import pytest

from agent_swarm.forge_store import READY_LABEL, ForgeStore, Role
from agent_swarm.job import TEST_RUN, Job
from agent_swarm.testing import RecordingForge


@pytest.fixture
def forge():
    return RecordingForge()


@pytest.fixture
def store(forge):
    return ForgeStore('ns', forge, role=Role.SUBMITTER)


def _register(store, ident: str = 'group/x'):
    job = Job(id=ident, kind=TEST_RUN)
    store.register(job)
    return job


def _numbers(store, forge):
    return {item.title: item.number for item in forge.list_work_items()}


# --------------------------------------------------------------------------- handed over at birth


def test_a_registered_item_is_claimable_without_anyone_labelling_it(store):
    """The CI loop has no human in it. If registration did not hand over, nothing would ever run."""
    job = _register(store)
    assert job in store.claimable(TEST_RUN).jobs


def test_registration_actually_applies_the_label(store, forge):
    """Asserting only that it is claimable would pass if the check were simply absent."""
    _register(store)
    number = next(iter(_numbers(store, forge).values()))
    assert READY_LABEL in forge.labels(number)


# --------------------------------------------------------------------------- taking it back


def test_removing_the_label_takes_the_work_back(store, forge):
    """THE NEW LEVER. Before this, the only way to stop a registered job was to close its item --
    which is also how a job is ANSWERED, so a scheduler could not tell "I want this back" from
    "this is done"."""
    job = _register(store)
    number = next(iter(_numbers(store, forge).values()))
    forge.remove_label(number, READY_LABEL)
    assert job not in store.claimable(TEST_RUN).jobs


def test_the_item_stays_OPEN_when_taken_back(store, forge):
    """Withdrawal must not look like completion. An item pulled back is still outstanding work, and
    closing it would publish a conclusion nobody reached."""
    _register(store)
    number = next(iter(_numbers(store, forge).values()))
    forge.remove_label(number, READY_LABEL)
    store.claimable(TEST_RUN)  # AFTER the sweep, not before: a sweep that "tidied up" what it
    # skipped would be invisible to a check taken beforehand -- measured, that mutation survived.
    assert forge.state(number) == 'open'


def test_handing_it_back_makes_it_claimable_again(store, forge):
    """Reversible in both directions, or it is a delete with extra steps."""
    job = _register(store)
    number = next(iter(_numbers(store, forge).values()))
    forge.remove_label(number, READY_LABEL)
    forge.add_label(number, READY_LABEL)
    assert job in store.claimable(TEST_RUN).jobs


# --------------------------------------------------------------------------- default deny


def test_an_item_this_store_did_not_hand_over_is_not_work(store, forge):
    """A title in the swarm grammar is NOT sufficient. Someone could copy one by hand, or an older
    build could have created it before handover existed; neither is a licence to run it."""
    number = forge.create_work_item(title='[swarm] ns/test:group/handmade', body='`x`')
    assert forge.state(number) == 'open'
    assert not store.claimable(TEST_RUN).jobs


def test_a_verdict_label_still_wins_over_a_ready_label(store, forge):
    """Two reasons to skip, and the ANSWERED one must not be overridable by re-handing-over: that
    would re-run a job whose conclusion is already published."""
    from agent_swarm.forge_store import VERDICT_LABELS

    job = _register(store)
    number = next(iter(_numbers(store, forge).values()))
    forge.add_label(number, VERDICT_LABELS['PASS'])
    assert job not in store.claimable(TEST_RUN).jobs


# --------------------------------------------------------------------------- one observation


def test_labels_are_read_once_per_item(store, forge, monkeypatch):
    """Two fetches would be two observations of a mutable thing: an item whose label is removed
    between them reads as claimable on one line and withdrawn on the next, and which one wins is a
    race nobody chose."""
    _register(store)
    calls: list[int] = []
    original = forge.labels
    monkeypatch.setattr(forge, 'labels', lambda n: (calls.append(n), original(n))[1])
    store.claimable(TEST_RUN)
    assert len(calls) == len(set(calls)) == 1
