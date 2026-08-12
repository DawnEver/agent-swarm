"""Run the formatting hooks and restage, so the FIRST `git commit` is the one that succeeds.

EXTRACTED FROM motronics' `scripts/repo/precommit_fix.py`, 2026-08-12. What stayed behind is the
half that reads that project's gate-verdict format; everything here is any repository's.

WHY THIS EXISTS. Measured over two days (2026-07-26/27): **42 commit-retry rounds**. Not one of them
was a bad commit. `pre-commit`'s `ruff-format` hook does what it is designed to do -- rewrite the
file and exit 1 -- and `git commit` does what IT is designed to do: abort. The result is a two-step
dance on every single commit, and the cost is entirely in the interaction, not in either tool.

The fix is ordering, not configuration: **format BEFORE staging, never discover it during
committing.** This runs the hooks over the staged set, restages exactly what they touched, and
reports what changed -- so the commit that follows is a first attempt.

Deliberately NOT a `git commit` wrapper. The commit message and its trailers are the author's
business, and a wrapper that owned them would be a second place where commit policy lives.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

# Resolved, not spelled: a partial executable path is refused by linters, and a missing tool is an
# ERROR here rather than a silent skip -- a formatter that did not run looks exactly like one that
# found nothing to do.
_GIT = shutil.which('git')

# `pre-commit` is NOT resolved from PATH: git invokes it through the venv's hook shim, so on some
# boxes `shutil.which('pre-commit')` is None while the hooks work perfectly -- measured, and it made
# this refuse to run on the very tree whose hooks it was written to absorb. Running it as a MODULE on
# the interpreter already executing this file cannot pick a different environment than the caller's.
_PRE_COMMIT = (sys.executable, '-m', 'pre_commit')


def git(*args: str) -> str:
    """`git <args>` in the current directory, stdout only. A failure reads as empty output."""
    assert _GIT, 'git is required'
    return subprocess.run([_GIT, *args], capture_output=True, text=True, check=False).stdout


def staged_files() -> list[str]:
    """Repo-relative paths in the index."""
    return [line for line in git('diff', '--cached', '--name-only').splitlines() if line.strip()]


def repo_root() -> Path:
    """The toplevel of the checkout this is running in."""
    return Path(git('rev-parse', '--show-toplevel').strip() or '.')


def format_and_restage(*, all_files: bool, files: list[str]) -> int:
    """Run the hooks, restage what they rewrote, and re-run. Returns the exit code to report.

    Exit 0 = the tree is clean for the hooks (whether or not anything was rewritten). Exit 1 = a hook
    FAILED for a reason formatting cannot fix (a real lint error) -- that one is the author's to read.
    """
    scope = ['--all-files'] if all_files else ['--files', *files]
    # `pre-commit run` exits 1 when a hook MODIFIED a file, which is the case this exists to absorb,
    # and also when a hook genuinely failed. The two are told apart below by asking git what actually
    # changed -- not by parsing the hook output, which is prose and would rot.
    proc = subprocess.run([*_PRE_COMMIT, 'run', *scope], capture_output=True, text=True, check=False)
    sys.stdout.write(proc.stdout)
    sys.stdout.write(proc.stderr)

    rewritten = [line for line in git('diff', '--name-only').splitlines() if line.strip()]
    if rewritten:
        assert _GIT, 'git is required'
        subprocess.run([_GIT, 'add', '--', *rewritten], check=False)
        sys.stdout.write(f'[precommit-fix] restaged {len(rewritten)} file(s) the hooks rewrote:\n')
        for path in rewritten:
            sys.stdout.write(f'  {path}\n')

    # A hook can fail for a reason no rewrite fixes (a lint RULE, a syntax error). If the hooks are
    # unhappy AND nothing was rewritten, this is not the retry loop -- it is a real finding, and
    # swallowing it here would turn a red into a green commit.
    still = subprocess.run([*_PRE_COMMIT, 'run', *scope], capture_output=True, text=True, check=False)
    if still.returncode != 0:
        sys.stdout.write(still.stdout)
        sys.stderr.write('[precommit-fix] a hook still fails after formatting -- a real error, not a rewrite\n')
        return 1
    sys.stdout.write('[precommit-fix] hooks clean; `git commit` will not be rewritten out from under you\n')
    return 0
