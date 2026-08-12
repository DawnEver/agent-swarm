"""Two lanes, one working tree -- the collision that happened, planted so the guard is proved to fire.

MEASURED 2026-08-12, in this package's own repository: two lanes ran `git checkout -b` in the shared
checkout **21 seconds apart**, shared one working tree, and one lane's files were committed under the
other's HEAD. Nothing was lost only because it was caught early. `agent_swarm.lanes` already exported
`create_lane`, and it was simply not used -- so the fix cannot be a convention, it has to be a check.

WHAT IS ACTUALLY DETECTABLE, which is the whole design of this guard. "Someone ran `git checkout -b`"
is not a state: afterwards nothing distinguishes a branch made that way from one made any other way.
"Two branches of this fan-out resolve to one working tree" IS a state, readable at any moment, and it
is the property that matters -- the collision was caused by two lanes editing one set of files, not
by a particular verb.

EVERY TEST HERE PLANTS THE CONDITION IN A REAL GIT REPOSITORY. `test_the_guard_can_FAIL` reproduces
the measured sequence exactly -- two `git checkout -b` in one checkout, no worktree in sight -- and
asserts the guard reds on it. Its sibling then fixes it the way `create_lane` would and asserts the
guard goes quiet, because a check that reds on everything is as useless as one that reds on nothing.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agent_swarm.lanes import LaneError, all_worktrees, fanout_conflicts, require_disjoint_worktrees, worktrees


def _git(root: Path, *args: str) -> str:
    out = subprocess.run(['git', '-C', str(root), *args], capture_output=True, text=True, check=True, timeout=60)
    return out.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / 'repo'
    subprocess.run(['git', 'init', '-q', '-b', 'main', str(root)], check=True, timeout=60)
    _git(root, 'config', 'user.email', 'x@example.com')
    _git(root, 'config', 'user.name', 'x')
    (root / 'file.txt').write_text('base\n', encoding='utf-8')
    _git(root, 'add', '-A')
    _git(root, 'commit', '-qm', 'base')
    return root


def _proper_lane(repo: Path, name: str) -> Path:
    """A lane the way `create_lane` makes one: its own branch in its own worktree."""
    path = repo.parent / 'lanes' / name
    _git(repo, 'worktree', 'add', '-q', '-b', name, str(path), 'main')
    return path


class TestTheGuardCanFAIL:
    def test_the_MEASURED_collision_is_detected(self, repo):
        """**THE PROOF THIS CAN FIRE.** The exact sequence from 2026-08-12: two `git checkout -b` in
        one shared checkout. Both branches exist; only the last one is checked out anywhere, and it
        is checked out in the tree the first one was supposed to be working in."""
        _git(repo, 'checkout', '-q', '-b', 'lane-one')
        _git(repo, 'checkout', '-q', '-b', 'lane-two')

        conflicts = fanout_conflicts(repo, ['lane-one', 'lane-two'])

        assert conflicts, 'the guard did not fire on the collision it was written for'
        assert any('lane-one' in reason for reason in conflicts)
        assert any('no working tree of its own' in reason for reason in conflicts)

    def test_the_guard_RAISES_and_names_every_conflict_at_once(self, repo):
        """One at a time teaches the requirement by attrition, and a caller that has to iterate
        starts guessing."""
        _git(repo, 'checkout', '-q', '-b', 'lane-one')
        _git(repo, 'checkout', '-q', '-b', 'lane-two')
        _git(repo, 'checkout', '-q', '-b', 'lane-three')

        with pytest.raises(LaneError) as caught:
            require_disjoint_worktrees(repo, ['lane-one', 'lane-two', 'lane-three'])

        message = str(caught.value)
        assert 'lane-one' in message and 'lane-two' in message
        assert message.startswith('REFUSING:')

    def test_a_lane_whose_worktree_was_deleted_by_hand_is_caught_too(self, repo):
        """The guard asserts the PROPERTY, so it does not care how the tree came to be shared."""
        path = _proper_lane(repo, 'lane-one')
        _proper_lane(repo, 'lane-two')
        assert fanout_conflicts(repo, ['lane-one', 'lane-two']) == []

        _git(repo, 'worktree', 'remove', '--force', str(path))

        assert fanout_conflicts(repo, ['lane-one', 'lane-two']), 'a lane with no tree left went unnoticed'


class TestTheGuardIsQuietOnAWellFormedFanOut:
    def test_lanes_made_the_way_create_lane_makes_them_pass(self, repo):
        """A check that reds on everything is as useless as one that reds on nothing."""
        _proper_lane(repo, 'lane-one')
        _proper_lane(repo, 'lane-two')
        _proper_lane(repo, 'lane-three')

        assert fanout_conflicts(repo, ['lane-one', 'lane-two', 'lane-three']) == []
        require_disjoint_worktrees(repo, ['lane-one', 'lane-two', 'lane-three'])

    def test_the_branch_checked_out_in_the_MAIN_checkout_is_a_legitimate_home(self, repo):
        """The main checkout is a working tree like any other -- exactly one branch may live in it,
        and this is why `all_worktrees` must not drop it the way `worktrees` does."""
        _proper_lane(repo, 'lane-one')

        assert fanout_conflicts(repo, ['main', 'lane-one']) == []

    def test_two_lanes_cannot_BOTH_claim_the_main_checkout(self, repo):
        """The converse of the test above, and it is the measured shape once more: a second branch
        cut in the main checkout evicts the first, which then has no tree at all."""
        _git(repo, 'checkout', '-q', '-b', 'lane-one')

        assert fanout_conflicts(repo, ['main', 'lane-one']), 'main was evicted and nothing said so'


class TestWhatTheGuardDeliberatelyDoesNotAnswer:
    def test_a_branch_that_does_not_exist_is_reported_as_treeless_and_not_as_a_typo(self, repo):
        """Whether a name was spelled right is a different question with a different remedy. Folding
        the two would make the refusal ambiguous at the moment somebody has to act on it -- but the
        guard must still not pass it, because a name with no tree has no tree."""
        assert fanout_conflicts(repo, ['never-created'])

    def test_a_DETACHED_worktree_is_not_a_lane_and_does_not_claim_a_branch(self, repo):
        """`fanout.md`'s probe worktrees are `--detach` on purpose; they must not be mistaken for a
        lane holding a branch."""
        probe = repo.parent / 'probe'
        _git(repo, 'worktree', 'add', '-q', '--detach', str(probe), 'main')
        _proper_lane(repo, 'lane-one')

        assert ('detached' in [branch for _path, branch in all_worktrees(repo)]) is True
        assert fanout_conflicts(repo, ['lane-one']) == []


def test_worktrees_still_excludes_the_main_checkout(repo):
    """THE CONTROL FOR THE REFACTOR. `worktrees` was the whole listing minus its first entry, and the
    pruning path depends on that exclusion -- without it the repository itself becomes a prune
    candidate. Splitting `all_worktrees` out must not have changed what `worktrees` answers."""
    _proper_lane(repo, 'lane-one')

    listed = all_worktrees(repo)
    assert listed[0][0].resolve() == repo.resolve()
    assert [(p.resolve(), b) for p, b in worktrees(repo)] == [(p.resolve(), b) for p, b in listed[1:]]
    assert repo.resolve() not in [p.resolve() for p, _b in worktrees(repo)]
