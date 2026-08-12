"""A Submission is the only unit that crosses into the trunk, and it is IMMUTABLE.

WHAT IS PINNED HERE, and why each of these needs a test rather than a docstring:

**The ordinal is allocated by a CAS, not by a read.** Two participants that both read "the highest
ordinal is 4" both write 5, and with a forcing write the second silently erases the first --
a submission that was accepted, acknowledged and then never seen again. The publish path therefore
pushes WITHOUT `--force`, which git refuses for an existing ref with unrelated history, and the
refusal is asserted here by planting the collision rather than by trusting the flag.

**Immutability is enforced by the transport, so the test uses the real one.** An in-memory double
that refused a second write would prove only that the double refuses; the property being claimed is
git's, so a real bare remote answers it.

**The declared intent may be WRONG, and that is legal.** A submission whose observed effects exceed
its declared paths is accepted -- scope is intent and routing, never a lock -- so the round trip is
asserted while nothing anywhere compares the declaration to the diff.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agent_swarm import refs
from agent_swarm.refstore import GitRefStore
from agent_swarm.submission import OrdinalTaken, Submission, create, publish, read, submitted_ordinals


def _never() -> bool:
    """The rehearsal predicate: these tests exercise the REAL writes against a scratch remote."""
    return False


def _git(root: Path, *args: str) -> str:
    out = subprocess.run(['git', '-C', str(root), *args], capture_output=True, text=True, check=True, timeout=60)
    return out.stdout.strip()


@pytest.fixture
def store(tmp_path: Path) -> GitRefStore:
    bare = tmp_path / 'remote.git'
    subprocess.run(['git', 'init', '--bare', '-q', str(bare)], check=True, timeout=60)
    work = tmp_path / 'work'
    subprocess.run(['git', 'init', '-q', str(work)], check=True, timeout=60)
    _git(work, 'config', 'user.email', 'x@example.com')
    _git(work, 'config', 'user.name', 'x')
    (work / 'a.txt').write_text('hello\n', encoding='utf-8')
    _git(work, 'add', '-A')
    _git(work, 'commit', '-qm', 'first')
    _git(work, 'remote', 'add', 'upstream', str(bare))
    return GitRefStore(work, 'upstream', withhold_writes=_never)


@pytest.fixture
def refusing_store(store: GitRefStore) -> GitRefStore:
    """The same store, against a remote that DECLINES every push.

    A real `pre-receive` hook rather than a patched-out function: what is being asserted is how this
    code behaves when the server says no, and a double that raised on command would prove only that
    the double raises. Reads still work, which is exactly the asymmetry a revoked write credential
    produces.
    """
    bare = store.root.parent / 'remote.git'
    hook = bare / 'hooks' / 'pre-receive'
    hook.write_text('#!/bin/sh\nexit 1\n', encoding='utf-8')
    hook.chmod(0o755)
    return store


def _submission(store: GitRefStore, ordinal: int = 1, **over) -> Submission:
    head = _git(store.root, 'rev-parse', 'HEAD')
    fields = {
        'ordinal': ordinal,
        'participant': 'boxA/session-3',
        'base': head,
        'head': head,
        'intent': 'make the thing work',
        'declared_paths': ('src/a.py',),
    }
    return Submission(**{**fields, **over})


# --------------------------------------------------------------------------- the object


def test_a_submission_is_frozen():
    """It is the record of what was PROPOSED. A field edited after publication would describe a
    proposal nobody ever made, while the ref still points at the original bytes."""
    sub = Submission(ordinal=1, participant='p', base='a' * 40, head='b' * 40, intent='x')
    with pytest.raises(Exception):  # noqa: B017 -- dataclass raises FrozenInstanceError
        sub.intent = 'y'  # type: ignore[misc]


def test_the_payload_round_trips_including_the_declared_paths():
    sub = Submission(
        ordinal=7, participant='p', base='a' * 40, head='b' * 40, intent='x', declared_paths=('src/a.py', 'src/b.py')
    )
    assert Submission.from_payload(sub.payload()) == sub


def test_declared_paths_survive_as_a_TUPLE_through_json():
    """JSON has one sequence type, so a naive reader hands back a list -- and a list is mutable and
    unhashable, which is how an immutable record acquires a mutable field nobody notices."""
    sub = Submission(ordinal=7, participant='p', base='a' * 40, head='b' * 40, intent='x', declared_paths=('src/a.py',))
    assert isinstance(Submission.from_payload(sub.payload()).declared_paths, tuple)


def test_an_ordinal_below_one_is_refused():
    """Ordinals are 1-based and their ref names are parsed back to `int`. A 0 or a negative would
    give an ordering with no first element and a ref name that no longer round trips."""
    with pytest.raises(ValueError, match='ordinal'):
        Submission(ordinal=0, participant='p', base='a' * 40, head='b' * 40, intent='x')


def test_the_ref_is_the_one_the_grammar_names():
    """The layout is `refs.py`'s, not a second convention invented beside it."""
    sub = Submission(ordinal=4, participant='p', base='a' * 40, head='b' * 40, intent='x')
    assert sub.ref() == refs.submission_ref(4)
    assert refs.submission_ordinal(sub.ref()) == 4


# --------------------------------------------------------------------------- publication


def test_a_published_submission_reads_back(store: GitRefStore):
    sub = _submission(store)
    publish(store, sub)
    assert read(store, 1) == sub


def test_publishing_the_same_ordinal_TWICE_is_refused(store: GitRefStore):
    """THE IMMUTABILITY CLAIM, asserted against real git rather than against a promise. The second
    publish carries DIFFERENT content, so a transport that accepted it would leave the first
    submission unreachable with nothing anywhere reporting the loss."""
    publish(store, _submission(store))
    with pytest.raises(OrdinalTaken):
        publish(store, _submission(store, intent='something else entirely'))
    assert read(store, 1).intent == 'make the thing work'


def test_create_allocates_ABOVE_every_existing_ordinal(store: GitRefStore):
    """The ordinary path, and its claim is only that: nothing here races, so this says the guess is
    sensible and NOT that the guess is what protects anybody. The protection is the refused push,
    which `test_publishing_the_same_ordinal_TWICE_is_refused` plants directly."""
    publish(store, _submission(store, ordinal=1))
    landed = create(store, participant='boxB', base='a' * 40, head='b' * 40, intent='second')
    assert landed.ordinal == 2
    assert read(store, 1).participant == 'boxA/session-3'
    assert read(store, 2).participant == 'boxB'


def test_create_gives_up_after_a_bounded_number_of_attempts(refusing_store: GitRefStore):
    """An escape hatch needs a CEILING. THE REFUSAL IS PLANTED IN THE REGION IT GOVERNS -- a real
    remote with a real hook that declines every push, so `create` meets the exception on every
    attempt and must end in one rather than in a loop nobody can see. A remote that refuses
    everything is what a revoked credential or a full disk actually looks like from here."""
    with pytest.raises(OrdinalTaken, match='consecutive ordinals'):
        create(refusing_store, participant='p', base='a' * 40, head='b' * 40, intent='x', attempts=3)


def test_a_remote_that_refuses_the_push_is_reported_as_a_submission_that_did_not_land(
    refusing_store: GitRefStore,
):
    """`publish` promises the ordinal is taken OR the remote refused, and both must raise. A refusal
    that returned normally would leave a caller holding a `Submission` object for something no
    reader anywhere can see."""
    head = _git(refusing_store.root, 'rev-parse', 'HEAD')
    with pytest.raises(OrdinalTaken):
        publish(refusing_store, Submission(ordinal=1, participant='p', base=head, head=head, intent='x'))


def test_submitted_ordinals_lists_what_is_there(store: GitRefStore):
    assert submitted_ordinals(store) == ()
    publish(store, _submission(store, ordinal=1))
    publish(store, _submission(store, ordinal=2))
    assert submitted_ordinals(store) == (1, 2)


def test_ordinals_are_ordered_NUMERICALLY_past_ten(store: GitRefStore):
    """The measured failure `refs.attempt_number` exists for: sorted as strings, 10 precedes 2, so
    the next ordinal is computed as 3 and collides forever from the tenth submission onward."""
    for ordinal in (2, 10):
        publish(store, _submission(store, ordinal=ordinal))
    assert submitted_ordinals(store) == (2, 10)
    assert create(store, participant='p', base='a' * 40, head='b' * 40, intent='x').ordinal == 11
