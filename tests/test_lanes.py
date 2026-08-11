"""Lane properties: what a fresh worktree must be given, and what forbids deleting one.

PROVENANCE. Extracted from motronics' `scripts/lanes/new_lane.py` and `prune_lanes.py`, both of them
written in answer to a measured incident rather than to a design. The incidents are named in the
module docstring; these tests pin what came out of them.

THESE RUN AGAINST REAL GIT. A double would be better behaved than git is -- it would not have the
`--show-toplevel`-inside-a-worktree behaviour that once offered the repository itself as a prune
candidate, and that behaviour is the reason two of these functions exist. So each test builds a real
repository in `tmp_path` and adds real worktrees to it.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import time
from pathlib import Path

import pytest

from agent_swarm import lanes
from agent_swarm.lanes import (
    LaneError,
    create_lane,
    idle_minutes,
    ignored_files,
    main_checkout_root,
    reasons_to_keep,
    related_notes,
    seed_lane,
    survey,
    unmerged_commits,
    worktrees,
)

#: The process census is the ONE factual protection a lane has, and it needs psutil. Without it
#: `occupants` answers `None` -- "I could not look" -- which every keep-reason test would then see
#: instead of the reason it is exercising. So the fixture asserts the instrument first rather than
#: letting the suite report a green that means "the probe was blind".
_CAN_SEE_PROCESSES = lanes.procs.HAVE_PSUTIL


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        [lanes.GIT, '-C', str(repo), *args], capture_output=True, text=True, check=True, encoding='utf-8'
    )
    return done.stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A real repository with one commit on `main` and a gitignored, generated file.

    The ignore rules are part of the fixture because the whole seeding mechanism exists for files git
    will NOT put in a worktree, and a fixture without them could not exercise it.
    """
    root = tmp_path / 'repo'
    root.mkdir()
    _git(root, 'init', '-b', 'main')
    _git(root, 'config', 'user.email', 'lane@example.com')
    _git(root, 'config', 'user.name', 'Lane Test')
    (root / '.gitignore').write_text('generated/\nlocal.toml\n', encoding='utf-8')
    (root / 'tracked.txt').write_text('tracked\n', encoding='utf-8')
    _git(root, 'add', '.')
    _git(root, 'commit', '-m', 'first')
    (root / 'local.toml').write_text('key = 1\n', encoding='utf-8')
    (root / 'generated').mkdir()
    (root / 'generated' / 'version.py').write_text("__version__ = '1.2.3'\n", encoding='utf-8')
    return root


class TestTheSeedingListIsDATA:
    """WHICH gitignored files a lane cannot run without is a fact about a PROJECT. Every list of them
    this code has ever seen named one project's files, and it was invisible because it worked.
    """

    def test_the_seeding_list_is_REQUIRED(self, repo, tmp_path):
        with pytest.raises(TypeError):
            seed_lane(repo, tmp_path / 'lane')  # type: ignore[call-arg]
        with pytest.raises(TypeError):
            create_lane(repo, 'l', base='main')  # type: ignore[call-arg]

    def test_an_EMPTY_list_seeds_NOTHING_rather_than_triggering_a_default(self, repo, tmp_path):
        """THE DISCRIMINATING CASE. A module that fell back to a built-in list when given nothing
        would pass every other test here and still be the coupling -- and the caller would never see
        it, because the fallback works.
        """
        lane = tmp_path / 'lane'
        lane.mkdir()
        assert seed_lane(repo, lane, []) == 0
        assert list(lane.iterdir()) == []

    def test_a_named_file_is_copied_and_PROVEN(self, repo, tmp_path):
        lane = tmp_path / 'lane'
        assert seed_lane(repo, lane, [Path('local.toml')]) == 1
        assert (lane / 'local.toml').read_text(encoding='utf-8') == 'key = 1\n'

    def test_a_MISSING_required_file_is_a_REFUSAL_naming_it(self, repo, tmp_path):
        """Never a warning. A copy whose failure is discarded is indistinguishable from one that
        worked -- measured 2026-07-25, where a `2>/dev/null` hid exactly this and cost a lane its
        budget chasing a red in a file it never touched.
        """
        with pytest.raises(LaneError, match='absent.toml'):
            seed_lane(repo, tmp_path / 'lane', [Path('absent.toml')])

    def test_an_OPTIONAL_files_absence_is_not_a_refusal(self, repo, tmp_path):
        """Git only lists what is there, so an empty derived list means this checkout has no cached
        artifacts either -- not that seeding failed.
        """
        assert seed_lane(repo, tmp_path / 'lane', [], optional=[Path('never-existed.bin')]) == 0

    def test_an_ignored_DIRECTORY_is_copied_whole(self, repo, tmp_path):
        """Git reports a wholly-ignored directory as ONE entry, not as its members. A version that
        only handled files would seed nothing while looking like it seeded something.
        """
        lane = tmp_path / 'lane'
        assert seed_lane(repo, lane, [], optional=[Path('generated')]) == 1
        assert (lane / 'generated' / 'version.py').is_file()

    def test_the_ignored_list_is_DERIVED_from_gits_own_rules(self, repo):
        """A hand-written list is correct the day it is written and silently wrong the first time a
        directory gains an artifact. Asking git means the two cannot disagree.
        """
        found = {p.as_posix() for p in ignored_files(repo)}
        assert 'local.toml' in found
        assert any(p.startswith('generated') for p in found)


class TestCreatingALane:
    def test_a_lane_is_a_worktree_on_a_branch_of_its_own_NAME(self, repo):
        dest = create_lane(repo, 'w42-subject', base='main', seed=[Path('local.toml')])
        assert dest.is_dir()
        assert (dest / 'tracked.txt').is_file()
        assert (dest / 'local.toml').is_file(), 'the gitignored file git does not carry over'
        assert _git(dest, 'rev-parse', '--abbrev-ref', 'HEAD').strip() == 'w42-subject'

    def test_the_BASE_has_no_default(self, repo):
        """Branching a lane off a stale default branch is its own measured failure: the integration
        branch is ahead of it, and the lane reds in code it never touched. A caller that has not
        decided its base has not decided anything.
        """
        with pytest.raises(TypeError):
            create_lane(repo, 'w43', seed=[])  # type: ignore[call-arg]

    def test_an_EXISTING_destination_is_REFUSED_not_reused(self, repo, tmp_path):
        worktrees_dir = tmp_path / 'wt'
        (worktrees_dir / 'taken').mkdir(parents=True)
        with pytest.raises(LaneError, match='already exists'):
            create_lane(repo, 'taken', base='main', seed=[], worktrees_dir=worktrees_dir)

    def test_an_UNKNOWN_base_is_REFUSED_with_gits_own_words(self, repo):
        with pytest.raises(LaneError, match='worktree add'):
            create_lane(repo, 'w44', base='no-such-branch', seed=[])

    def test_creating_FROM_a_lane_is_refused(self, repo):
        """`--show-toplevel` answers with whatever checkout you stand in, so this would nest the new
        worktree INSIDE the lane. The first revision of that function did exactly that in its own
        smoke test and was stopped only by a branch name that already existed.
        """
        lane = create_lane(repo, 'w45', base='main', seed=[])
        assert main_checkout_root(repo).resolve() == repo.resolve()
        with pytest.raises(LaneError, match='is a worktree, not the main checkout'):
            main_checkout_root(lane)

    def test_outside_a_repository_it_refuses_rather_than_guessing(self, tmp_path):
        outside = tmp_path / 'not-a-repo'
        outside.mkdir()
        with pytest.raises(LaneError, match='not inside a git repository'):
            main_checkout_root(outside)


class TestPriorArt:
    """A correct finding nobody retrieves is worth what no finding is worth. Lane creation is the
    only moment that is both before any work and already holds a description of the work.
    """

    @pytest.fixture
    def notes(self, tmp_path: Path) -> Path:
        root = tmp_path / 'notes' / '2026' / '08'
        root.mkdir(parents=True)
        for name in (
            'finding-the-heartbeat-was-never-stamped.md',
            'finding-the-mesh-adapt-size-field.md',
            'plan-the-thread-watchdog.md',
            '_index.md',
        ):
            (root / name).write_text('body\n', encoding='utf-8')
        return tmp_path / 'notes'

    def test_a_shared_LONG_word_is_a_hit(self, notes):
        hits = related_notes('w51-thread-pinning', notes)
        assert [p.name for _s, p, _sh in hits] == ['plan-the-thread-watchdog.md']

    def test_a_shared_SHORT_word_alone_is_NOT(self, notes):
        """Measured 2026-07-28: `size` (4) matched a mesh note for a lane about a pool size -- a
        coincidence. `thread` (6) was a real hit. One short word is noise, and noise at the top of a
        three-item list evicts the finding that mattered.
        """
        assert related_notes('w52-pool-size', notes) == []

    def test_an_INDEX_file_is_never_offered(self, notes):
        assert all(not p.name.startswith('_') for _s, p, _sh in related_notes('w53-index', notes))

    def test_a_MISSING_notes_directory_is_not_an_error(self, tmp_path):
        """A consumer that keeps no notes is not a misconfiguration."""
        assert related_notes('w54-anything', tmp_path / 'absent') == []

    def test_the_ceiling_holds(self, tmp_path):
        """A retrieval aid with no ceiling becomes a wall, a wall is skipped, and a skipped aid is
        worth what an unread finding is worth.
        """
        root = tmp_path / 'notes'
        root.mkdir()
        for i in range(10):
            (root / f'finding-heartbeat-{i}.md').write_text('x', encoding='utf-8')
        assert len(related_notes('w55-heartbeat', root)) == lanes.MAX_SUGGESTIONS

    def test_the_stoplist_is_the_CONSUMERS_to_extend(self, tmp_path):
        """In a repository whose every note names the product, that word is the emptiest token there
        is -- and it is not this package's to know.
        """
        root = tmp_path / 'notes'
        root.mkdir()
        (root / 'finding-widget-behaviour.md').write_text('x', encoding='utf-8')
        assert related_notes('w56-widget', root)
        assert related_notes('w56-widget', root, stopwords=[*lanes.STOPWORDS, 'widget']) == []


class TestNothingIsDeletedOnAMaybe:
    """Every refusal names itself, and the reader's next move after reading them is a deletion."""

    # Without psutil, `occupants` answers `None` and EVERY lane keeps a reason -- so these would pass
    # for the wrong cause. Skipped rather than tolerated: a green that means "the probe was blind" is
    # the shape this package exists to refuse.
    pytestmark = pytest.mark.skipif(not _CAN_SEE_PROCESSES, reason='the process census needs psutil')

    def test_a_clean_fully_upstream_idle_lane_has_NO_reasons_to_keep(self, repo):
        lane = create_lane(repo, 'w60-clean', base='main', seed=[])
        _age(lane)
        assert reasons_to_keep(lane, upstream='main') == []

    def test_an_UNTRACKED_file_keeps_it(self, repo):
        lane = create_lane(repo, 'w61-untracked', base='main', seed=[])
        (lane / 'scratch.txt').write_text('unfinished\n', encoding='utf-8')
        _age(lane)
        assert any('untracked' in r for r in reasons_to_keep(lane, upstream='main'))

    def test_a_MODIFIED_tracked_file_keeps_it(self, repo):
        lane = create_lane(repo, 'w62-dirty', base='main', seed=[])
        (lane / 'tracked.txt').write_text('edited\n', encoding='utf-8')
        _age(lane)
        assert any('modified' in r for r in reasons_to_keep(lane, upstream='main'))

    def test_a_commit_whose_PATCH_is_not_upstream_keeps_it(self, repo):
        lane = create_lane(repo, 'w63-work', base='main', seed=[])
        (lane / 'tracked.txt').write_text('real work\n', encoding='utf-8')
        _git(lane, 'add', '.')
        _git(lane, 'commit', '-m', 'work')
        _age(lane)
        assert any('NOT in main' in r for r in reasons_to_keep(lane, upstream='main'))

    def test_a_CHERRY_PICKED_commit_is_recognised_as_already_upstream(self, repo):
        """THE MEASURED MISTAKE. Classifying by `merge-base --is-ancestor`, three lanes read as
        unmerged and TWO OF THE THREE were already in, under different SHAs. Ancestry answers "is
        this COMMIT upstream"; the question is "is this CHANGE upstream", and `git cherry` compares
        patch-ids.
        """
        lane = create_lane(repo, 'w64-picked', base='main', seed=[])
        (lane / 'tracked.txt').write_text('picked\n', encoding='utf-8')
        _git(lane, 'add', '.')
        _git(lane, 'commit', '-m', 'to be picked')
        sha = _git(lane, 'rev-parse', 'HEAD').strip()
        _git(repo, 'cherry-pick', sha)
        assert unmerged_commits(lane, upstream='main') == []

    def test_the_UPSTREAM_branch_is_REQUIRED(self, repo):
        """The branch a fan-out integrates into is a fact about the fan-out. A default would compare
        against a branch the work never targeted and report a lane fully merged when none of it is.
        """
        lane = create_lane(repo, 'w65-upstream', base='main', seed=[])
        with pytest.raises(TypeError):
            unmerged_commits(lane)  # type: ignore[call-arg]
        with pytest.raises(TypeError):
            reasons_to_keep(lane)  # type: ignore[call-arg]

    def test_RECENCY_keeps_it_and_says_it_is_a_HEURISTIC(self, repo):
        """Recency is evidence of use, not proof of it -- the only check here that can be wrong in
        the safe direction, which is why it says so where the reader is.
        """
        lane = create_lane(repo, 'w66-fresh', base='main', seed=[])
        reasons = reasons_to_keep(lane, upstream='main', idle_ceiling=60.0)
        assert any('HEURISTIC' in r for r in reasons), reasons

    def test_an_idle_ceiling_of_zero_disables_only_THAT_check(self, repo):
        lane = create_lane(repo, 'w67-off', base='main', seed=[])
        assert reasons_to_keep(lane, upstream='main', idle_ceiling=0) == []

    def test_an_UNANSWERABLE_process_census_REFUSES(self, repo, monkeypatch):
        """``None`` IS NOT AN EMPTY LIST. The version before this returned 0 on every platform it had
        not been written for, so the only FACTUAL protection for a running lane silently evaporated
        -- while the idle check, a documented off switch, was the other one.
        """
        lane = create_lane(repo, 'w68-unknown', base='main', seed=[])
        monkeypatch.setattr(lanes.procs, 'occupants', lambda _tree: None)
        assert any('cannot prove this lane is idle' in r for r in reasons_to_keep(lane, upstream='main'))

    def test_a_lane_IN_USE_is_not_prunable(self, repo, monkeypatch):
        lane = create_lane(repo, 'w69-busy', base='main', seed=[])
        monkeypatch.setattr(lanes.procs, 'occupants', lambda _tree: ['a process'])
        assert any('IN USE' in r for r in reasons_to_keep(lane, upstream='main'))


class TestTheMainCheckoutIsNeverACandidate:
    def test_the_survey_lists_only_LINKED_worktrees(self, repo):
        """It was `rev-parse --show-toplevel` until 2026-07-28, and inside a linked worktree that
        returns THAT worktree -- so the lane you were standing in was excluded and the REPOSITORY was
        offered as a prune candidate.
        """
        create_lane(repo, 'w70-a', base='main', seed=[])
        create_lane(repo, 'w71-b', base='main', seed=[])
        listed = [p.resolve() for p, _b in worktrees(repo)]
        assert repo.resolve() not in listed
        assert len(listed) == 2

    def test_the_survey_reports_a_branch_per_lane(self, repo):
        create_lane(repo, 'w72-named', base='main', seed=[])
        assert [b for _p, b, _r in survey(repo, upstream='main')] == ['w72-named']


class TestIdleMinutesIsMeasuredRELATIVELY:
    def test_a_noise_name_in_the_PREFIX_does_not_prune_the_walk(self, tmp_path):
        """THE WHOLE CORRECTNESS CONTENT. Matching skip names absolutely means any of them appearing
        in the parent path prunes the ENTIRE walk: nothing is found, this returns ``inf``, the lane
        reads as infinitely idle, and a LIVE lane is deleted. Measured 2026-08-05, the identical
        scheme elsewhere scanned nothing and reported 0 of 5 items -- same defect, opposite blast
        radius.
        """
        lane = tmp_path / 'output' / 'worktrees' / 'lane'
        lane.mkdir(parents=True)
        (lane / 'work.txt').write_text('just now\n', encoding='utf-8')
        assert idle_minutes(lane) < 5.0

    def test_noise_directories_INSIDE_the_lane_are_still_skipped(self, tmp_path):
        lane = tmp_path / 'lane'
        (lane / 'output').mkdir(parents=True)
        (lane / 'output' / 'log.txt').write_text('a run wrote this\n', encoding='utf-8')
        assert idle_minutes(lane) == float('inf')


def _age(lane: Path, minutes: float = 120.0) -> None:
    """Backdate every mtime under a lane so the recency HEURISTIC does not mask the other checks.

    A test that let recency fire would pass whether or not the check it means to exercise works --
    one reason on the list is indistinguishable from another to an `assert reasons`.
    """
    stamp = time.time() - minutes * 60.0
    for child in [lane, *lane.rglob('*')]:
        with contextlib.suppress(OSError):
            os.utime(child, (stamp, stamp))
