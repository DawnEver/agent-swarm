"""The launcher must find an interpreter that WORKS, not a name that resolves.

FOUND BY A REMOTE AGENT ON WS1 AND REPRODUCED HERE. `where python3` succeeds on a stock Windows
install: it resolves to the Microsoft Store app-execution alias, which prints an advertisement and
exits non-zero. `where py` is the same shape -- the launcher can be installed with no 3.x
registered. Existence and usability are different questions and only the second one decides whether
swarmctl can run, so every candidate is now probed by RUNNING it with `--version`.

AND A SECOND DEFECT THE FIRST FIX EXPOSED, which is the more dangerous one. pyenv, conda and scoop
all put `.cmd`/`.bat` SHIMS on PATH, and **a batch file that invokes another batch file without
`call` transfers control and never returns.** Measured with a planted shim: the run exited with the
shim's code and swarmctl never executed -- the probe hijacked the wrapper instead of answering it.
`call` is harmless for a real `.exe`, so it is correct for both.

WHY THIS IS TESTED THROUGH THE REAL `cmd.exe`. The behaviour under test IS cmd's -- control
transfer, `&&` semantics, `%ERRORLEVEL%` timing. A python-level simulation of it would be a
simulation of my belief about cmd, which is exactly what was wrong the first time.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

#: The launchers live beside the checkout they launch, not inside the package: they exist precisely
#: for the machine where the package is NOT installed, and a file inside `src/` would be found only
#: by something that had already solved the problem they solve.
_REPO = Path(__file__).resolve().parents[1]
_WRAPPER = _REPO / 'swarmctl.cmd'

pytestmark = [pytest.mark.unit, pytest.mark.skipif(os.name != 'nt', reason='cmd.exe is absent off Windows')]


@pytest.fixture
def sandbox(tmp_path):
    """A copy of the wrapper over a stub PACKAGE that reports success, so the test observes
    RESOLUTION rather than swarmctl's own behaviour.

    THE STUB IS A PACKAGE UNDER `src/` BECAUSE THAT IS WHAT THE WRAPPER RESOLVES. It forwards to
    `-m agent_swarm.swarmctl` with its own `src` prepended to PYTHONPATH, so a sandbox holding a
    bare `swarmctl.py` would fail for a reason that has nothing to do with interpreter resolution,
    and a sandbox holding nothing at all would fall through to the REAL package -- a control that
    passes without the wrapper having resolved anything.
    """
    shutil.copy(_WRAPPER, tmp_path / 'swarmctl.cmd')
    package = tmp_path / 'src' / 'agent_swarm'
    package.mkdir(parents=True)
    (package / '__init__.py').write_text('', encoding='utf-8')
    (package / 'swarmctl.py').write_text('print("RESOLVED")\n', encoding='utf-8')
    return tmp_path


def _shim(directory, name: str, code: int) -> None:
    """A candidate that EXISTS and FAILS -- the Microsoft Store alias, as a batch file."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f'{name}.cmd').write_text(
        f'@echo off\r\necho Python was not found\r\nexit /b {code}\r\n', encoding='ascii'
    )


def _run(sandbox, extra_path=None):
    env = dict(os.environ)
    if extra_path is not None:
        env['PATH'] = f'{extra_path}{os.pathsep}{env["PATH"]}'
    return subprocess.run(
        [str(sandbox / 'swarmctl.cmd'), 'config'],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        env=env,
        cwd=str(sandbox),
    )


def test_a_working_interpreter_is_found_normally(sandbox):
    """The control. Without it, every assertion below could pass because nothing ever resolves."""
    result = _run(sandbox)
    assert 'RESOLVED' in result.stdout, result.stderr


def test_a_candidate_that_EXISTS_but_FAILS_is_skipped(sandbox, tmp_path):
    """The Microsoft Store alias, reproduced. `where` finds it; running it does not work."""
    fake = tmp_path / 'fake'
    _shim(fake, 'py', 9009)
    _shim(fake, 'python3', 49)
    result = _run(sandbox, extra_path=str(fake))
    assert 'RESOLVED' in result.stdout, f'rc={result.returncode} err={result.stderr}'
    assert result.returncode == 0


def test_a_batch_SHIM_does_not_hijack_the_wrapper(sandbox, tmp_path):
    """The defect the first fix exposed. Without `call`, control transfers to the shim and swarmctl
    never runs -- and the failure looks like swarmctl exiting with a strange code rather than like a
    probe that ate the process.
    """
    fake = tmp_path / 'fake'
    _shim(fake, 'py', 9009)
    result = _run(sandbox, extra_path=str(fake))
    assert result.returncode != 9009, 'the probe shim replaced the wrapper instead of answering it'
    assert 'RESOLVED' in result.stdout


def test_diagnostics_go_to_stderr_not_stdout(sandbox, tmp_path):
    """A diagnostic on stdout corrupts anything piping this, and the POSIX sibling already used
    stderr -- two launchers for one tool must not disagree about which stream is which.
    """
    (sandbox / 'src' / 'agent_swarm' / 'swarmctl.py').unlink()
    fake = tmp_path / 'fake'
    for name in ('py', 'python3', 'python'):
        _shim(fake, name, 1)
    env = dict(os.environ)
    env['PATH'] = str(fake)  # nothing usable at all
    result = subprocess.run(
        [str(sandbox / 'swarmctl.cmd'), 'config'],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        env=env,
        cwd=str(sandbox),
    )
    assert 'No usable Python 3' in result.stderr
    assert 'No usable Python 3' not in result.stdout


def test_the_posix_launcher_probes_the_same_way():
    """Read as text rather than executed: the shell is absent on the boxes this suite runs on today,
    and the two launchers drifting apart is the failure being guarded -- one of them would then be
    fixed and the other quietly left broken.
    """
    posix = (_REPO / 'swarmctl').read_text(encoding='utf-8')
    assert '--version' in posix, 'the POSIX launcher still trusts name resolution alone'
    assert 'command -v' in posix, 'existence is still worth checking first; it is cheaper'
    assert '-m agent_swarm.swarmctl' in posix, 'the two launchers no longer start the same module'
    assert 'PYTHONPATH' in posix, 'the POSIX launcher no longer points the interpreter at this tree'
