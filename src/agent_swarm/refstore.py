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

import contextlib
import json
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextvars import ContextVar
from pathlib import Path
from typing import Protocol, runtime_checkable

#: Resolved once: a partial executable path is a lint finding worth honouring rather than
#: suppressing, and a box without git then fails at the first call with a name.
_GIT = shutil.which('git') or 'git'

#: git verbs that WRITE to the shared remote. `push` is the whole set on purpose: it covers ref
#: DELETION too (`push --delete`), which is the destructive half. `fetch` is deliberately ABSENT --
#: it writes only this checkout's remote-tracking refs, and a caller measuring how far behind it is
#: needs it to answer at all, so forbidding it would make a rehearsal report a staleness it could
#: not measure.
FORGE_MUTATING_VERBS = frozenset({'push'})

#: Whether the CURRENT pass may write to the shared remote. Module-scoped rather than per-instance
#: because the writes it governs sit several frames below whoever armed it, and threading a flag
#: through every signature in between would put the decision in N places again -- which is the
#: defect :meth:`GitRefStore.run` exists to remove. Read through the predicate a consumer supplies,
#: never directly: a consumer with its own flag must be able to keep it.
_WITHHOLDING: ContextVar[bool] = ContextVar('agent_swarm_refstore_withholding', default=False)


def withholding_writes() -> bool:
    """The predicate for a consumer that wants THIS module's `withholding()` to govern its store.

    Offered so the ordinary wiring -- `GitRefStore(root, remote, withhold_writes=withholding_writes)`
    -- is one obvious spelling rather than a lambda each consumer writes differently. It is still
    PASSED rather than assumed: a consumer whose rehearsal flag lives somewhere else supplies its
    own, and nothing here reaches for a global on its behalf.
    """
    return _WITHHOLDING.get()


@contextlib.contextmanager
def withholding(active: bool) -> Iterator[None]:
    """Arm :func:`withholding_writes` for the length of one pass, and ALWAYS disarm it.

    MODULE-LEVEL BECAUSE THE STATE IS, and saying so matters: the flag governs writes several frames
    below whoever armed it, so it cannot be per-store without threading it through every signature
    in between -- the very dispersal :meth:`GitRefStore.run` exists to remove. A caller holding a
    store may spell it `store.withholding(...)`, which forwards here; a caller holding a TEST DOUBLE
    can still arm the real flag, which a method-only form would have made impossible.

    A `ContextVar` AND `reset(token)`, never `= False`: that restores what was there BEFORE, where
    the assignment would hardcode a guess about the outer state. Nothing nests passes today; a
    restore that guesses is how that stops being true safely one day and unsafely the next. The
    `finally` is not padding -- a pass that RAISES must not leave the refusal armed for a later
    caller in the same interpreter, which is exactly the arrangement a test suite is.
    """
    token = _WITHHOLDING.set(active)
    try:
        yield
    finally:
        _WITHHOLDING.reset(token)


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
    """A :class:`RefStore` backed by a git remote, and THE ONE PLACE A CONSUMER SPAWNS GIT.

    IT IS MORE THAN THE FOUR OPERATIONS, AND THAT IS THE POINT rather than scope creep. The four
    are what a liveness DECISION needs; a consumer that also publishes results needs `rev-parse`,
    `hash-object`, `mktree`, `commit-tree`, `update-ref` and `show`, and if this class offers only
    four it will build its own `subprocess.run` for the rest -- which is exactly what motronics'
    `ci_tick.py` did. It grew FOUR git entry points, then a fifth `RefStore` implementation of its
    own, precisely so that all of them could pass through one withholding check. Extracting the
    four and leaving the funnel behind would have moved the decisions and stranded the seam.

    ALL THREE CONSTRUCTOR ARGUMENTS ARE REQUIRED. `root` is the checkout the commands run in,
    `remote` is the name they push to, and `withhold_writes` says whether a REHEARSAL is in
    progress. Defaults for the first two would be this package deciding a consumer's deployment.
    A default for the third is worse: it would be `lambda: False`, so every consumer that forgot to
    wire its rehearsal flag would reach the real remote while its own `--dry-run` reported
    otherwise -- a default that is invisible precisely because it works, which is the defect this
    package removed from `default_forge` and must not reintroduce on the destructive path.

    A PREDICATE, NOT A BOOLEAN. The flag is armed for the length of one pass, after the store is
    built -- consumers construct this at module scope -- so a value captured at construction would
    be the value at import time, forever.
    """

    def __init__(self, root: Path, remote: str, *, withhold_writes: Callable[[], bool]) -> None:
        self.root = root
        self.remote = remote
        self._withhold_writes = withhold_writes

    @staticmethod
    def mutates_the_forge(args: Sequence[str]) -> bool:
        """Does this git argv WRITE to the shared remote.

        THE VERB IS `args[0]`, NOT `verb in args`. A membership test would also fire on any argv
        that merely CONTAINED the word -- a branch or a ref named `push` -- and a guard that refuses
        reads it was never asked to refuse is how a safety flag becomes one people route around.
        """
        return bool(args) and args[0] in FORGE_MUTATING_VERBS

    def run(self, *args: str, cwd: Path | None = None, **kwargs) -> subprocess.CompletedProcess:
        """THE SEAM. Every git call a consumer makes goes through here, and so does the refusal.

        WHY A SEAM RATHER THAN A GUARD PER WRITE SITE. A guard at each write site is N places to
        forget, and the next writer added does not know to add an N+1th -- which is the shape that
        produced the original defect: four entry points, each with its own `subprocess.run`, and a
        `--dry-run` that only one of them consulted.

        A REFUSED WRITE IS ANNOUNCED, NOT SWALLOWED. Returning a synthetic success silently would
        make a rehearsal indistinguishable from a real run, which is the same class of lie the flag
        was introduced to remove. The caller still gets a SUCCESS-shaped result, deliberately: a
        rehearsal must not take the FAILURE branch either, or a liveness writer would report that
        the fleet cannot see this box -- alarming, and false.
        """
        if self._withhold_writes() and self.mutates_the_forge(args):
            sys.stdout.write(f'[refstore] WITHHELD -- no forge write in a rehearsal: git {" ".join(args)}\n')
            return subprocess.CompletedProcess([_GIT, *args], 0, stdout='', stderr='')
        return subprocess.run(
            [_GIT, '-C', str(cwd or self.root), *args], capture_output=True, text=True, check=False, **kwargs
        )

    def text(self, *args: str, check: bool = True, cwd: Path | None = None) -> str:
        """Stdout, stripped. `check` RAISES with git's own words rather than returning ''.

        An empty string is a legitimate answer from several plumbing commands, so a caller that
        cannot tell it from a failure will read one as the other.
        """
        out = self.run(*args, cwd=cwd)
        if check and out.returncode != 0:
            msg = f'git {" ".join(args)} failed: {out.stderr.strip() or "(nothing on stderr)"}'
            raise RuntimeError(msg)
        return out.stdout.strip()

    def ok(self, *args: str) -> bool:
        """Did this call SUCCEED. For writes whose failure must change what happens next.

        :meth:`text` drops the return code, which is right for a best-effort read and WRONG
        whenever the next statement depends on the write having landed. A swallowed code there is
        why an empty liveness namespace on a live box once took a remote inspection to explain.
        """
        return self.run(*args).returncode == 0

    def stdin_text(self, payload: str, *args: str) -> str:
        r"""A git call that reads stdin (`hash-object`, `mktree`). BYTES, NOT TEXT, deliberately.

        `text=True` wraps the pipe in a TextIOWrapper with `newline=None`, which on Windows rewrites
        every `\n` as `\r\n`. For `hash-object` that silently changes the CONTENT hashed; for
        `mktree` the `\r` lands INSIDE the filename, and the first real payload published this way
        held `<name>\r`, so `git cat-file -p <ref>:<name>` -- the documented read path -- answered
        *path does not exist*. A plumbing payload is a byte string by definition and must not be
        line-ending-translated on the way to the process.
        """
        out = subprocess.run(
            [_GIT, '-C', str(self.root), *args], input=payload.encode('utf-8'), capture_output=True, check=True
        )
        return out.stdout.decode('utf-8').strip()

    @staticmethod
    def withholding(active: bool):
        """The ergonomic spelling of :func:`withholding` for a caller that is holding a store.

        A `staticmethod` AND NOT AN INSTANCE ONE, deliberately: the flag is module state, so binding
        it to a store would imply a per-store scope this does not have -- and a reader who believed
        that would arm one store and be surprised by another. It forwards; it does not decide.
        """
        return withholding(active)

    def head(self) -> str:
        out = self.run('rev-parse', 'HEAD')
        if out.returncode != 0:
            msg = f'cannot resolve HEAD in {self.root}: {out.stderr.strip()}'
            raise RefUnreachable(msg)
        return out.stdout.strip()

    def list(self, pattern: str) -> dict[str, str]:
        """One `ls-remote`. THE NON-ZERO EXIT IS RAISED, which is this class's whole reason for
        being explicit about it -- see the module docstring for the incident."""
        out = self.run('ls-remote', self.remote, pattern)
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
        out = self.run('push', self.remote, f'{commit}:{ref}', '--force')
        return out.returncode == 0, out.stderr

    def delete(self, ref: str) -> bool:
        """Exit status only. See the protocol's note: git exits 0 for an absent ref."""
        return self.run('push', self.remote, '--delete', ref).returncode == 0

    def write_payload(self, ref: str, payload: Mapping, message: str, *, filename: str) -> str:
        """Store `payload` as JSON in an ORPHAN COMMIT and point `ref` at it, LOCALLY. Returns the commit.

        NOTHING IS PUSHED. Splitting the durable half from the fragile half is what lets a consumer
        record an expensive answer before touching the network -- see :mod:`agent_swarm.spool` for
        the inversion this exists to serve.

        AN ORPHAN COMMIT RATHER THAN A REF POINTING STRAIGHT AT A BLOB: a blob-ref works locally but
        is an unusual thing to push and server behaviour varies. A commit is the boring choice that
        works everywhere, and `git cat-file -p <ref>:<filename>` reads it back with no tooling.

        `filename` HAS NO DEFAULT. It is the name the reader will `cat-file` and therefore half of a
        contract between a writer here and a reader somewhere else; a default would let the two
        drift while both kept passing, which is the failure this package has already paid for once
        at a repository boundary.
        """
        blob = self.stdin_text(json.dumps(dict(payload), indent=2), 'hash-object', '-w', '--stdin')
        tree = self.stdin_text(f'100644 blob {blob}\t{filename}\n', 'mktree')
        commit = self.text('commit-tree', tree, '-m', message)
        self.text('update-ref', ref, commit)
        return commit
