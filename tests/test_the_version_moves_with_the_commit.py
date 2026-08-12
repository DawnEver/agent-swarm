"""The version string is a fact about the commit, and two different commits cannot share one.

MEASURED 2026-08-12 in the consuming project, which is why this file exists rather than a note:
reinstalling this package across a real push produced `- agent-swarm==0.1.0 (...@e77417da...)` and
`+ agent-swarm==0.1.0 (...@3c90de56...)`. Byte-identical versions, genuinely different code. The
consumer keys cached test verdicts on a resolved-environment key, so a verdict earned against one
commit was being served for another.

WHAT THIS FILE REFUSES TO BE. Asserting that `[project] version` is absent and `dynamic` contains
`"version"` is a spelling check: it passes against a config that derives a CONSTANT just as happily
as against one that derives the commit. The property is "two different commits produce two different
strings", and the discriminating case is TWO SIBLING COMMITS off one base -- both the same distance
from the tag, so a scheme carrying only the distance (`local_scheme = "no-local-version"`, the one
plausible wrong turn) gives them the identical string and reds this file. Sequential commits would
not have caught that.

The version is computed with `setuptools_scm` directly rather than by running a build. That is not a
substitute engine: hatch-vcs IS a `setuptools_scm` front end, and `[tool.hatch.version.raw-options]`
is passed to it unchanged -- so this test reads OUR configuration out of pyproject.toml and feeds it
to the same code the build backend would. Delete or neuter that section and this file reds, which is
the whole point of reading the config instead of restating it.
"""

from __future__ import annotations

import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = REPO_ROOT / 'pyproject.toml'

# Every subprocess in this file gets a timeout. Identity is stamped per-command rather than through
# a global config, so the test cannot be affected by (or affect) the developer's git identity.
GIT_TIMEOUT_S = 30
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


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ['git', *GIT_IDENTITY, *args],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT_S,
        check=True,
    )
    return done.stdout.strip()


@pytest.fixture(scope='module')
def declared_version_config() -> dict:
    """The `[tool.hatch.version]` table as this repository actually declares it."""
    with PYPROJECT.open('rb') as handle:
        return tomllib.load(handle)['tool']['hatch']['version']


def test_the_project_declares_no_written_version(declared_version_config: dict) -> None:
    """The cheap half: nothing hand-writes a version, and the build is told to derive one.

    On its own this proves nothing about the STRING -- see the module docstring. It is here because
    it names the failure precisely when someone reintroduces the constant, instead of leaving the
    property test below to fail with a confusing message about two equal versions.
    """
    with PYPROJECT.open('rb') as handle:
        pyproject = tomllib.load(handle)

    assert 'version' not in pyproject['project'], (
        'pyproject declares a hand-written [project] version. That string cannot tell two commits '
        'apart, which is the defect measured on 2026-08-12.'
    )
    assert 'version' in pyproject['project']['dynamic']
    assert declared_version_config['source'] == 'vcs'
    assert 'fallback-version' not in declared_version_config, (
        'A fallback-version answers when the tree has no SCM data -- i.e. it reinstates a constant '
        'string exactly where nothing can check it. The build must raise instead.'
    )


def test_two_sibling_commits_cannot_spell_the_same_version(tmp_path: Path, declared_version_config: dict) -> None:
    """The property: same tag, same distance, different commit -> different version string.

    Siblings, not a sequence. Two commits in a line differ in `guess-next-dev`'s distance counter
    alone, so a line would pass even with the node dropped from the local segment; siblings are the
    case where only the commit hash distinguishes them.
    """
    setuptools_scm = pytest.importorskip(
        'setuptools_scm',
        reason='setuptools_scm is in the `dev` extra -- it is the engine hatch-vcs drives, and this '
        'test computes with it rather than running a full build.',
    )
    raw_options = declared_version_config.get('raw-options', {})

    repo = tmp_path / 'repo'
    repo.mkdir()
    _git(repo, 'init', '--quiet', '--initial-branch=main')
    (repo / 'a.txt').write_text('base\n', encoding='utf-8')
    _git(repo, 'add', 'a.txt')
    _git(repo, 'commit', '--quiet', '-m', 'feat: base')
    _git(repo, 'tag', '-a', 'v0.1.0', '-m', 'Release v0.1.0')
    base = _git(repo, 'rev-parse', 'HEAD')

    versions: dict[str, str] = {}
    for name in ('left', 'right'):
        _git(repo, 'checkout', '--quiet', '-B', name, base)
        (repo / f'{name}.txt').write_text(f'{name}\n', encoding='utf-8')
        _git(repo, 'add', f'{name}.txt')
        _git(repo, 'commit', '--quiet', '-m', f'feat: {name}')
        versions[name] = setuptools_scm.get_version(root=str(repo), **raw_options)

    # Same base, same tag, same distance -- confirmed, so the assertion below is about the commit
    # and nothing else.
    assert _git(repo, 'rev-list', '--count', 'v0.1.0..left') == _git(repo, 'rev-list', '--count', 'v0.1.0..right')
    assert versions['left'] != versions['right'], (
        f'Two different commits produced the same version string {versions["left"]!r}. '
        'That is the 2026-08-12 defect: a consumer caching a verdict against this version serves it '
        'for the other commit. The local segment must carry the node.'
    )


def test_a_tagged_commit_is_the_bare_release_and_an_untagged_one_is_not(
    tmp_path: Path, declared_version_config: dict
) -> None:
    """A tag spells itself `0.1.0`; the commit after it spells itself lower, and carries its hash.

    The ordering half matters as much as the uniqueness half: `guess-next-dev` makes an untagged
    build a dev release of the NEXT patch, so it sorts BELOW that release. A scheme that sorted an
    untagged build ABOVE the tag it precedes would make every dev install look newer than the
    release, which is how a resolver ends up preferring an unreviewed commit.
    """
    setuptools_scm = pytest.importorskip('setuptools_scm')
    raw_options = declared_version_config.get('raw-options', {})

    repo = tmp_path / 'repo'
    repo.mkdir()
    _git(repo, 'init', '--quiet', '--initial-branch=main')
    (repo / 'a.txt').write_text('base\n', encoding='utf-8')
    _git(repo, 'add', 'a.txt')
    _git(repo, 'commit', '--quiet', '-m', 'feat: base')
    _git(repo, 'tag', '-a', 'v0.1.0', '-m', 'Release v0.1.0')

    tagged = setuptools_scm.get_version(root=str(repo), **raw_options)
    assert tagged == '0.1.0', f'a tagged commit must be the bare release, got {tagged!r}'

    (repo / 'b.txt').write_text('next\n', encoding='utf-8')
    _git(repo, 'add', 'b.txt')
    _git(repo, 'commit', '--quiet', '-m', 'feat: next')
    sha = _git(repo, 'rev-parse', '--short=7', 'HEAD')

    untagged = setuptools_scm.get_version(root=str(repo), **raw_options)
    assert untagged.startswith('0.1.1.dev'), f'expected a dev release of the next patch, got {untagged!r}'
    assert f'+g{sha}' in untagged, f'the commit hash must be in the version, got {untagged!r} for {sha}'


def test_git_is_available() -> None:
    """The two tests above are worth nothing without it, and a missing git would make them error.

    Stated as its own assertion so 'git is absent' reads as itself rather than as a version defect.
    """
    assert shutil.which('git') is not None
