"""The ref transport, and the double that stands in for it.

TWO HALVES, AND THE SECOND IS THE ONE THAT EARNED ITS KEEP ON THE FIRST RUN. `GitRefStore` is
exercised against a REAL bare repository in `tmp_path`, then the DOUBLE is audited against it.

THE AUDIT IMMEDIATELY REFUTED THE PREMISE IT WAS WRITTEN UNDER. Both this seam and two repositories'
comments asserted that git's `*` does not cross a `/`, and the double was built segment-bounded to
match. Measured 2026-08-12 against real git: `*` DOES cross, and a pattern also matches any TAIL of
a refname beginning at a `/`. The double was therefore STRICTER than git -- the dangerous direction,
since a test could assert that a too-short pattern finds nothing while in production it finds
everything. A second, smaller one came out of the same run: `push --delete` of an absent ref exits 0,
so the double's tidy False was a distinction the transport does not make.

Both are now pinned as POSITIVE claims below, because a corrected belief that leaves no test behind
is a belief that comes back.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agent_swarm.refstore import GitRefStore, RefStore, RefUnreachable
from agent_swarm.testing import InMemoryRefStore


def _never() -> bool:
    """The rehearsal predicate for a store under test: these tests exercise the REAL writes."""
    return False


def _git(root: Path, *args: str) -> str:
    out = subprocess.run(['git', '-C', str(root), *args], capture_output=True, text=True, check=True)
    return out.stdout.strip()


@pytest.fixture
def remote_and_store(tmp_path: Path) -> tuple[Path, GitRefStore]:
    """A real bare remote and a real checkout pointed at it."""
    bare = tmp_path / 'remote.git'
    subprocess.run(['git', 'init', '--bare', '-q', str(bare)], check=True)
    work = tmp_path / 'work'
    subprocess.run(['git', 'init', '-q', str(work)], check=True)
    _git(work, 'config', 'user.email', 'x@example.com')
    _git(work, 'config', 'user.name', 'x')
    (work / 'a.txt').write_text('hello', encoding='utf-8')
    _git(work, 'add', '-A')
    _git(work, 'commit', '-qm', 'first')
    _git(work, 'remote', 'add', 'upstream', str(bare))
    return work, GitRefStore(work, 'upstream', withhold_writes=_never)


# --------------------------------------------------------------------------- the real transport


def test_it_satisfies_the_protocol():
    assert isinstance(GitRefStore(Path('.'), 'origin', withhold_writes=_never), RefStore)
    assert isinstance(InMemoryRefStore(), RefStore)


def test_head_is_the_checkouts_commit(remote_and_store):
    work, store = remote_and_store
    assert store.head() == _git(work, 'rev-parse', 'HEAD')


def test_a_write_then_a_list_round_trips(remote_and_store):
    _work, store = remote_and_store
    ok, _why = store.write('refs/ci/heartbeat/boxA/17', store.head())
    assert ok
    assert store.list('refs/ci/heartbeat/*/*') == {'refs/ci/heartbeat/boxA/17': store.head()}


def test_a_glob_ONE_WILDCARD_SHORT_STILL_MATCHES(remote_and_store):
    """THE MEASUREMENT THAT REFUTED THE PREMISE OF THIS FILE. Two repositories' comments said git's
    `*` does not cross a separator, and three tests here were written to assert it. Against a real
    remote it DOES cross -- so no caller can narrow a listing by depth, and every one of them must
    filter what comes back. Pinned as the positive claim so the false one cannot come back."""
    _work, store = remote_and_store
    store.write('refs/ci/heartbeat/boxA/17', store.head())
    assert set(store.list('refs/ci/heartbeat/*')) == {'refs/ci/heartbeat/boxA/17'}


def test_a_glob_matches_a_TAIL_at_a_slash_boundary(remote_and_store):
    """The other half of the measured rule, and the half nobody expects: a bare tail matches."""
    _work, store = remote_and_store
    store.write('refs/ci/heartbeat/boxA/17', store.head())
    assert set(store.list('boxA/17')) == {'refs/ci/heartbeat/boxA/17'}


def test_a_tail_that_does_NOT_start_at_a_boundary_misses(remote_and_store):
    """The discriminating half. Without it, "matches a tail" would be indistinguishable from
    "matches any substring", and the double could be written far too permissively."""
    _work, store = remote_and_store
    store.write('refs/ci/heartbeat/boxA/17', store.head())
    assert store.list('eartbeat/*') == {}


def test_a_PREFIX_with_no_wildcard_is_not_a_match(remote_and_store):
    """A pattern must match a whole tail, so a directory-ish prefix finds nothing -- which is the
    one case where a caller's intuition about narrowing accidentally holds."""
    _work, store = remote_and_store
    store.write('refs/ci/heartbeat/boxA/17', store.head())
    assert store.list('refs/ci/heartbeat/boxA') == {}


def test_an_empty_namespace_is_an_empty_dict(remote_and_store):
    _work, store = remote_and_store
    assert store.list('refs/ci/nothing/*') == {}


def test_an_UNREACHABLE_remote_RAISES(tmp_path: Path):
    """And this is the distinction the class exists for: not an empty answer."""
    work = tmp_path / 'work'
    subprocess.run(['git', 'init', '-q', str(work)], check=True)
    store = GitRefStore(work, 'a-remote-that-does-not-exist', withhold_writes=_never)
    with pytest.raises(RefUnreachable):
        store.list('refs/ci/*')


def test_a_delete_removes_it(remote_and_store):
    _work, store = remote_and_store
    store.write('refs/ci/heartbeat/boxA/17', store.head())
    assert store.delete('refs/ci/heartbeat/boxA/17')
    assert store.list('refs/ci/heartbeat/*/*') == {}


def test_deleting_an_ABSENT_ref_SUCCEEDS(remote_and_store):
    """MEASURED, and the second correction of the day: git warns and exits 0. So the return value
    cannot tell "removed it" from "there was nothing" -- right for a prune racing another prune,
    and a trap for any caller that reads it as "it existed"."""
    _work, store = remote_and_store
    assert store.delete('refs/ci/heartbeat/boxA/999') is True


def test_a_write_to_an_UNREACHABLE_remote_reports_the_reason(tmp_path: Path):
    """The pair rather than a bool: a liveness push that fails without words is a runner that reads
    as dead for a reason nobody on the box can see."""
    work = tmp_path / 'work'
    subprocess.run(['git', 'init', '-q', str(work)], check=True)
    _git(work, 'config', 'user.email', 'x@example.com')
    _git(work, 'config', 'user.name', 'x')
    (work / 'a.txt').write_text('x', encoding='utf-8')
    _git(work, 'add', '-A')
    _git(work, 'commit', '-qm', 'first')
    ok, why = GitRefStore(work, 'nope', withhold_writes=_never).write('refs/ci/x/1', _git(work, 'rev-parse', 'HEAD'))
    assert not ok
    assert why.strip()


def test_the_project_facts_are_required():
    """Which checkout and which remote are the consumer's deployment. A defaulted `origin` is right
    until it is not, and then the fleet publishes its liveness to somebody else's repository."""
    with pytest.raises(TypeError):
        GitRefStore()  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        GitRefStore(Path('.'))  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        # AND THE THIRD IS THE DESTRUCTIVE ONE. The tempting default is `lambda: False`, which
        # works, so a consumer that forgot to wire its rehearsal flag reaches the real remote while
        # its own `--dry-run` reports otherwise -- invisible precisely because it works.
        GitRefStore(Path('.'), 'origin')  # type: ignore[call-arg]


# --------------------------------------------------------------------------- the double agrees


def test_the_double_has_the_SAME_glob_semantics(remote_and_store):
    """THE AUDIT THAT FOUND THE DEFECT, and it is run over the matching cases AND the missing ones.

    Only-matching cases would have passed against the original segment-bounded double for two of
    these patterns, which is how a too-strict double survives review: it agrees wherever anyone
    thinks to look. The `eartbeat/*` and bare-prefix rows are what separate "the double matches
    like git" from "the double happens to agree on the patterns we use".
    """
    _work, real = remote_and_store
    fake = InMemoryRefStore(head=real.head())
    for ref in ('refs/ci/heartbeat/boxA/17', 'refs/ci/heartbeat/boxB/18', 'refs/ci/fleet/boxA/native'):
        real.write(ref, real.head())
        fake.write(ref, fake.head())
    patterns = (
        'refs/ci/heartbeat/*',
        'refs/ci/heartbeat/*/*',
        'refs/ci/heartbeat/boxA/*',
        'refs/ci/*',
        'boxA/17',
        '17',
        'eartbeat/*',
        'refs/ci/heartbeat/boxA',
        'refs/ci/nothing/*',
    )
    for pattern in patterns:
        assert set(real.list(pattern)) == set(fake.list(pattern)), pattern


def test_the_double_also_reports_an_absent_delete_as_success():
    """Matching the measurement above rather than the tidier answer, because a double that made
    the distinction would offer a signal production never carries."""
    assert InMemoryRefStore().delete('refs/ci/nothing/1') is True


def test_the_double_can_be_made_UNREACHABLE():
    """Because that state is most of what the seam is for, and it cannot be produced on demand from
    a working remote."""
    fake = InMemoryRefStore()
    fake.unreachable = True
    with pytest.raises(RefUnreachable):
        fake.list('refs/ci/*')


def test_the_double_can_be_made_to_FAIL_A_WRITE():
    fake = InMemoryRefStore()
    fake.fail_writes = 'remote: denied'
    ok, why = fake.write('refs/ci/x/1', fake.head())
    assert not ok and 'denied' in why
    assert 'refs/ci/x/1' not in fake.refs


def test_the_doubles_listing_order_is_NOT_insertion_order():
    """Rotated by half, for the reason the forge double is: insertion order makes "the first one
    back" and "the oldest" the same function, and hides a tie-break defect until production."""
    fake = InMemoryRefStore()
    written = [f'refs/ci/heartbeat/boxA/{n}' for n in (1, 2, 3, 4)]
    for ref in written:
        fake.write(ref, fake.head())
    assert list(fake.list('refs/ci/heartbeat/*/*')) != written
    assert set(fake.list('refs/ci/heartbeat/*/*')) == set(written)


def test_a_DEEPER_pattern_reaches_LESS_not_more(remote_and_store):
    """THE ROW THAT REVERSES THE INTUITION, pinned against real git with both depths present.

    `*` crosses separators, but every literal `/` in a pattern is REQUIRED -- so each extra segment
    narrows. A retention sweep must therefore keep its SHALLOW pattern; dropping that one as
    "redundant" is what would grandfather the older refs forever, which is the exact opposite of
    what the comment this replaced advised.
    """
    _work, store = remote_and_store
    store.write('refs/verdicts/OLD/fast', store.head())
    store.write('refs/verdicts/NEW/fast/ENV', store.head())
    assert set(store.list('refs/verdicts/*')) == {'refs/verdicts/OLD/fast', 'refs/verdicts/NEW/fast/ENV'}
    assert set(store.list('refs/verdicts/*/*')) == {'refs/verdicts/OLD/fast', 'refs/verdicts/NEW/fast/ENV'}
    assert set(store.list('refs/verdicts/*/*/*')) == {'refs/verdicts/NEW/fast/ENV'}


def test_the_double_agrees_that_a_deeper_pattern_reaches_less(remote_and_store):
    """The audit for the row above. A double that got only the crossing half right would pass every
    other test in this file and still mislead a retention sweep."""
    _work, real = remote_and_store
    fake = InMemoryRefStore(head=real.head())
    for ref in ('refs/verdicts/OLD/fast', 'refs/verdicts/NEW/fast/ENV'):
        real.write(ref, real.head())
        fake.write(ref, fake.head())
    for pattern in ('refs/verdicts/*', 'refs/verdicts/*/*', 'refs/verdicts/*/*/*'):
        assert set(real.list(pattern)) == set(fake.list(pattern)), pattern
