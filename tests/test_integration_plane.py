"""The integration plane, against real git: a real merge, a real tree, a real compare-and-swap.

EVERY MERGE HERE IS PERFORMED BY GIT. That is not thoroughness for its own sake -- the module exists
because a verdict on a BRANCH is not a verdict on the MERGE, and a suite that faked the merge would
be asserting the same false premise the module was written to refuse. So the central test plants the
measured case directly: two submissions each carrying HALF of what a check needs, each of which
FAILS the injected verdict alone and PASSES together.

THE VERDICT FUNCTION IS INJECTED AND IT READS THE TREE. Several tests hand `integrate` a function
that actually inspects the tree sha it was given, rather than returning a constant, because a
verdict function that ignores its argument cannot tell whether the argument was the merged tree or
the trunk -- and "it was handed the wrong tree" is precisely the bug that would survive a constant.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agent_swarm import refs
from agent_swarm.integration import (
    CONFLICTED,
    INTEGRATED,
    REJECTED,
    Conflict,
    HeadNotPresent,
    Merge,
    TrunkMoved,
    advance,
    build_merge,
    disposed_ordinals,
    disposition_of,
    integrate,
    open_ordinals,
    open_submissions,
    order_explanation,
    order_key,
    queue,
    record,
    trunk_commit,
)
from agent_swarm.refstore import GitRefStore
from agent_swarm.shards import FAIL, INCONCLUSIVE, PASS
from agent_swarm.submission import Submission, publish


TRUNK = 'trunk'


def _never() -> bool:
    return False


def _git(root: Path, *args: str) -> str:
    out = subprocess.run(['git', '-C', str(root), *args], capture_output=True, text=True, check=True, timeout=60)
    return out.stdout.strip()


@pytest.fixture
def store(tmp_path: Path) -> GitRefStore:
    """A real remote, a real checkout, and a trunk with one commit on it."""
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
    return GitRefStore(work, 'upstream', withhold_writes=_never)


def _branch_with(store: GitRefStore, name: str, files: dict[str, str]) -> str:
    """A branch off the trunk carrying `files`. Returns its head sha."""
    root = store.root
    _git(root, 'checkout', '-q', '-b', name, TRUNK)
    for path, text in files.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding='utf-8')
    _git(root, 'add', '-A')
    _git(root, 'commit', '-qm', f'work on {name}')
    head = _git(root, 'rev-parse', 'HEAD')
    _git(root, 'checkout', '-q', TRUNK)
    return head


def _submit(store: GitRefStore, ordinal: int, name: str, files: dict[str, str]) -> Submission:
    head = _branch_with(store, name, files)
    sub = Submission(
        ordinal=ordinal,
        participant=name,
        base=_git(store.root, 'rev-parse', TRUNK),
        head=head,
        intent=f'{name} does its half',
        declared_paths=tuple(files),
    )
    publish(store, sub)
    return sub


def _stale_clash(store: GitRefStore, *, ordinal: int, publish_it: bool = True) -> Submission:
    """A submission that REALLY cannot merge: the trunk edits `shared.txt`, and so does a branch cut
    from before that edit. The conflict is git's own, not a flag this suite set."""
    _git(store.root, 'checkout', '-q', TRUNK)
    stale_point = _git(store.root, 'rev-parse', 'HEAD')
    (store.root / 'shared.txt').write_text('the trunk edited this line\n', encoding='utf-8')
    _git(store.root, 'add', '-A')
    _git(store.root, 'commit', '-qm', 'trunk changes the shared file')

    _git(store.root, 'checkout', '-q', '-b', f'clasher{ordinal}', stale_point)
    (store.root / 'shared.txt').write_text('a different edit of the same line\n', encoding='utf-8')
    _git(store.root, 'add', '-A')
    _git(store.root, 'commit', '-qm', 'clashing edit')
    head = _git(store.root, 'rev-parse', 'HEAD')
    _git(store.root, 'checkout', '-q', TRUNK)

    sub = Submission(ordinal=ordinal, participant='clasher', base=stale_point, head=head, intent='edits the same lines')
    if publish_it:
        publish(store, sub)
    return sub


def _pass(_tree: str) -> str:
    return PASS


# --------------------------------------------------------------------------- order


def test_the_queue_is_arrival_order_and_deterministic():
    subs = [Submission(ordinal=n, participant=f'p{n}', base='a' * 40, head='b' * 40, intent='x') for n in (3, 1, 2)]
    assert [s.ordinal for s in queue(subs)] == [1, 2, 3]
    assert queue(subs) == queue(reversed(subs)), 'the order depends on how the caller happened to iterate'


def test_the_order_can_be_EXPLAINED_position_by_position():
    """ "Why is this one first" must have an answer, and it must come from the function that does the
    ordering -- an explanation written separately keeps describing the old rule after a change."""
    subs = [Submission(ordinal=n, participant=f'p{n}', base='a' * 40, head='b' * 40, intent='x') for n in (2, 1)]
    lines = order_explanation(subs)
    assert lines[0].startswith('1. submission 1 ')
    assert 'arrival order' in lines[0]
    assert order_key(subs[0]) == (2,)


# --------------------------------------------------------------------------- the three-valued verdict


def test_PASS_and_FAIL_dispose_but_INCONCLUSIVE_does_NOT():
    """The single most important line in the module, asserted directly rather than through a batch:
    INCONCLUSIVE maps to NO disposition, which is what makes a requeue the absence of a write."""
    assert disposition_of(PASS) == INTEGRATED
    assert disposition_of(FAIL) == REJECTED
    assert disposition_of(INCONCLUSIVE) is None


def test_a_fourth_verdict_word_is_refused_rather_than_guessed():
    with pytest.raises(ValueError, match='not a verdict'):
        disposition_of('GREEN')


def test_record_REFUSES_to_write_a_non_terminal_disposition(store: GitRefStore):
    """The requeue path cannot close a submission through a typo: the only value `disposition_of`
    returns for INCONCLUSIVE is `None`, and `None` is refused here."""
    sub = _submit(store, 1, 'a', {'a.txt': 'a\n'})
    with pytest.raises(ValueError, match='terminal disposition'):
        record(store, sub, None, 'x')  # type: ignore[arg-type]
    with pytest.raises(ValueError, match='terminal disposition'):
        record(store, sub, INCONCLUSIVE, 'x')


def test_a_disposition_is_written_once_and_not_overwritten(store: GitRefStore):
    """A second integrator reaching the same submission must not be able to unsay a decision that
    may already have advanced the trunk."""
    sub = _submit(store, 1, 'a', {'a.txt': 'a\n'})
    assert record(store, sub, INTEGRATED, 'landed')
    assert not record(store, sub, REJECTED, 'no it did not')


# --------------------------------------------------------------------------- what is open


def test_open_is_the_ABSENCE_of_a_disposition(store: GitRefStore):
    a = _submit(store, 1, 'a', {'a.txt': 'a\n'})
    _submit(store, 2, 'b', {'b.txt': 'b\n'})
    assert open_ordinals(store) == (1, 2)
    record(store, a, INTEGRATED, 'landed')
    assert disposed_ordinals(store) == frozenset({1})
    assert open_ordinals(store) == (2,)
    assert [s.ordinal for s in open_submissions(store)] == [2]


# --------------------------------------------------------------------------- the REAL merged tree


def test_two_halves_that_are_each_RED_alone_are_GREEN_together(store: GitRefStore):
    """THE MEASUREMENT THIS MODULE EXISTS FOR, planted as a real merge.

    Each submission writes one of the two lines a check needs. The injected verdict reads the tree
    it is given and passes only when BOTH are present -- so a merged tree that really contains both
    is the only thing that can produce a PASS here, and gating either branch alone cannot.
    """
    first = _submit(store, 1, 'first', {'half_a.txt': 'A\n'})
    second = _submit(store, 2, 'second', {'half_b.txt': 'B\n'})

    def both_halves(tree: str) -> str:
        listing = _git(store.root, 'ls-tree', '-r', '--name-only', tree).split()
        return PASS if {'half_a.txt', 'half_b.txt'} <= set(listing) else FAIL

    # Each branch ALONE fails the very same check.
    for sub in (first, second):
        alone = build_merge(
            store, trunk_commit=trunk_commit(store, TRUNK), submissions=[sub], workdir=store.root.parent / 'wt-alone'
        )
        assert both_halves(alone.tree) == FAIL

    result = integrate(
        store,
        trunk=TRUNK,
        submissions=[first, second],
        verdict_of=both_halves,
        workdir=store.root.parent / 'wt',
    )
    assert result.verdict == PASS
    assert result.advanced
    assert [s.ordinal for s in result.integrated] == [1, 2]
    assert _git(store.root, 'rev-parse', TRUNK) == result.merge.commit


def test_the_verdict_function_receives_the_MERGED_tree_and_not_the_trunk(store: GitRefStore):
    """A constant-returning verdict function cannot tell these apart, so this one records what it
    was handed and the test compares it against both candidates."""
    sub = _submit(store, 1, 'a', {'new.txt': 'a\n'})
    trunk_tree = _git(store.root, 'rev-parse', f'{TRUNK}^{{tree}}')
    seen: list[str] = []

    def watching(tree: str) -> str:
        seen.append(tree)
        return PASS

    result = integrate(store, trunk=TRUNK, submissions=[sub], verdict_of=watching, workdir=store.root.parent / 'wt')
    assert seen == [result.merge.tree]
    assert seen[0] != trunk_tree, 'the verdict was handed the trunk, not the merge'


def test_a_conflict_is_recorded_and_the_REST_of_the_batch_proceeds(store: GitRefStore):
    """A conflict is ordinary. The batch must not be abandoned because one participant's base went
    stale -- verdict capacity is the scarce resource and the others' work is unaffected."""
    clashing = _stale_clash(store, ordinal=1)
    innocent = _submit(store, 2, 'innocent', {'elsewhere.txt': 'fine\n'})

    result = integrate(
        store,
        trunk=TRUNK,
        submissions=[clashing, innocent],
        verdict_of=_pass,
        workdir=store.root.parent / 'wt',
    )
    assert [c.submission.ordinal for c in result.conflicts] == [1]
    assert result.conflicts[0].paths == ('shared.txt',)
    assert [s.ordinal for s in result.integrated] == [2], 'one conflict abandoned the whole batch'
    assert disposed_ordinals(store) == frozenset({1, 2})
    assert result.advanced

    # The disposition must SAY conflicted and name the path. "It was disposed of" is not something a
    # participant can act on, and rebasing against that path is what it has to do next.
    _git(store.root, 'fetch', 'upstream', refs.outcome_ref(1))
    recorded = _git(store.root, 'cat-file', '-p', 'FETCH_HEAD:outcome.json')
    assert CONFLICTED in recorded
    assert 'shared.txt' in recorded


def test_a_batch_in_which_EVERYTHING_conflicted_spends_no_verdict(store: GitRefStore):
    """The merged tree would be the trunk unchanged. Judging it would spend one of the day's runs
    re-confirming the trunk and would report a PASS a reader could read as being about the batch."""
    clashing = _stale_clash(store, ordinal=1)

    called: list[str] = []

    def counted(tree: str) -> str:
        called.append(tree)
        return PASS

    result = integrate(store, trunk=TRUNK, submissions=[clashing], verdict_of=counted, workdir=store.root.parent / 'wt')
    assert called == [], 'a verdict was spent on a tree containing nothing'
    assert not result.advanced
    assert result.merge.is_empty()


def test_a_missing_head_stops_the_batch_BEFORE_a_partial_tree_exists(store: GitRefStore):
    """A tree containing some participants' work and not others' would be judged, and would pass or
    fail on behalf of work that was not in it."""
    present = _submit(store, 1, 'a', {'a.txt': 'a\n'})
    absent = Submission(ordinal=2, participant='ghost', base='a' * 40, head='b' * 40, intent='never fetched')
    with pytest.raises(HeadNotPresent, match='fetch them'):
        build_merge(
            store,
            trunk_commit=trunk_commit(store, TRUNK),
            submissions=[present, absent],
            workdir=store.root.parent / 'wt',
        )


def test_the_worktree_is_removed_even_when_the_merge_conflicted(store: GitRefStore):
    """A leaked worktree is a permanent entry in the checkout's administrative files. The conflict
    path is the one that leaves it dirty, so it is the path the cleanup has to survive."""
    clashing = _stale_clash(store, ordinal=1, publish_it=False)
    workdir = store.root.parent / 'wt'
    merge = build_merge(store, trunk_commit=trunk_commit(store, TRUNK), submissions=[clashing], workdir=workdir)
    assert merge.conflicts
    assert not workdir.exists()
    assert 'wt' not in _git(store.root, 'worktree', 'list')


# --------------------------------------------------------------------------- the CAS


def test_the_trunk_advances_only_from_the_expected_parent(store: GitRefStore):
    """`git update-ref <ref> <new> <old>` IS the compare-and-swap. The competing write is planted
    for real between the read and the swap, which is the only way to know the comparison happens."""
    expected = trunk_commit(store, TRUNK)
    (store.root / 'someone_else.txt').write_text('a peer landed first\n', encoding='utf-8')
    _git(store.root, 'add', '-A')
    _git(store.root, 'commit', '-qm', 'a peer advanced the trunk')
    moved_to = trunk_commit(store, TRUNK)
    assert moved_to != expected

    with pytest.raises(TrunkMoved):
        advance(store, trunk=TRUNK, expected=expected, new=_git(store.root, 'rev-parse', 'HEAD'))
    assert trunk_commit(store, TRUNK) == moved_to, 'the refused swap moved the trunk anyway'


def test_a_PASS_against_a_trunk_that_MOVED_leaves_every_submission_open(store: GitRefStore):
    """The recoverable order: the swap runs before any disposition is written, so a trunk that moved
    under a slow verdict closes nothing. Recording first would close submissions whose work never
    landed anywhere."""
    sub = _submit(store, 1, 'a', {'a.txt': 'a\n'})

    def moves_the_trunk_then_passes(_tree: str) -> str:
        (store.root / 'peer.txt').write_text('a peer landed first\n', encoding='utf-8')
        _git(store.root, 'add', '-A')
        _git(store.root, 'commit', '-qm', 'peer')
        return PASS

    with pytest.raises(TrunkMoved):
        integrate(
            store,
            trunk=TRUNK,
            submissions=[sub],
            verdict_of=moves_the_trunk_then_passes,
            workdir=store.root.parent / 'wt',
        )
    assert open_ordinals(store) == (1,), 'a submission was closed although its work never landed'


# --------------------------------------------------------------------------- FAIL vs INCONCLUSIVE


def test_a_FAIL_rejects_the_batch_and_does_not_advance(store: GitRefStore):
    sub = _submit(store, 1, 'a', {'a.txt': 'a\n'})
    before = trunk_commit(store, TRUNK)
    result = integrate(
        store, trunk=TRUNK, submissions=[sub], verdict_of=lambda _t: FAIL, workdir=store.root.parent / 'wt'
    )
    assert [s.ordinal for s in result.rejected] == [1]
    assert result.requeued == ()
    assert not result.advanced
    assert trunk_commit(store, TRUNK) == before
    assert open_ordinals(store) == (), 'a rejected submission is terminal and must not requeue'


def test_an_INCONCLUSIVE_requeues_and_records_NOTHING(store: GitRefStore):
    """THE DEFECT THIS PACKAGE EXISTS TO REFUSE, asserted on the store rather than on the return
    value: after a run that could not answer, the submission must still be OPEN, and the outcome
    namespace must be untouched. A rejection recorded here would blame a submission for having been
    scheduled on a broken machine, and afterwards would be indistinguishable from a real failure."""
    sub = _submit(store, 1, 'a', {'a.txt': 'a\n'})
    before = trunk_commit(store, TRUNK)

    result = integrate(
        store, trunk=TRUNK, submissions=[sub], verdict_of=lambda _t: INCONCLUSIVE, workdir=store.root.parent / 'wt'
    )
    assert [s.ordinal for s in result.requeued] == [1]
    assert result.rejected == ()
    assert not result.advanced
    assert trunk_commit(store, TRUNK) == before
    assert disposed_ordinals(store) == frozenset(), 'an inconclusive run wrote a disposition'
    assert open_ordinals(store) == (1,)
    assert [s.ordinal for s in open_submissions(store)] == [1], 'the submission did not requeue'


def test_the_SAME_submission_integrates_on_a_later_pass_after_an_INCONCLUSIVE(store: GitRefStore):
    """The requeue is worth nothing unless the next pass really can land it. Same submission, same
    store, a run that can answer this time."""
    sub = _submit(store, 1, 'a', {'a.txt': 'a\n'})
    integrate(
        store, trunk=TRUNK, submissions=[sub], verdict_of=lambda _t: INCONCLUSIVE, workdir=store.root.parent / 'wt1'
    )
    again = integrate(
        store,
        trunk=TRUNK,
        submissions=open_submissions(store),
        verdict_of=_pass,
        workdir=store.root.parent / 'wt2',
    )
    assert again.advanced
    assert [s.ordinal for s in again.integrated] == [1]
    assert open_ordinals(store) == ()


def test_the_disposition_of_an_integrated_submission_names_where_it_landed(store: GitRefStore):
    """A disposition a human cannot trace back to a commit is a status, not a record."""
    sub = _submit(store, 1, 'a', {'a.txt': 'a\n'})
    result = integrate(store, trunk=TRUNK, submissions=[sub], verdict_of=_pass, workdir=store.root.parent / 'wt')
    _git(store.root, 'fetch', 'upstream', refs.outcome_ref(1))
    payload = _git(store.root, 'cat-file', '-p', 'FETCH_HEAD:outcome.json')
    assert INTEGRATED in payload
    assert result.merge.commit[:12] in payload


# --------------------------------------------------------------------------- the shapes themselves


def test_a_merge_and_a_conflict_are_immutable_records():
    """They are handed to a caller that reports on them; a field edited afterwards would describe a
    merge that never happened while the tree sha still pointed at the real one."""
    merge = Merge(tree='a' * 40, commit='b' * 40, base='c' * 40, merged=(), conflicts=())
    with pytest.raises(Exception):  # noqa: B017 -- dataclass raises FrozenInstanceError
        merge.tree = 'd' * 40  # type: ignore[misc]
    conflict = Conflict(
        submission=Submission(ordinal=1, participant='p', base='a' * 40, head='b' * 40, intent='x'), paths=('a.txt',)
    )
    with pytest.raises(Exception):  # noqa: B017
        conflict.paths = ()  # type: ignore[misc]
