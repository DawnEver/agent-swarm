"""`GIT_DIR` overrides `cwd`, so a suite run from inside a git hook can write to the REAL repository.

MEASURED 2026-08-13, and it is the reason this file exists rather than a comment. The hooks this
repo declares were installed for the first time that night; the next push ran the suite from inside
`pre-push`, where git had exported `GIT_DIR` pointing at the real checkout. Every helper that spawns
git passes `cwd=<tmp_path>` and is correct read on its own -- and every one of them operated on the
real repo anyway. The damage, all of it silent: `core.hooksPath` left pointing at a pytest temp
directory that no longer exists (which disarms every hook while the files sit there looking
installed), the committer identity rewritten to a fixture's, the index emptied, and a commit
gutting `.pre-commit-config.yaml` from 148 lines to 6 pushed to `main`.

WHY A TEST AND NOT JUST THE FIXTURE. The fixture in `conftest.py` removes the variables; nothing
would notice if it stopped. The first test below PLANTS the hazard -- it sets `GIT_DIR` by hand and
shows git ignoring `cwd` -- so the mechanism is proven to be real rather than asserted. The second
shows the removal is what makes `cwd` decide again. Delete the fixture and the second test reds.

THE SCOPE CLAIM, stated because a scope claim needs its own check: this protects any test that
spawns git as a SUBPROCESS and inherits the environment. It does NOT protect a test that passes its
own `env=` containing `GIT_DIR`, and it cannot -- that is an explicit instruction, not an ambient
one.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.usefixtures('_no_ambient_git_dir')

_GIT = shutil.which('git') or 'git'


def _run(cwd: Path, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    # timeout: a hanging git parks the suite at 0% CPU instead of failing.
    return subprocess.run([_GIT, *args], cwd=cwd, env=env, check=True, capture_output=True, text=True, timeout=60)


def _repo(root: Path, name: str) -> Path:
    made = root / name
    made.mkdir()
    _run(made, 'init', '-q')
    return made


def test_GIT_DIR_really_does_override_cwd(tmp_path: Path) -> None:
    """The hazard itself, planted. Without this the fixture below guards nothing anyone can see."""
    victim, working = _repo(tmp_path, 'victim'), _repo(tmp_path, 'working')

    poisoned = dict(os.environ, GIT_DIR=str(victim / '.git'))
    _run(working, 'config', 'user.email', 'planted@example.invalid', env=poisoned)

    # cwd said `working`; git wrote to `victim`. That is the whole defect, in one assertion.
    landed = _run(victim, 'config', '--local', '--get', 'user.email').stdout.strip()
    assert landed == 'planted@example.invalid'
    assert _run(working, 'config', '--local', '--list').stdout.find('planted@example.invalid') == -1


def test_with_the_ambient_variable_gone_cwd_decides(tmp_path: Path) -> None:
    """The fixture's actual effect. Reds if `_no_ambient_git_dir` stops removing the variables."""
    victim, working = _repo(tmp_path, 'victim'), _repo(tmp_path, 'working')

    # The session fixture has already emptied these; assert that rather than trust it, because a
    # fixture that silently stopped running is exactly what this file is here to catch.
    for name in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE'):
        assert name not in os.environ, f'{name} is still ambient -- the session fixture did not run'

    _run(working, 'config', 'user.email', 'ordinary@example.invalid')

    assert _run(working, 'config', '--local', '--get', 'user.email').stdout.strip() == 'ordinary@example.invalid'
    assert _run(victim, 'config', '--local', '--list').stdout.find('ordinary@example.invalid') == -1
