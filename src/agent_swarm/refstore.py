"""THE REF TRANSPORT: the seam that lets a decision about refs be tested without a remote.

WHY THIS EXISTS, AND IT IS THE REASON THE REST OF THE MIGRATION STALLED. Motronics' `ci_tick.py`
holds a cluster of functions that are pure fleet mechanism -- prove I am alive, advertise what I can
serve, who is still beating, retire what is stale -- and NONE of them could move here, because every
one is welded to a module-level ``git -C <root>`` helper. The decision and the transport were the
same lines of code. A package whose stated property is "this layer decides; it does not reach" could
not take them, and a lift that dragged `subprocess` along would have made that property false.

SO THE SEAM IS THE DELIVERABLE. :class:`RefStore` is four operations -- list, write, delete, and the
head this store writes against -- and every liveness decision in :mod:`agent_swarm.liveness` is
written against it. `GitRefStore` is the real one; :class:`InMemoryRefStore` in
:mod:`agent_swarm.testing` is the double, and it is deliberately as ill-behaved as git is.

THE ONE OPERATION THAT IS NOT OBVIOUS IS `list`, AND IT CARRIES THE MOST IMPORTANT DISTINCTION IN
THIS FILE: **it RAISES when it cannot ask, and returns empty when there is nothing there.** Those
are different states and collapsing them is a measured incident, not a hypothetical. A listing that
swallowed its error returned `''`, indistinguishable from an empty namespace, so an offline box
reported that no runner in the fleet was alive and refused work on that basis. Every caller that
prefers the quiet reading can catch `RefUnreachable` -- but it can only do that if something raises.

THE PROJECT FACTS ARE CONSTRUCTOR ARGUMENTS WITH NO DEFAULTS: which checkout, and which remote.
A default remote of `origin` is the tempting one and is refused for the same reason as every other
default in this package -- it is right until it is not, and the day it is wrong the fleet publishes
its liveness to somebody else's repository while every local check passes.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Protocol, runtime_checkable

#: Resolved once: a partial executable path is a lint finding worth honouring rather than
#: suppressing, and a box without git then fails at the first call with a name.
_GIT = shutil.which('git') or 'git'


class RefUnreachable(RuntimeError):
    """The remote could not be asked. NOT "there is nothing there".

    Raised only by :meth:`RefStore.list`, because that is the only operation whose failure is
    routinely mistaken for a legitimate empty answer. Writes and deletes report success as a
    boolean, since their callers are already obliged to decide what a failed publish means.
    """


@runtime_checkable
class RefStore(Protocol):
    """Somewhere refs live. Four operations, and no vocabulary above them."""

    def head(self) -> str:
        """A commit id this store may point a marker ref at.

        MARKER REFS CARRY NO CONTENT. A heartbeat and a capability say everything they have to say
        in their NAME, so they need a commit that already exists rather than a new object -- which
        is what keeps a per-tick namespace from growing the repository.
        """
        ...

    def list(self, pattern: str) -> dict[str, str]:
        """``{ref: commit}`` for every ref matching `pattern`.

        THE PATTERN IS A TAIL GLOB AND `*` CROSSES `/`. Measured 2026-08-12 against a real remote,
        because the opposite was written down in two repositories: `refs/ci/heartbeat/*` matches
        `refs/ci/heartbeat/boxA/17`, and so does the bare tail `boxA/17`. A caller therefore CANNOT
        use depth to narrow a listing -- it must filter what comes back. The full table, with the
        cases that do NOT match, is in `InMemoryRefStore`, which is where a reader will be checking
        whether the double is honest.

        Raises:
            RefUnreachable: the remote could not be asked. An empty dict means the namespace is
                empty, and the two must never be conflated -- see this module's docstring.
        """
        ...

    def write(self, ref: str, commit: str) -> tuple[bool, str]:
        """Point `ref` at `commit`, overwriting. Returns ``(ok, why_not)``.

        A PAIR RATHER THAN A BOOL, because the caller's report is worthless without git's own
        words: a liveness push that fails silently is a runner that reads as dead for a reason
        nobody on the box can see.
        """
        ...

    def delete(self, ref: str) -> bool:
        """Remove `ref`. Returns whether the DELETE SUCCEEDED, NOT whether anything was there.

        MEASURED: `git push --delete` of a non-existent ref warns and exits 0, so this cannot
        distinguish "removed it" from "there was nothing". That is the right behaviour for a prune
        racing another prune -- both wanted the ref gone -- but a caller must not read the return
        as "it existed".
        """
        ...


class GitRefStore:
    """A :class:`RefStore` backed by a git remote.

    BOTH ARGUMENTS ARE REQUIRED. `root` is the checkout the commands run in and `remote` is the
    name they push to; defaults for either would be this package deciding a consumer's deployment,
    which is the coupling it deletes rather than adds.
    """

    def __init__(self, root: Path, remote: str) -> None:
        self.root = root
        self.remote = remote

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([_GIT, '-C', str(self.root), *args], capture_output=True, text=True, check=False)

    def head(self) -> str:
        out = self._run('rev-parse', 'HEAD')
        if out.returncode != 0:
            msg = f'cannot resolve HEAD in {self.root}: {out.stderr.strip()}'
            raise RefUnreachable(msg)
        return out.stdout.strip()

    def list(self, pattern: str) -> dict[str, str]:
        """One `ls-remote`. THE NON-ZERO EXIT IS RAISED, which is this class's whole reason for
        being explicit about it -- see the module docstring for the incident."""
        out = self._run('ls-remote', self.remote, pattern)
        if out.returncode != 0:
            msg = f'cannot list {pattern!r} on {self.remote}: {out.stderr.strip() or "(nothing on stderr)"}'
            raise RefUnreachable(msg)
        found: dict[str, str] = {}
        for line in out.stdout.splitlines():
            commit, _, ref = line.partition('\t')
            if commit and ref:
                found[ref.strip()] = commit.strip()
        return found

    def write(self, ref: str, commit: str) -> tuple[bool, str]:
        out = self._run('push', self.remote, f'{commit}:{ref}', '--force')
        return out.returncode == 0, out.stderr

    def delete(self, ref: str) -> bool:
        """Exit status only. See the protocol's note: git exits 0 for an absent ref."""
        return self._run('push', self.remote, '--delete', ref).returncode == 0
