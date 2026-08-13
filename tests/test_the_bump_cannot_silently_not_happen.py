"""A bump that does not happen must fail the push, loudly, naming why.

THE SHAPE BEING REFUSED. motronics' `bump-version.sh` is best-effort: every path exits 0 so tagging
can never block a code push, and a failed tag push prints "will retry next push". pre-commit
DISCARDS a passing hook's output, so on that design "the tag could not be created" and "the tag was
created and published" have the same observable outcome -- a successful push. A guard reporting into
a discarded stdout is indistinguishable from no guard, and here the thing it fails to guard is the
one property SCM versioning was adopted for: that the next commit reinstalls as a different version.

Each test below drives `tools/bump-version.sh` against a real synthetic repository with a real bare
remote, and asserts the EFFECT (which refs the remote holds) rather than only the exit code.

WHAT THE HOOK IS ASKED. git feeds a pre-push hook one line per ref on stdin --
`<local ref> <local sha> <remote ref> <remote sha>` -- and pre-commit passes it through. So every
test here supplies real stdin: driving the script with only a positional remote would be asking it a
question git never asks, and it was exactly that gap (deciding on the LOCAL branch instead) that let
`git push origin <topic>:main` report `Passed` against a remote holding zero tags.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

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


ZERO = '0' * 40


def _run_bump(repo: Path, remote: str, *ref_lines: str) -> subprocess.CompletedProcess[str]:
    """Run the hook the way git and pre-commit run it: remote named, ref lines on stdin."""
    return subprocess.run(
        ['bash', str(BUMP), remote],
        cwd=repo,
        input=''.join(f'{line}\n' for line in ref_lines),
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


def _pushing(repo: Path, local_ref: str, remote_ref: str = 'refs/heads/main') -> str:
    """The stdin line git writes for `git push <remote> <local_ref>:<remote_ref>`."""
    return f'{local_ref} {_git(repo, "rev-parse", local_ref)} {remote_ref} {ZERO}'


def _remote_tags(bare: Path) -> list[str]:
    out = subprocess.run(
        ['git', 'tag', '--list'], cwd=bare, capture_output=True, text=True, timeout=TIMEOUT_S, check=True
    )
    return out.stdout.split()


def test_the_first_push_mints_v0_1_0_and_the_next_one_moves_it(repo_with_remote: tuple[Path, Path]) -> None:
    """The happy path, and the documented answer to "this repo has zero tags"."""
    repo, bare = repo_with_remote

    first = _run_bump(repo, 'origin', _pushing(repo, 'refs/heads/main'))
    assert first.returncode == 0, first.stderr
    assert _remote_tags(bare) == ['v0.1.0'], 'the first tag must be v0.1.0 -- where the hardcoded string left off'

    (repo / 'b.txt').write_text('more\n', encoding='utf-8')
    _git(repo, 'add', 'b.txt')
    _git(repo, 'commit', '--quiet', '-m', 'feat: more')

    second = _run_bump(repo, 'origin', _pushing(repo, 'refs/heads/main'))
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
    line = _pushing(repo, 'refs/heads/main')
    _git(repo, 'remote', 'set-url', 'origin', str(repo.parent / 'no-such-repo.git'))

    result = _run_bump(repo, 'origin', line)

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
    line = _pushing(repo, 'refs/heads/main')
    _git(repo, 'remote', 'set-url', 'origin', str(repo.parent / 'no-such-repo.git'))

    result = _run_bump(repo, 'origin', line)

    assert result.stderr.strip(), 'the refusal produced no stderr at all'
    assert 'REFUSED' not in result.stdout


def test_a_topic_branch_mints_no_release_tag_but_still_says_so(repo_with_remote: tuple[Path, Path]) -> None:
    """A feature branch must not pollute the tag namespace -- and the no-op must be audible.

    Silence here would be the same defect in the other direction: an operator who believes a bump
    happened on a branch where it structurally cannot.
    """
    repo, bare = repo_with_remote
    _git(repo, 'checkout', '--quiet', '-b', 'some-feature')

    result = _run_bump(repo, 'origin', _pushing(repo, 'refs/heads/some-feature', 'refs/heads/some-feature'))

    assert result.returncode == 0, result.stderr
    assert _remote_tags(bare) == []
    assert _git(repo, 'tag', '--list') == ''
    assert 'some-feature' in result.stderr and 'no tag minted' in result.stderr


def test_a_topic_branch_pushed_ONTO_main_mints_the_release(repo_with_remote: tuple[Path, Path]) -> None:
    """THE DEFECT. `git push origin <topic>:main` IS a release; the integrator's own branch name is
    not a fact about it.

    Against the old logic -- `if $(git rev-parse --abbrev-ref HEAD) != main: exit 0` -- this reds on
    the tag assertion: the hook exits 0 having minted nothing, which is exactly what three pushes did
    tonight while the remote held zero tags. Nothing about the exit code distinguishes the two
    versions of the script, so this test asserts the remote's refs.
    """
    repo, bare = repo_with_remote
    _git(repo, 'checkout', '--quiet', '-b', 'integrate-planes')
    (repo / 'c.txt').write_text('integrated\n', encoding='utf-8')
    _git(repo, 'add', 'c.txt')
    _git(repo, 'commit', '--quiet', '-m', 'feat: integrated')
    head = _git(repo, 'rev-parse', 'HEAD')

    result = _run_bump(repo, 'origin', _pushing(repo, 'refs/heads/integrate-planes'))

    assert result.returncode == 0, result.stderr
    assert _remote_tags(bare) == ['v0.1.0'], 'a push that advances the trunk minted no version. stderr was: ' + repr(
        result.stderr
    )
    # The version belongs to the commit the trunk was moved TO, not to whatever HEAD happened to be.
    assert _git(repo, 'rev-list', '-1', 'v0.1.0') == head
    # And the branch half of the atomic publish really moved the remote trunk.
    assert _git(bare, 'rev-parse', 'refs/heads/main') == head


def test_only_the_ref_that_updates_main_decides_among_several(repo_with_remote: tuple[Path, Path]) -> None:
    """A push may carry several refs at once. The others are irrelevant, not disqualifying."""
    repo, bare = repo_with_remote
    _git(repo, 'branch', 'side')
    _git(repo, 'checkout', '--quiet', '-b', 'integrate-planes')
    (repo / 'c.txt').write_text('integrated\n', encoding='utf-8')
    _git(repo, 'add', 'c.txt')
    _git(repo, 'commit', '--quiet', '-m', 'feat: integrated')
    head = _git(repo, 'rev-parse', 'HEAD')

    result = _run_bump(
        repo,
        'origin',
        _pushing(repo, 'refs/heads/side', 'refs/heads/side'),
        _pushing(repo, 'refs/heads/integrate-planes'),
        f'refs/tags/some-note {_git(repo, "rev-parse", "HEAD")} refs/tags/some-note {ZERO}',
    )

    assert result.returncode == 0, result.stderr
    assert _remote_tags(bare) == ['v0.1.0']
    assert _git(repo, 'rev-list', '-1', 'v0.1.0') == head


def test_deleting_the_default_branch_mints_nothing(repo_with_remote: tuple[Path, Path]) -> None:
    """`git push origin :main` carries an all-zero LOCAL sha. There is no commit to version, so a
    tag here would point a release at nothing -- and it must be a quiet no-op, not a refusal, since
    the deletion itself is a legitimate thing to push."""
    repo, bare = repo_with_remote

    result = _run_bump(repo, 'origin', f'(delete) {ZERO} refs/heads/main {_git(repo, "rev-parse", "HEAD")}')

    assert result.returncode == 0, result.stderr
    assert _remote_tags(bare) == []
    assert _git(repo, 'tag', '--list') == ''
    assert 'DELETES' in result.stderr


def test_a_push_touching_no_default_branch_ref_at_all_is_quiet(repo_with_remote: tuple[Path, Path]) -> None:
    """Standing ON main but pushing only a topic branch: still not a release. This is the case the
    old logic got WRONG in the other direction -- it would have minted a tag."""
    repo, bare = repo_with_remote
    _git(repo, 'branch', 'side')

    result = _run_bump(repo, 'origin', _pushing(repo, 'refs/heads/side', 'refs/heads/side'))

    assert result.returncode == 0, result.stderr
    assert _remote_tags(bare) == [], 'a topic-branch push minted a release tag'
    assert _git(repo, 'tag', '--list') == ''
    assert 'refs/heads/side' in result.stderr, 'the decline must name the refs it saw'


def test_an_unpublishable_release_from_a_topic_branch_still_fails_loudly(
    repo_with_remote: tuple[Path, Path],
) -> None:
    """The loud-failure property must survive the change of what decides. A push that SHOULD have
    minted and could not is still a blocked push, whatever branch the integrator stood on."""
    repo, _bare = repo_with_remote
    _git(repo, 'checkout', '--quiet', '-b', 'integrate-planes')
    line = _pushing(repo, 'refs/heads/integrate-planes')
    _git(repo, 'remote', 'set-url', 'origin', str(repo.parent / 'no-such-repo.git'))

    result = _run_bump(repo, 'origin', line)

    assert result.returncode != 0, result.stderr
    assert 'REFUSED' in result.stderr and 'v0.1.0' in result.stderr
    assert _git(repo, 'tag', '--list') == '', 'an unpublishable tag must not be left behind locally'


def test_an_already_tagged_head_is_published_rather_than_bumped_again(repo_with_remote: tuple[Path, Path]) -> None:
    """One commit, one version. A second tag on the same commit would make the version ambiguous."""
    repo, bare = repo_with_remote
    _git(repo, 'tag', '-a', 'v0.4.0', '-m', 'Release v0.4.0')

    result = _run_bump(repo, 'origin', _pushing(repo, 'refs/heads/main'))

    assert result.returncode == 0, result.stderr
    assert _remote_tags(bare) == ['v0.4.0']
    assert _git(repo, 'tag', '--list') == 'v0.4.0'


def test_the_declined_case_is_actually_shown_to_the_operator() -> None:
    """stderr ALONE IS NOT VISIBILITY. pre-commit discards a passing hook's output, and the hook's
    commonest passing outcome is a decline -- so without `verbose: true` on this hook the operator
    reads `Passed` and cannot tell "correctly declined" from "did nothing". That is the whole reason
    three topic-branch pushes looked successful while the remote gained zero tags.

    This asserts the MECHANISM that makes the message reach a reader, not the message itself; a test
    that only checked stderr would pass on the invisible configuration too.
    """
    config = yaml.safe_load((REPO_ROOT / '.pre-commit-config.yaml').read_text(encoding='utf-8'))
    hooks = [h for repo in config['repos'] for h in repo['hooks'] if h['id'] == 'bump-version']

    assert len(hooks) == 1, 'searched every hook in .pre-commit-config.yaml'
    assert hooks[0].get('verbose') is True, (
        "bump-version's decline goes to stderr, which pre-commit swallows for a passing hook. "
        'Without verbose: true the no-op is unobservable.'
    )
