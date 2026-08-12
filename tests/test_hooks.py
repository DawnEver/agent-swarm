"""Formatting must happen BEFORE staging, or every commit is a two-step dance.

MOVED HERE WITH THE CODE, 2026-08-12. The measurement stays with the consumer: 42 commit-retry
rounds over two days, not one of them a bad commit. A formatting hook rewrites the file and exits 1;
`git commit` aborts; the author re-stages and commits again. The cost is entirely in the ordering.

These tests build real git repositories and run the REAL hook runner over a real configuration. A
double would be the test checking its own model of what `pre-commit run` does with a rewritten file,
and that model is precisely the thing that was wrong. The configured hook is `language: system`, so
nothing is downloaded and no environment is provisioned -- the run is offline and local.

WHAT THESE CANNOT SEE: `git()` swallows a failure into empty output by design, so a genuinely broken
git installation reads here the same as an empty repository; and nothing below asserts anything about
any particular hook implementation, only about the rewrite/restage/re-run cycle around one.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from agent_swarm.hooks import format_and_restage, git, repo_root, staged_files

#: A hook that REWRITES its files and exits 1 -- the shape that causes the retry dance. Written as a
#: script rather than inlined into the config, because a `language: system` entry is shlex-split and
#: an inlined program would have to survive quoting on two platforms.
_REWRITER = """import sys
changed = 0
for path in sys.argv[1:]:
    body = open(path, encoding='utf-8').read()
    if body != body.upper():
        open(path, 'w', encoding='utf-8').write(body.upper())
        changed = 1
sys.exit(changed)
"""

#: A hook that FAILS and rewrites nothing -- a real lint finding, which must survive as an exit 1.
_COMPLAINER = 'import sys\nsys.exit(1)\n'


def _git(repo: Path, *args: str) -> None:
    # timeout: a git call that hangs would park the suite at 0% CPU rather than failing.
    subprocess.run([shutil.which('git') or 'git', *args], cwd=repo, check=True, capture_output=True, timeout=60)


def _write_config(repo: Path, script: str, body: str) -> None:
    (repo / script).write_text(body, encoding='utf-8')
    entry = f'{Path(sys.executable).as_posix()} {(repo / script).as_posix()}'
    (repo / '.pre-commit-config.yaml').write_text(
        'repos:\n'
        '  - repo: local\n'
        '    hooks:\n'
        '      - id: local-hook\n'
        '        name: local-hook\n'
        '        language: system\n'
        f'        entry: {entry}\n'
        r'        files: \.txt$'
        '\n',
        encoding='utf-8',
    )


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    _git(tmp_path, 'init', '-q')
    _git(tmp_path, 'config', 'user.email', 't@t')
    _git(tmp_path, 'config', 'user.name', 't')
    (tmp_path / 'a.txt').write_text('alpha\n', encoding='utf-8')
    (tmp_path / 'b.txt').write_text('beta\n', encoding='utf-8')
    _git(tmp_path, 'add', '-A')
    _git(tmp_path, 'commit', '-qm', 'base')
    # `git()` runs in the CURRENT directory: that is the module's contract, so the test supplies a
    # current directory rather than reaching inside it.
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_repo_root_answers_about_the_checkout_it_is_standing_in(repo: Path) -> None:
    assert repo_root().resolve() == repo.resolve()


def test_staged_files_lists_exactly_the_index(repo: Path) -> None:
    assert staged_files() == []
    (repo / 'a.txt').write_text('alpha changed\n', encoding='utf-8')
    assert staged_files() == [], 'an unstaged edit is not in the index'
    _git(repo, 'add', 'a.txt')
    assert staged_files() == ['a.txt']


def test_git_returns_stdout_and_reads_a_failure_as_empty(repo: Path) -> None:
    assert git('rev-parse', '--abbrev-ref', 'HEAD').strip()
    assert git('rev-parse', '--verify', 'no-such-ref') == '', 'a failure must not raise from a stdout accessor'


def test_a_rewriting_hook_leaves_the_tree_clean_and_the_rewrite_STAGED(repo: Path, capsys) -> None:
    """THE POINT OF THE WHOLE MODULE. The hook rewrites and exits 1 -- the shape that aborts a
    commit. Here it is absorbed: the rewrite is restaged, the re-run is clean, and the exit is 0, so
    the `git commit` that follows is a first attempt rather than a retry.
    """
    _write_config(repo, 'rewrite.py', _REWRITER)
    _git(repo, 'add', 'a.txt')

    assert format_and_restage(all_files=False, files=['a.txt']) == 0

    assert (repo / 'a.txt').read_text(encoding='utf-8') == 'ALPHA\n'
    assert git('diff', '--name-only').strip() == '', 'the rewrite was left unstaged; the commit would still abort'
    assert 'a.txt' in git('diff', '--cached', '--name-only')
    out = capsys.readouterr().out
    assert 'restaged 1 file(s)' in out and 'a.txt' in out


def test_a_hook_that_rewrites_NOTHING_is_also_a_zero(repo: Path, capsys) -> None:
    """The control. A run over already-formatted content must not report a restage it did not do --
    a report loud about the harmless direction is how the loud one stops being read.
    """
    _write_config(repo, 'rewrite.py', _REWRITER)
    (repo / 'a.txt').write_text('ALPHA\n', encoding='utf-8')
    _git(repo, 'add', 'a.txt')

    assert format_and_restage(all_files=False, files=['a.txt']) == 0
    assert 'restaged' not in capsys.readouterr().out


def test_a_hook_that_FAILS_without_rewriting_survives_as_an_exit_1(repo: Path, capsys) -> None:
    """A lint RULE is not the retry loop. Swallowing it here would turn a red into a green commit,
    which is strictly worse than the dance this module removes.
    """
    _write_config(repo, 'complain.py', _COMPLAINER)
    _git(repo, 'add', 'a.txt')

    assert format_and_restage(all_files=False, files=['a.txt']) == 1
    assert 'a real error, not a rewrite' in capsys.readouterr().err


def test_the_FILES_argument_really_decides_what_is_touched(repo: Path) -> None:
    """THE DISCRIMINATING TEST FOR THE EXTRACTION. Two calls over the same repository with the same
    hook, differing only in the file list, must touch different files. A scope argument that were
    ignored -- silently widened to the whole tree -- would rewrite and RESTAGE files the author had
    deliberately left out of this commit, which is a data loss dressed as a convenience.
    """
    _write_config(repo, 'rewrite.py', _REWRITER)
    _git(repo, 'add', 'a.txt', 'b.txt')

    assert format_and_restage(all_files=False, files=['a.txt']) == 0
    assert (repo / 'a.txt').read_text(encoding='utf-8') == 'ALPHA\n'
    assert (repo / 'b.txt').read_text(encoding='utf-8') == 'beta\n', 'a file outside the given scope was rewritten'

    assert format_and_restage(all_files=False, files=['b.txt']) == 0
    assert (repo / 'b.txt').read_text(encoding='utf-8') == 'BETA\n'


def test_all_files_reaches_what_a_narrow_scope_did_not(repo: Path) -> None:
    """The other half of the same claim: the widening flag is honoured too, so the narrow result
    above is a property of the argument and not of a runner that only ever sees one file.
    """
    _write_config(repo, 'rewrite.py', _REWRITER)
    _git(repo, 'add', 'a.txt', 'b.txt')

    assert format_and_restage(all_files=True, files=[]) == 0
    assert (repo / 'a.txt').read_text(encoding='utf-8') == 'ALPHA\n'
    assert (repo / 'b.txt').read_text(encoding='utf-8') == 'BETA\n'
