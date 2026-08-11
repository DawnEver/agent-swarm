@echo off
rem ===========================================================================
rem  swarmctl -- Windows launcher. It finds a Python, puts this checkout's
rem  `src` on PYTHONPATH and forwards to `-m agent_swarm.swarmctl`; every verb,
rem  flag and default lives in that module, which runs identically on macOS and
rem  Linux via the sibling `swarmctl` shell script.
rem
rem  PYTHONPATH RATHER THAN AN INSTALL, and it is the reason swarmctl can be run
rem  at all on the machine that matters: the Gitea host has no venv and nothing
rem  installed. The package is stdlib-only by construction, so a checkout plus
rem  any Python 3 is the whole requirement. It also means the code under test is
rem  THIS tree and not a pinned copy in site-packages -- the same trap
rem  `pyproject.toml`'s `pythonpath` closes for the suite.
rem
rem  NOTHING IS CONFIGURED HERE. Run `swarmctl config <admin-user>` once per
rem  machine; `swarmctl config` prints where that is stored.
rem ===========================================================================
setlocal

rem PROBE BY RUNNING IT, not by asking whether the name resolves. `where python3` SUCCEEDS on a
rem stock Windows install and resolves to the Microsoft Store app-execution alias, which prints an
rem advertisement and exits non-zero -- so existence and usability are different questions, and only
rem the second one matters. Same for `py`: the launcher can be installed with no 3.x registered.
rem Measured on this box: `where python3` finds it, `python3 --version` says "Python was not found".
rem `call`, and it is not decoration: pyenv, conda and scoop all put `.cmd`/`.bat` SHIMS on PATH,
rem and a batch file that invokes another batch file WITHOUT `call` transfers control and never
rem returns -- the probe would hijack the wrapper instead of answering it. Measured with a planted
rem shim: the run exited with the shim's code and swarmctl never ran. `call` is harmless for a real
rem `.exe`, so it is right for both.
set "PY="
call py -3 --version >nul 2>&1 && set "PY=py -3"
if not defined PY ( call python3 --version >nul 2>&1 && set "PY=python3" )
if not defined PY ( call python  --version >nul 2>&1 && set "PY=python" )
if not defined PY (
  rem stderr, matching the POSIX sibling: a diagnostic on stdout corrupts anything piping this.
  echo   No usable Python 3 on PATH. swarmctl is stdlib-only; any Python 3.9+ will do. 1>&2
  echo   ^(A name that resolves is not enough -- each candidate is run with --version.^) 1>&2
  exit /b 2
)

rem NO GIT CHECK, and its removal is the point rather than a tidy-up. There was one, and its stated
rem reason -- credentials being stored by piping them into `git credential approve` -- stopped being
rem true when role tokens moved out of the operator's credential store (agent_swarm.credentials).
rem swarmctl now runs no git at all. A guard that outlives its reason does not become harmless: it
rem refuses to start over a dependency nothing uses, and it asserts a mechanism that is gone.

rem PREPENDED, never assigned over: a caller may already be pointing PYTHONPATH somewhere, and
rem losing that silently is how a launcher starts deciding things it was written not to decide.
if defined PYTHONPATH (
  set "PYTHONPATH=%~dp0src;%PYTHONPATH%"
) else (
  set "PYTHONPATH=%~dp0src"
)

%PY% -m agent_swarm.swarmctl %*
exit /b %ERRORLEVEL%
