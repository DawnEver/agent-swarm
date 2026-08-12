"""A rehearsal must reach NO remote, through ANY git call the store offers -- not just the four.

WHY THIS SEAM IS THE DELIVERABLE AND NOT A CONVENIENCE. motronics' `ci_tick.py` grew four git entry
points, each with its own `subprocess.run`, and a `--dry-run` flag that only some of them consulted:
a rehearsal announced that it was withholding writes while pushing through the others. The repair
was one funnel. It then had to write a FIFTH `RefStore` implementation of its own -- duplicating
`GitRefStore` method for method -- purely because the packaged one spawned its own subprocess and
would have reached the forge through a path the refusal did not cover.

SO THE FUNNEL IS THE PROPERTY UNDER TEST, NOT THE FLAG. A test that only checked `write()` would
pass against a store whose `run()` and `text()` push freely, which is the arrangement that actually
shipped. Every entry point is therefore exercised below through a REAL git repository with a REAL
remote, and the assertion is on what the remote HOLDS afterwards -- never on what was printed.

THE TWO DIRECTIONS ARE BOTH HERE. A store that withheld everything would also pass a
"nothing reached the remote" test while being useless, so each refusal has a control that performs
the same call with the flag down and asserts the write DID land.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agent_swarm import refstore
from agent_swarm.refstore import GitRefStore

pytestmark = pytest.mark.unit


def _git(cwd: Path, *args: str) -> str:
    out = subprocess.run(['git', '-C', str(cwd), *args], capture_output=True, text=True, check=True)
    return out.stdout.strip()


@pytest.fixture
def work_and_remote(tmp_path: Path) -> tuple[Path, Path]:
    """A real checkout with a real bare remote, so the refusal is measured against git itself."""
    bare = tmp_path / 'remote.git'
    subprocess.run(['git', 'init', '--bare', '-q', str(bare)], check=True)
    work = tmp_path / 'work'
    subprocess.run(['git', 'init', '-q', '-b', 'main', str(work)], check=True)
    _git(work, 'config', 'user.email', 'a@b.c')
    _git(work, 'config', 'user.name', 'T')
    (work / 'f.txt').write_text('x', encoding='utf-8')
    _git(work, 'add', '-A')
    _git(work, 'commit', '-qm', 'first')
    _git(work, 'remote', 'add', 'upstream', str(bare))
    _git(work, 'push', '-q', 'upstream', 'main')
    return work, bare


@pytest.fixture
def store(work_and_remote) -> GitRefStore:
    work, _ = work_and_remote
    return GitRefStore(work, 'upstream', withhold_writes=refstore.withholding_writes)


def _remote_refs(store: GitRefStore) -> dict[str, str]:
    """Asked with the flag DOWN, so the audit itself is never withheld."""
    with store.withholding(False):
        return store.list('refs/probe/*')


# --------------------------------------------------------------- the verb, not the word


def test_only_the_VERB_counts_so_a_ref_named_push_is_still_readable(store) -> None:
    """`args[0]`, never `'push' in args`. A guard that refuses reads it was never asked to refuse
    is how a safety flag becomes one people route around -- and a branch may legitimately be
    named `push`.
    """
    assert store.mutates_the_forge(['push', 'upstream', 'x:y'])
    assert not store.mutates_the_forge(['ls-remote', 'upstream', 'refs/heads/push'])
    assert not store.mutates_the_forge(['rev-parse', 'push'])
    assert not store.mutates_the_forge([])


def test_fetch_is_deliberately_NOT_withheld(store) -> None:
    """It writes only this checkout's remote-tracking refs, and a caller measuring how far behind
    it is needs it to answer at all. Forbidding it would make a rehearsal report a staleness it
    could not measure.
    """
    assert not store.mutates_the_forge(['fetch', 'upstream'])


# --------------------------------------------------------------- every entry point, both directions


def test_write_is_withheld_and_the_remote_stays_empty(store) -> None:
    head = store.head()
    with store.withholding(True):
        ok, _ = store.write('refs/probe/a', head)
    assert ok, 'a rehearsal must not take the FAILURE branch -- that reports an outage that is not one'
    assert _remote_refs(store) == {}, 'the rehearsal reached the remote'


def test_write_LANDS_when_the_flag_is_down(store) -> None:
    """The control. Without it, a store that withheld unconditionally would pass the test above."""
    head = store.head()
    with store.withholding(False):
        assert store.write('refs/probe/a', head)[0]
    assert _remote_refs(store) == {'refs/probe/a': head}


def test_delete_is_withheld_because_it_is_a_push(store) -> None:
    """THE DESTRUCTIVE HALF, and the one a `verb == 'push'` reading is easy to get wrong: a ref
    deletion is spelled `push --delete`, so a guard keyed on anything narrower lets it through.
    """
    head = store.head()
    with store.withholding(False):
        store.write('refs/probe/a', head)
    with store.withholding(True):
        assert store.delete('refs/probe/a')
    assert _remote_refs(store) == {'refs/probe/a': head}, 'a rehearsal DELETED a ref from the remote'


def test_the_RAW_run_seam_is_withheld_too(store) -> None:
    """The entry point a future writer will reach for. If only the four named operations were
    guarded, the next `store.run('push', ...)` added anywhere would bypass the refusal silently --
    which is precisely the defect that produced this funnel.
    """
    head = store.head()
    with store.withholding(True):
        out = store.run('push', 'upstream', f'{head}:refs/probe/raw', '--force')
    assert out.returncode == 0
    assert _remote_refs(store) == {}


def test_the_TEXT_helper_is_withheld_too(store) -> None:
    """`text(check=True)` raises on a non-zero exit, so a withheld write must return success-shaped
    output or a rehearsal would raise where a real run would not.
    """
    head = store.head()
    with store.withholding(True):
        assert store.text('push', 'upstream', f'{head}:refs/probe/text', '--force') == ''
    assert _remote_refs(store) == {}


def test_the_OK_helper_is_withheld_and_still_reports_success(store) -> None:
    head = store.head()
    with store.withholding(True):
        assert store.ok('push', 'upstream', f'{head}:refs/probe/ok', '--force')
    assert _remote_refs(store) == {}


def test_READS_still_work_during_a_rehearsal(store) -> None:
    """THE OTHER FAILURE MODE. A rehearsal that could not READ would report an empty fleet, an
    unreachable forge, or nothing to do -- deciding differently from the real run it rehearses,
    which makes it worthless as a rehearsal.
    """
    head = store.head()
    with store.withholding(False):
        store.write('refs/probe/a', head)
    with store.withholding(True):
        assert store.head() == head
        assert store.list('refs/probe/*') == {'refs/probe/a': head}


# --------------------------------------------------------------- arming and disarming


def test_the_flag_is_RESTORED_even_when_the_body_raises(store) -> None:
    """A pass that raises must not leave the refusal armed for a later caller in the same
    interpreter -- which is exactly the arrangement a test suite is.
    """
    with pytest.raises(ZeroDivisionError), store.withholding(True):
        _ = 1 / 0
    assert not refstore.withholding_writes()


def test_it_RESTORES_rather_than_assuming_the_outer_state_was_False(store) -> None:
    """`reset(token)`, not `= False`. Nothing nests passes today; a restore that GUESSES is how
    that stops being true safely one day and unsafely the next.
    """
    with store.withholding(True):
        with store.withholding(False):
            assert not refstore.withholding_writes()
        # NESTED, NOT `with A, B:` -- a single with-statement exits both at once and could not
        # observe the restore at all. The assertion is what the INNER exit put back.
        assert refstore.withholding_writes(), 'the inner pass reset the flag to a guess, not to what it found'
    assert not refstore.withholding_writes()


def test_a_consumer_may_supply_its_OWN_predicate(work_and_remote) -> None:
    """The argument is not decoration: a consumer whose rehearsal flag lives elsewhere keeps it,
    and nothing here reaches for this module's global on its behalf.
    """
    work, _ = work_and_remote
    mine = {'on': True}
    store = GitRefStore(work, 'upstream', withhold_writes=lambda: mine['on'])
    head = store.head()
    store.write('refs/probe/own', head)
    assert _remote_refs(store) == {}, "the store ignored the consumer's own predicate"
    mine['on'] = False
    store.write('refs/probe/own', head)
    assert _remote_refs(store) == {'refs/probe/own': head}


# --------------------------------------------------------------- the payload writer


def test_a_payload_round_trips_through_the_documented_read_path(store) -> None:
    """`git cat-file -p <ref>:<filename>` is what a reader is told to type, so that is what is
    asserted -- not the blob's existence.
    """
    store.write_payload('refs/probe/p', {'result': 'PASS', 'n': 3}, 'a message', filename='payload.json')
    assert '"result": "PASS"' in store.text('cat-file', '-p', 'refs/probe/p:payload.json')


def test_the_payload_filename_carries_no_CARRIAGE_RETURN(store) -> None:
    """THE MEASURED BUG THIS ENCODES. `text=True` on the `mktree` pipe rewrites `\\n` as `\\r\\n` on
    Windows, and the `\\r` lands INSIDE the tree entry's filename -- so the ref exists, the commit
    exists, and the documented read path answers *path does not exist*.
    """
    store.write_payload('refs/probe/p', {'a': 1}, 'm', filename='payload.json')
    listing = store.text('cat-file', '-p', 'refs/probe/p^{tree}')
    assert '\r' not in listing, f'a carriage return is inside the tree entry: {listing!r}'
    assert listing.endswith('payload.json'), listing


def test_write_payload_PUSHES_NOTHING(store) -> None:
    """The durable half must land without touching the network -- that separation is what lets a
    consumer record an expensive answer before the fragile step.
    """
    store.write_payload('refs/probe/p', {'a': 1}, 'm', filename='payload.json')
    assert _remote_refs(store) == {}


def test_the_payload_filename_has_NO_DEFAULT(store) -> None:
    """It is half a contract between a writer here and a reader somewhere else. A default would let
    the two drift while both kept passing -- which this package has already paid for once.
    """
    with pytest.raises(TypeError):
        store.write_payload('refs/probe/p', {'a': 1}, 'm')  # type: ignore[call-arg]
