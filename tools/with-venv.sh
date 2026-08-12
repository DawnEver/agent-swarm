#!/usr/bin/env bash
# Resolve ONE Python interpreter for the dev-time hooks, and exec through it.
#
# WHY A RESOLVER RATHER THAN `entry: ruff check`. A hook naming a bare tool resolves whatever PATH
# offers, and PATH offering a DIFFERENT copy than the one the developer tests with is not
# hypothetical: motronics measured it on 2026-08-12, `ruff` on PATH at 0.15.15 from a global install
# against `python -m ruff` at 0.16.1 in its venv, the two disagreeing about the formatting of one
# file -- the hook rewrote the tree one way and the gate judged it the other. Pinning both to one
# version is forbidden (never pin) and only postpones the divergence to whoever bumps one of them.
# Removing the second copy is the fix, and the only way to remove it is to stop letting PATH answer.
#
# THIS REPO HAS NO VENV OF ITS OWN, and that is the honest complication. Its runtime is stdlib-only
# and it is developed from a checkout, so there is nothing that must be installed for the PACKAGE to
# work -- only for the HOOKS. So the resolution order is:
#
#   1. $AGENT_SWARM_PYTHON  -- an explicit interpreter, which is how a developer whose dev extras
#                              live in a sibling project's environment says so. Explicit beats
#                              guessed, and it keeps this repo from naming any specific project
#                              (`tests/test_this_package_names_no_specific_project.py`).
#   2. <this checkout>/.venv -- a lane's own environment wins when it has one.
#   3. <main checkout>/.venv -- a worktree with none borrows the main checkout's, resolved from
#                              `--git-common-dir` rather than a hardcoded path.
#
# AND THERE IS NO FOURTH STEP. Falling back to PATH would reintroduce the exact second copy this
# script exists to remove, and it would do it silently, on the machines least likely to notice.
# Unresolved means REFUSED, loudly, naming both remedies.
#
# Both venv layouts are probed on every platform rather than switched on `uname`: Windows is
# `Scripts/python.exe`, macOS/Linux is `bin/python`, and the fleet contains both.
set -euo pipefail

THIS_ROOT="$(git rev-parse --show-toplevel)"
MAIN_ROOT="$(dirname "$(git rev-parse --path-format=absolute --git-common-dir)")"

PYTHON=''
if [ -n "${AGENT_SWARM_PYTHON:-}" ]; then
  if [ ! -x "${AGENT_SWARM_PYTHON}" ]; then
    echo "with-venv: AGENT_SWARM_PYTHON is set to '${AGENT_SWARM_PYTHON}', which is not executable." >&2
    echo "  It is not ignored in favour of a guess -- an explicit setting that is wrong is a" >&2
    echo "  different problem from no setting at all, and silently guessing past it hides both." >&2
    exit 1
  fi
  PYTHON="${AGENT_SWARM_PYTHON}"
else
  for root in "${THIS_ROOT}" "${MAIN_ROOT}"; do
    for candidate in "${root}/.venv/Scripts/python.exe" "${root}/.venv/bin/python"; do
      if [ -z "${PYTHON}" ] && [ -x "${candidate}" ]; then PYTHON="${candidate}"; fi
    done
  done
fi

if [ -z "${PYTHON}" ]; then
  echo "with-venv: no interpreter resolved for agent-swarm's dev hooks." >&2
  echo "  Checked: \$AGENT_SWARM_PYTHON, ${THIS_ROOT}/.venv, ${MAIN_ROOT}/.venv" >&2
  echo "  PATH IS DELIBERATELY NOT CONSULTED -- a hook and a developer resolving two different" >&2
  echo "  copies of ruff is the defect this script exists to refuse." >&2
  echo "  Fix by either:" >&2
  echo "    python -m venv .venv && .venv/bin/python -m pip install -e '.[dev]'" >&2
  echo "    export AGENT_SWARM_PYTHON=/path/to/an/interpreter/with/agent-swarm[dev]" >&2
  exit 1
fi

# A SCRIPT PATH IS NOT A MODULE NAME and `-m` cannot run one. motronics measured the consequence of
# conflating them: its pre-push smoke expanded to `python -m python <script>.py`, failed instantly
# with `No module named python`, and ran ZERO tests for weeks while reporting an unfinished run.
# The `.py` suffix already says which of the two an argument is, so dispatch on it.
case "${1:-}" in
  *.py) exec "${PYTHON}" "$@" ;;
esac

exec "${PYTHON}" -m "$@"
