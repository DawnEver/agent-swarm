"""A bump that does not happen must fail the push, loudly, naming why.

THE SHAPE BEING REFUSED. motronics' `bump-version.sh` is best-effort: every path exits 0 so tagging
can never block a code push, and a failed tag push prints "will retry next push". pre-commit
DISCARDS a passing hook's output, so on that design "the tag could not be created" and "the tag was
created and published" have the same observable outcome -- a successful push. A guard reporting into
a discarded stdout is indistinguishable from no guard, and here the thing it fails to guard is the
one property SCM versioning was adopted for: that the next commit reinstalls as a different version.

Each test below drives `tools/bump-version.sh` against a real synthetic repository with a real bare
remote, and asserts the EFFECT (which refs the remote holds) rather than only the exit code.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BUMP = REPO_ROOT / 'tools' / 'bump-version.sh'

TIMEOUT_S = 60
GIT_IDENTITY = [
    '-c',
    'user.name=test',
    '-c',
    'user.email=test@example.invalid',
    '-c',
    'commit.gpgsign=false',
    '-c',
    'tag.gpgsign=false',
]

pytestmark = pytest.mark.skipif(
    shutil.which('bash') is None,
    reason='tools/bump-version.sh is a bash hook; without bash there is nothing to exercise. This is '
    'an environment capability, not a tolerated failure -- every machine that RUNS the hook has bash.',
)


def _git(repo: Path, *args: str, check: bool = True) -> str:
    done = subprocess.run(
        ['git', *GIT_IDENTITY, *args],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=TIMEOUT_S,
        check=check,
    )
    return done.stdout.strip()


def _run_bump(repo: Path, remote: str) -> subprocess.CompletedProcess[str]:
    """Run the hook the way pre-commit runs it: from the work tree, with the remote named."""
    return subprocess.run(
        ['bash', str(BUMP), remote],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=TIMEOUT_S,
        # A clean, non-interactive git: no credential prompt may turn an unreachable remote into a
        # hang, because the test asserting a LOUD failure would then simply time out.
        env={**os.environ, 'GIT_TERMINAL_PROMPT': '0', 'GIT_ASKPASS': 'echo'},
        check=False,
    )


@pytest.fixture
def repo_with_remote(tmp_path: Path) -> tuple[Path, Path]:
    """A `main` checkout whose `origin` is a real bare repository."""
    bare = tmp_path / 'remote.git'
    subprocess.run(['git', 'init', '--quiet', '--bare', str(bare)], check=True, timeout=TIMEOUT_S)

    repo = tmp_path / 'work'
    repo.mkdir()
    _git(repo, 'init', '--quiet', '--initial-branch=main')
    (repo / 'a.txt').write_text('base\n', encoding='utf-8')
    _git(repo, 'add', 'a.txt')
    _git(repo, 'commit', '--quiet', '-m', 'feat: base')
    _git(repo, 'remote', 'add', 'origin', str(bare))
    return repo, bare


def _remote_tags(bare: Path) -> list[str]:
    out = subprocess.run(
        ['git', 'tag', '--list'], cwd=bare, capture_output=True, text=True, timeout=TIMEOUT_S, check=True
    )
    return out.stdout.split()


def test_the_first_push_mints_v0_1_0_and_the_next_one_moves_it(repo_with_remote: tuple[Path, Path]) -> None:
    """The happy path, and the documented answer to "this repo has zero tags"."""
    repo, bare = repo_with_remote

    first = _run_bump(repo, 'origin')
    assert first.returncode == 0, first.stderr
    assert _remote_tags(bare) == ['v0.1.0'], 'the first tag must be v0.1.0 -- where the hardcoded string left off'

    (repo / 'b.txt').write_text('more\n', encoding='utf-8')
    _git(repo, 'add', 'b.txt')
    _git(repo, 'commit', '--quiet', '-m', 'feat: more')

    second = _run_bump(repo, 'origin')
    assert second.returncode == 0, second.stderr
    assert sorted(_remote_tags(bare)) == ['v0.1.0', 'v0.1.1']


def test_an_unpublishable_tag_fails_the_push_and_leaves_no_local_tag(repo_with_remote: tuple[Path, Path]) -> None:
    """THE DISCRIMINATING TEST. Make publishing impossible; the hook must exit non-zero.

    Revert `tools/bump-version.sh` to the best-effort shape it was modelled on -- every path exits 0
    -- and this reds on the first assertion. That is what separates "the bump is guarded" from "the
    bump says it is guarded".

    The residue half matters too: a tag left behind locally would make the NEXT push believe this
    version was already released and skip straight past it, so the version that could not be
    published would be silently burned.
    """
    repo, _bare = repo_with_remote
    _git(repo, 'remote', 'set-url', 'origin', str(repo.parent / 'no-such-repo.git'))

    result = _run_bump(repo, 'origin')

    assert result.returncode != 0, (
        'the push was allowed through with no published version. The next reinstall of this commit '
        f'would be indistinguishable from the previous one. stderr was: {result.stderr!r}'
    )
    assert 'REFUSED' in result.stderr
    assert 'v0.1.0' in result.stderr, 'the refusal must name the version that could not be published'
    assert _git(repo, 'tag', '--list') == '', 'an unpublishable tag must not be left behind locally'


def test_the_refusal_goes_to_stderr_not_a_discarded_stdout(repo_with_remote: tuple[Path, Path]) -> None:
    """The reason it must be stderr: pre-commit shows a hook's output on failure and swallows it
    otherwise, and the operator's own terminal is where a blocked push is read. A message on stdout
    only would still be visible on THIS failure path -- but the skip and success paths below write
    there too, and those are the ones nobody would ever see."""
    repo, _bare = repo_with_remote
    _git(repo, 'remote', 'set-url', 'origin', str(repo.parent / 'no-such-repo.git'))

    result = _run_bump(repo, 'origin')

    assert result.stderr.strip(), 'the refusal produced no stderr at all'
    assert 'REFUSED' not in result.stdout


def test_a_topic_branch_mints_no_release_tag_but_still_says_so(repo_with_remote: tuple[Path, Path]) -> None:
    """A feature branch must not pollute the tag namespace -- and the no-op must be audible.

    Silence here would be the same defect in the other direction: an operator who believes a bump
    happened on a branch where it structurally cannot.
    """
    repo, bare = repo_with_remote
    _git(repo, 'checkout', '--quiet', '-b', 'some-feature')

    result = _run_bump(repo, 'origin')

    assert result.returncode == 0, result.stderr
    assert _remote_tags(bare) == []
    assert _git(repo, 'tag', '--list') == ''
    assert 'some-feature' in result.stderr and 'no tag minted' in result.stderr


def test_an_already_tagged_head_is_published_rather_than_bumped_again(repo_with_remote: tuple[Path, Path]) -> None:
    """One commit, one version. A second tag on the same commit would make the version ambiguous."""
    repo, bare = repo_with_remote
    _git(repo, 'tag', '-a', 'v0.4.0', '-m', 'Release v0.4.0')

    result = _run_bump(repo, 'origin')

    assert result.returncode == 0, result.stderr
    assert _remote_tags(bare) == ['v0.4.0']
    assert _git(repo, 'tag', '--list') == 'v0.4.0'
