"""A bump that does not happen must fail the push, loudly, naming why.

THE SHAPE BEING REFUSED. motronics' `bump-version.sh` is best-effort: every path exits 0 so tagging
can never block a code push, and a failed tag push prints "will retry next push". pre-commit
DISCARDS a passing hook's output, so on that design "the tag could not be created" and "the tag was
created and published" have the same observable outcome -- a successful push. A guard reporting into
a discarded stdout is indistinguishable from no guard, and here the thing it fails to guard is the
one property SCM versioning was adopted for: that the next commit reinstalls as a different version.

Each test below drives `tools/bump-version.sh` against a real synthetic repository with a real bare
remote, and asserts the EFFECT (which refs the remote holds) rather than only the exit code.

WHAT THE HOOK IS ASKED, AND HOW A TEST CAN BE WRONG ABOUT IT. A raw `.git/hooks/pre-push` gets one
line per ref on stdin. This script never does: pre-commit's pre-push hook-impl consumes stdin itself
and hands `entry` scripts an empty one, exporting `PRE_COMMIT_REMOTE_BRANCH` and friends instead.

A previous version of this file drove the script by stdin, and by a positional argument before that.
Both passed. Both were asking a question the production entry point never asks, so the suite stayed
green while a real `<topic>:main` push minted nothing -- twice, for two different reasons. The unit
tests below therefore set the environment pre-commit actually sets, and
`test_a_real_pre_commit_push_of_a_topic_branch_onto_main_mints_a_real_tag` closes the loop by
installing pre-commit for real and pushing for real, so no future rewrite of "what the hook reads"
can be green here and dead in production.
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


def _run_bump(
    repo: Path,
    *,
    remote: str | None = 'origin',
    remote_branch: str | None = 'refs/heads/main',
    local_branch: str | None = 'refs/heads/main',
    to_ref: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run the hook exactly as pre-commit runs it: no args, empty stdin, PRE_COMMIT_* exported.

    `None` for any of these means "pre-commit did not export it", which is a distinct case from the
    empty string and is what the refusal tests need. `to_ref=None` is the ordinary shape for a first
    push into an empty remote, so it is also this helper's default.
    """
    env = {
        # A clean, non-interactive git: no credential prompt may turn an unreachable remote into a
        # hang, because the test asserting a LOUD failure would then simply time out.
        **os.environ,
        'GIT_TERMINAL_PROMPT': '0',
        'GIT_ASKPASS': 'echo',
    }
    for name, value in (
        ('PRE_COMMIT_REMOTE_NAME', remote),
        ('PRE_COMMIT_REMOTE_BRANCH', remote_branch),
        ('PRE_COMMIT_LOCAL_BRANCH', local_branch),
        ('PRE_COMMIT_TO_REF', to_ref),
    ):
        env.pop(name, None)
        if value is not None:
            env[name] = value

    return subprocess.run(
        ['bash', str(BUMP)],
        cwd=repo,
        input='',  # pre-commit's pre-push impl consumes git's ref lines; entry scripts get nothing.
        capture_output=True,
        text=True,
        timeout=TIMEOUT_S,
        env=env,
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


def _remote_branches(bare: Path) -> list[str]:
    out = subprocess.run(
        ['git', 'branch', '--list', '--format=%(refname)'],
        cwd=bare,
        capture_output=True,
        text=True,
        timeout=TIMEOUT_S,
        check=True,
    )
    return out.stdout.split()


def test_the_first_push_mints_v0_1_0_and_the_next_one_moves_it(repo_with_remote: tuple[Path, Path]) -> None:
    """The happy path, and the documented answer to "this repo has zero tags"."""
    repo, bare = repo_with_remote

    first = _run_bump(repo)
    assert first.returncode == 0, first.stderr
    assert _remote_tags(bare) == ['v0.1.0'], 'the first tag must be v0.1.0 -- where the hardcoded string left off'

    (repo / 'b.txt').write_text('more\n', encoding='utf-8')
    _git(repo, 'add', 'b.txt')
    _git(repo, 'commit', '--quiet', '-m', 'feat: more')

    second = _run_bump(repo, to_ref=_git(repo, 'rev-parse', 'HEAD'))
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

    result = _run_bump(repo)

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

    result = _run_bump(repo)

    assert result.stderr.strip(), 'the refusal produced no stderr at all'
    assert 'REFUSED' not in result.stdout


def test_a_topic_branch_mints_no_release_tag_but_still_says_so(repo_with_remote: tuple[Path, Path]) -> None:
    """A feature branch must not pollute the tag namespace -- and the no-op must be audible.

    Silence here would be the same defect in the other direction: an operator who believes a bump
    happened on a branch where it structurally cannot.
    """
    repo, bare = repo_with_remote
    _git(repo, 'checkout', '--quiet', '-b', 'some-feature')

    result = _run_bump(
        repo,
        remote_branch='refs/heads/some-feature',
        local_branch='refs/heads/some-feature',
    )

    assert result.returncode == 0, result.stderr
    assert _remote_tags(bare) == []
    assert _git(repo, 'tag', '--list') == ''
    assert 'some-feature' in result.stderr and 'no tag minted' in result.stderr


def test_a_topic_branch_pushed_ONTO_main_mints_the_release(repo_with_remote: tuple[Path, Path]) -> None:
    """THE DEFECT. `git push origin <topic>:main` IS a release; the integrator's own branch name is
    not a fact about it.

    Against either superseded logic -- comparing `git rev-parse --abbrev-ref HEAD` to the default
    branch, or reading git's ref lines from a stdin pre-commit never forwards -- this reds on the tag
    assertion: the hook exits 0 having minted nothing. Nothing about the exit code distinguishes the
    three versions of the script, so this asserts the remote's refs.
    """
    repo, bare = repo_with_remote
    _git(repo, 'checkout', '--quiet', '-b', 'integrate-planes')
    (repo / 'c.txt').write_text('integrated\n', encoding='utf-8')
    _git(repo, 'add', 'c.txt')
    _git(repo, 'commit', '--quiet', '-m', 'feat: integrated')
    head = _git(repo, 'rev-parse', 'HEAD')

    result = _run_bump(repo, local_branch='refs/heads/integrate-planes')

    assert result.returncode == 0, result.stderr
    assert _remote_tags(bare) == ['v0.1.0'], 'a push that advances the trunk minted no version. stderr was: ' + repr(
        result.stderr
    )
    # The version belongs to the commit the trunk was moved TO, not to whatever HEAD happened to be.
    assert _git(repo, 'rev-list', '-1', 'v0.1.0') == head
    # The hook publishes the TAG ONLY -- moving the branch here is what got the outer push rejected.
    # Both halves are asserted together in the end-to-end test, which performs a real outer push.
    assert _remote_branches(bare) == [], "the hook moved the trunk; that is the outer push's job"


def test_the_tagged_commit_is_the_one_being_pushed_not_head(repo_with_remote: tuple[Path, Path]) -> None:
    """PRE_COMMIT_TO_REF, when present, is the release -- and it need not be HEAD. An integrator can
    push `<sha>:main` from a checkout sitting somewhere else entirely; tagging HEAD there would
    version a commit the trunk never received."""
    repo, bare = repo_with_remote
    released = _git(repo, 'rev-parse', 'HEAD')
    _git(repo, 'checkout', '--quiet', '-b', 'elsewhere')
    (repo / 'd.txt').write_text('unrelated\n', encoding='utf-8')
    _git(repo, 'add', 'd.txt')
    _git(repo, 'commit', '--quiet', '-m', 'feat: unrelated')
    assert _git(repo, 'rev-parse', 'HEAD') != released

    result = _run_bump(repo, local_branch='refs/heads/elsewhere', to_ref=released)

    assert result.returncode == 0, result.stderr
    assert _remote_tags(bare) == ['v0.1.0']
    assert _git(repo, 'rev-list', '-1', 'v0.1.0') == released, 'the tag landed on HEAD, not on the pushed commit'


def test_a_push_touching_no_default_branch_ref_at_all_is_quiet(repo_with_remote: tuple[Path, Path]) -> None:
    """Standing ON main but pushing only a topic branch: still not a release, and a quiet decline
    rather than a refusal -- the question WAS answerable, and the answer was no."""
    repo, bare = repo_with_remote
    _git(repo, 'branch', 'side')

    result = _run_bump(repo, remote_branch='refs/heads/side', local_branch='refs/heads/side')

    assert result.returncode == 0, result.stderr
    assert _remote_tags(bare) == [], 'a topic-branch push minted a release tag'
    assert _git(repo, 'tag', '--list') == ''
    assert 'refs/heads/side' in result.stderr, 'the decline must name the ref it saw'


def test_an_unanswerable_push_blocks_rather_than_declining(repo_with_remote: tuple[Path, Path]) -> None:
    """ "I CANNOT TELL" IS NOT "NO RELEASE". If pre-commit stops exporting the remote ref -- a version
    bump, a rename, a different invocation -- the script must not read that as "not a release". That
    silent reading is the entire defect being fixed here: it is indistinguishable, from the
    operator's side, from a release correctly skipped, and it shipped three untagged pushes.

    Delete the guard and this reds: the run falls through to a quiet exit 0.
    """
    repo, bare = repo_with_remote

    result = _run_bump(repo, remote_branch=None)

    assert result.returncode != 0, 'an undecidable push was waved through. stderr was: ' + repr(result.stderr)
    assert 'REFUSED' in result.stderr
    assert 'PRE_COMMIT_REMOTE_BRANCH' in result.stderr, 'the refusal must name what was missing'
    assert _remote_tags(bare) == []


def test_an_unknown_remote_blocks_rather_than_guessing_origin(repo_with_remote: tuple[Path, Path]) -> None:
    """The same argument one field over. Defaulting to 'origin' would publish a tag to whichever
    forge happens to be called that -- a wrong-forge publish is worse than a blocked push."""
    repo, bare = repo_with_remote

    result = _run_bump(repo, remote=None)

    assert result.returncode != 0, result.stderr
    assert 'PRE_COMMIT_REMOTE_NAME' in result.stderr
    assert _remote_tags(bare) == []


def test_a_release_whose_commit_cannot_be_resolved_blocks(repo_with_remote: tuple[Path, Path]) -> None:
    """The release ref IS being updated, but neither TO_REF nor the local ref answers which commit.
    That is a release that cannot be versioned, so it blocks -- the property this script exists for."""
    repo, bare = repo_with_remote

    result = _run_bump(repo, local_branch='refs/heads/no-such-branch', to_ref=None)

    assert result.returncode != 0, result.stderr
    assert 'REFUSED' in result.stderr
    assert _remote_tags(bare) == []


def test_an_unpublishable_release_from_a_topic_branch_still_fails_loudly(
    repo_with_remote: tuple[Path, Path],
) -> None:
    """The loud-failure property must survive the change of what decides. A push that SHOULD have
    minted and could not is still a blocked push, whatever branch the integrator stood on."""
    repo, _bare = repo_with_remote
    _git(repo, 'checkout', '--quiet', '-b', 'integrate-planes')
    _git(repo, 'remote', 'set-url', 'origin', str(repo.parent / 'no-such-repo.git'))

    result = _run_bump(repo, local_branch='refs/heads/integrate-planes')

    assert result.returncode != 0, result.stderr
    assert 'REFUSED' in result.stderr and 'v0.1.0' in result.stderr
    assert _git(repo, 'tag', '--list') == '', 'an unpublishable tag must not be left behind locally'


def test_an_already_tagged_head_is_published_rather_than_bumped_again(repo_with_remote: tuple[Path, Path]) -> None:
    """One commit, one version. A second tag on the same commit would make the version ambiguous."""
    repo, bare = repo_with_remote
    _git(repo, 'tag', '-a', 'v0.4.0', '-m', 'Release v0.4.0')

    result = _run_bump(repo)

    assert result.returncode == 0, result.stderr
    assert _remote_tags(bare) == ['v0.4.0']
    assert _git(repo, 'tag', '--list') == 'v0.4.0'


def test_a_non_fast_forward_release_refuses_before_minting(repo_with_remote: tuple[Path, Path]) -> None:
    """The precondition that replaced the atomic push. If the remote trunk is not an ancestor of the
    commit being released, the outer push is about to be rejected as a non-fast-forward -- so minting
    now would publish a version for a commit that never becomes the trunk, and the tag would outlive
    the push that failed.

    Remove the ancestry check and this reds: v0.1.0 lands on the remote for a commit the trunk never
    receives.
    """
    repo, bare = repo_with_remote
    # Give the remote a trunk on an unrelated history, so nothing local descends from it.
    other = repo.parent / 'other'
    other.mkdir()
    _git(other, 'init', '--quiet', '--initial-branch=main')
    (other / 'z.txt').write_text('theirs\n', encoding='utf-8')
    _git(other, 'add', 'z.txt')
    _git(other, 'commit', '--quiet', '-m', 'feat: theirs')
    _git(other, 'remote', 'add', 'origin', str(bare))
    _git(other, 'push', '--quiet', 'origin', 'main')

    result = _run_bump(repo)

    assert result.returncode != 0, 'a non-fast-forward release was tagged anyway. stderr: ' + repr(result.stderr)
    assert 'REFUSED' in result.stderr
    assert _remote_tags(bare) == [], 'a version was published for a commit that will not become the trunk'
    assert _git(repo, 'tag', '--list') == ''


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


# --------------------------------------------------------------------------------------------------
# THE END-TO-END ACCEPTANCE. Everything above sets PRE_COMMIT_* by hand, which is a claim ABOUT the
# entry point rather than a use of it -- and a wrong claim there is exactly how two green suites hid
# a hook that minted nothing. This one installs pre-commit for real and pushes for real, so the only
# thing it trusts about the invocation is pre-commit itself.
# --------------------------------------------------------------------------------------------------

PRE_COMMIT_CONFIG = """\
repos:
  - repo: local
    hooks:
      - id: bump-version
        name: mint and publish the next version tag
        entry: bash {entry}
        language: system
        always_run: true
        pass_filenames: false
        stages: [pre-push]
        verbose: true
"""


def test_a_real_pre_commit_push_of_a_topic_branch_onto_main_mints_a_real_tag(tmp_path: Path) -> None:
    """THE ACCEPTANCE: `git push origin <topic>:main`, through a real installed pre-push hook, must
    leave a real tag on a real remote.

    This is the only test in the file that would have caught the stdin version, because it is the
    only one that does not decide for itself what the hook is handed. It pins a single config key --
    just this hook, by absolute path -- so it says nothing about the repo's other hooks.
    """
    pre_commit = shutil.which('pre-commit')
    if pre_commit is None:
        pytest.skip('pre-commit is not on PATH; the end-to-end path needs the real runner')

    bare = tmp_path / 'remote.git'
    subprocess.run(['git', 'init', '--quiet', '--bare', str(bare)], check=True, timeout=TIMEOUT_S)
    repo = tmp_path / 'work'
    repo.mkdir()
    _git(repo, 'init', '--quiet', '--initial-branch=main')
    (repo / '.pre-commit-config.yaml').write_text(PRE_COMMIT_CONFIG.format(entry=BUMP.as_posix()), encoding='utf-8')
    _git(repo, 'add', '.pre-commit-config.yaml')
    _git(repo, 'commit', '--quiet', '-m', 'feat: base')
    _git(repo, 'remote', 'add', 'origin', str(bare))
    subprocess.run(
        [pre_commit, 'install', '--hook-type', 'pre-push'],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=TIMEOUT_S,
        check=True,
    )

    _git(repo, 'checkout', '--quiet', '-b', 'integrate-planes')
    (repo / 'c.txt').write_text('integrated\n', encoding='utf-8')
    _git(repo, 'add', 'c.txt')
    _git(repo, 'commit', '--quiet', '-m', 'feat: integrated')
    released = _git(repo, 'rev-parse', 'HEAD')

    push = subprocess.run(
        ['git', *GIT_IDENTITY, 'push', 'origin', 'integrate-planes:main'],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=TIMEOUT_S,
        env={**os.environ, 'GIT_TERMINAL_PROMPT': '0', 'GIT_ASKPASS': 'echo'},
        check=False,
    )

    assert push.returncode == 0, (
        'the push itself failed. Publishing the branch from inside the hook moves the ref out from '
        f"under the outer push's compare-and-swap and it is rejected. stderr:\n{push.stderr}"
    )
    assert _remote_tags(bare) == ['v0.1.0'], (
        'a real topic-branch-onto-main push minted no tag. The hook reported success and did '
        f'nothing -- the defect this file exists for. Hook output was:\n{push.stderr}'
    )
    assert _git(bare, 'rev-parse', 'refs/tags/v0.1.0^{}') == released
    assert _git(bare, 'rev-parse', 'refs/heads/main') == released

    # THE SECOND PUSH, and the one that matters most: the trunk now EXISTS, so git's outer update is
    # a compare-and-swap rather than a create. This is the shape of every real integration push, and
    # it is the one that failed -- `cannot lock ref 'refs/heads/main': is at <new> but expected
    # <old>` -- while the first push failed differently ("reference already exists"). Covering only
    # the create would have declared this fixed on the strength of the rarer case.
    (repo / 'e.txt').write_text('again\n', encoding='utf-8')
    _git(repo, 'add', 'e.txt')
    _git(repo, 'commit', '--quiet', '-m', 'feat: again')
    released2 = _git(repo, 'rev-parse', 'HEAD')

    push2 = subprocess.run(
        ['git', *GIT_IDENTITY, 'push', 'origin', 'integrate-planes:main'],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=TIMEOUT_S,
        env={**os.environ, 'GIT_TERMINAL_PROMPT': '0', 'GIT_ASKPASS': 'echo'},
        check=False,
    )

    assert push2.returncode == 0, f'the second integration push failed. stderr:\n{push2.stderr}'
    assert sorted(_remote_tags(bare)) == ['v0.1.0', 'v0.1.1'], push2.stderr
    assert _git(bare, 'rev-parse', 'refs/tags/v0.1.1^{}') == released2
    assert _git(bare, 'rev-parse', 'refs/heads/main') == released2
