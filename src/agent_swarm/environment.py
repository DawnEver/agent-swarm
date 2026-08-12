"""THE RESOLVED ENVIRONMENT: what a verdict was earned IN, and whether yours may reuse it.

WHAT THIS IS. The mechanism half of motronics' `scripts/ci/ci.py` environment fingerprint, extracted
the day an audit classified every top-level definition under `scripts/` by whether its CODE (not its
prose) names a project noun. Forty-three of that file's sixty-four did not, and this cluster was the
largest coherent one: canonicalising a distribution name, walking a `Requires-Dist` closure, hashing
an interpreter plus an installed set, diffing two such manifests and GRADING each difference.

WHY IT EXISTS AT ALL. A tree hash answers "which code"; it cannot answer "in what". A project that
floats its dependencies has no lockfile to point at, so the set of versions that actually runs is a
fact about a MACHINE at a MOMENT, and `pip install -U` changes what runs while touching no tracked
file. A verdict is only meaningful as the pair.

AND WHY IT IS A DIFF RATHER THAN AN EQUALITY. Two boxes never have identical environments -- one has
jupyter, another a different formatter -- and until this existed ANY difference gave them separate
verdict namespaces, so the same tree was re-tested from scratch on every machine. A difference can
only change an outcome if the tests can REACH it, and reachability is exactly what the dependency
graph records. Three grades come out of it: identical (reuse), differs outside the closure (reuse,
and SAY what differed), differs inside it (re-run).

THE TWO PROJECT FACTS ARE ARGUMENTS, WITH NO DEFAULTS. Which distribution is the closure's root, and
which machine-local paths the tests read, are the consumer's questions. A default for either would
be the `DEFAULT_REPO` coupling this package already deleted once wearing a different noun: every
caller that omitted it would silently get somebody else's project, and nothing would ever say so.

THE FAILURE DIRECTION DECIDES EVERY UNKNOWN HERE, because unknown must cost a re-run and never an
unearned green:

* a distribution whose name cannot be read is recorded as ``UNREADABLE``, not dropped -- dropped, it
  would hash identically to not having the package,
* an unreadable local file is likewise a value rather than an omission,
* extras and markers are IGNORED when walking the closure, which widens it on purpose: a
  distribution wrongly left out is a verdict served for an environment that could have changed the
  answer, one wrongly left in costs a single re-run,
* a declared dependency that is not installed stays IN the closure with no children,
* an EMPTY closure means "cannot tell", not "nothing matters" -- see :func:`env_diff`.
"""

from __future__ import annotations

import contextlib
import hashlib
import platform
import re
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path

#: A `Requires-Dist` entry down to its NAME. Everything after the name is a version specifier, an
#: extras bracket or an environment marker, none of which changes WHICH distribution is meant.
_REQUIREMENT_NAME = re.compile(r'^\s*([A-Za-z0-9._-]+)')

#: The one verdict that permits reuse: the item is not reachable from the project's dependency
#: graph, so no test there can observe it.
OUTSIDE_CLOSURE = 'outside this project'

UNREADABLE = 'UNREADABLE'


def canonical(name: str) -> str:
    """PEP 503 normalisation. `Foo_Bar.Baz` and `foo-bar-baz` are the same distribution, and a diff
    that treated them as two would report a removal and an addition for a package nobody touched.
    """
    return re.sub(r'[-_.]+', '-', name).lower()


def interpreter_identity() -> str:
    """Which Python this is. Its own function so a test can vary it without a fake environment."""
    return f'{platform.python_implementation().lower()}-{platform.python_version()}-{sys.platform}'


def project_closure(root: str) -> frozenset[str]:
    """Every distribution reachable from `root` through `Requires-Dist`, canonicalised.

    `root` IS REQUIRED. It is the one fact here that names a project, and a default would make the
    coupling invisible: every caller that omitted it would get a closure for somebody else's tree
    and a plausible answer about the wrong environment.

    UNKNOWN IS NOT EMPTY, and the caller must treat it that way. If `root` is not installed -- a
    stale editable install, a runner probing before its first sync -- there is no graph to walk and
    the result is the bare root; :func:`env_diff` reads that as "cannot tell" and refuses reuse.
    """
    seen: set[str] = set()
    frontier = [canonical(root)]
    while frontier:
        name = frontier.pop()
        if name in seen:
            continue
        seen.add(name)
        try:
            requires = metadata.distribution(name).requires or []
        except metadata.PackageNotFoundError:
            # A DECLARED DEPENDENCY THAT IS NOT INSTALLED STAYS IN THE CLOSURE and simply has no
            # children. Dropping it would make "absent here, present there" look irrelevant, which
            # is the one difference most likely to change an outcome.
            continue
        matches = (_REQUIREMENT_NAME.match(entry) for entry in requires)
        frontier.extend(canonical(m.group(1)) for m in matches if m)
    return frozenset(seen)


def local_digest(root: Path, entries: Sequence[str]) -> list[str]:
    """Hash the machine-local test inputs a tree hash cannot key. Sorted, and UNREADABLE is a value.

    BOTH ARGUMENTS ARE REQUIRED for the same reason `root` is above: which files those are, and
    which tree they hang off, is the consumer's data. `entries` are paths relative to `root`; a
    directory is walked, a file is hashed, anything absent contributes nothing.

    Recorded rather than skipped when unreadable, for the same reason as a broken distribution: a
    file that vanished from the key when it became unreadable would hash like not having it, and a
    verdict earned without it would be served to a run that has it.
    """
    lines = []
    for entry in entries:
        target = root / entry
        paths = sorted(target.rglob('*')) if target.is_dir() else [target]
        for path in paths:
            if not path.is_file():
                continue
            try:
                digest = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
            except OSError:
                digest = UNREADABLE
            lines.append(f'{path.relative_to(root).as_posix()}={digest}')
    return sorted(lines)


def env_manifest(distributions: Iterable[object] | None = None, *, local_lines: Sequence[str] = ()) -> list[str]:
    """The environment as READABLE data rather than a digest: interpreter, packages, local inputs.

    A HASH CANNOT BE DIFFED, and that was the whole defect this replaced. A verdict that stored only
    its 16-hex key let a runner learn THAT another environment differed and never WHAT differed --
    leaving exactly one behaviour available, a full re-run. The manifest costs a few KB in the
    payload and is what makes every graded answer below possible.

    SORTED, so enumeration order cannot vary the key: `importlib.metadata` walks `sys.path` and
    promises nothing about order, and unsorted the same environment would key differently per
    process, so every lookup would miss for a reason nobody could reproduce.

    `distributions` IS A BAG OF UNKNOWN OBJECTS on purpose -- a test passes doubles -- so the two
    attributes are read with `getattr` rather than through a contract the doubles would have to
    implement.
    """
    if distributions is None:
        distributions = metadata.distributions()
    entries = []
    for dist in distributions:
        # A BROAD SUPPRESSION, DELIBERATELY: `dist` may be a test double or a corrupt install, and
        # every way of failing to read a name has the same answer here -- UNREADABLE, recorded.
        name = None
        with contextlib.suppress(Exception):
            name = getattr(dist, 'metadata', {})['Name']
        version = getattr(dist, 'version', None)
        entries.append(f'{name or UNREADABLE}=={version or UNREADABLE}')
    return [interpreter_identity(), *sorted(entries), *local_lines]


def compute_envkey(manifest: Iterable[str]) -> str:
    """A stable short hash OF a manifest.

    IT TAKES THE MANIFEST rather than recomputing one, which is what makes "one derivation, two
    consumers" a property instead of a promise: a manifest and a key cannot describe different
    environments, because there is only one place the environment is read.
    """
    return hashlib.sha256('\n'.join(manifest).encode()).hexdigest()[:16]


@dataclass(frozen=True)
class EnvChange:
    """One entry of a manifest difference.

    `mine` / `theirs` are ``None`` when the item is absent from that side, which is how "installed
    here only" is told apart from "a different version".
    """

    item: str
    theirs: str | None
    mine: str | None
    #: Why this entry does or does not permit reuse. A REASON, not a bool, because the whole point
    #: of this machinery is that a reader learns WHAT differed rather than only THAT it did.
    verdict: str

    @property
    def blocks_reuse(self) -> bool:
        return self.verdict != OUTSIDE_CLOSURE


def split_manifest(lines: Iterable[str]) -> tuple[dict[str, str], dict[str, str]]:
    """A manifest into ``({distribution: version}, {local path: digest})`` plus its interpreter row.

    The interpreter line has neither separator and is keyed under `interpreter` in the FILE map, so
    it can never be silently dropped: an interpreter change is a change to a test input that no
    dependency graph describes, and it must always block reuse.
    """
    dists: dict[str, str] = {}
    files: dict[str, str] = {}
    for line in lines:
        if '==' in line:
            name, _, version = line.partition('==')
            dists[canonical(name)] = version
        elif '=' in line:
            path, _, digest = line.partition('=')
            files[path] = digest
        else:
            files['interpreter'] = line
    return dists, files


def env_diff(theirs: Iterable[str], mine: Iterable[str], closure: frozenset[str]) -> list[EnvChange]:
    """What separates two environments, each entry SAYING whether it can change a test outcome.

    THE THREE GRADES, with the evidence for all of them: an empty list is "identical, reuse"; a
    non-empty list where nothing `blocks_reuse` is "differs, but nothing this project can reach --
    reuse and SAY what differed"; anything else is "re-run".

    `closure` IS REQUIRED, and passing :func:`project_closure`'s answer for a root that is not
    installed is the "cannot tell" case: a one-element closure containing only the root cannot rule
    anything out. AN EMPTY CLOSURE IS THE SAME ANSWER AND IS HANDLED EXPLICITLY -- treating it as
    "no distribution is relevant" would reuse verdicts across genuinely unrelated environments, an
    unearned green arriving through the door built to prevent one.

    EVERY MACHINE-LOCAL FILE BLOCKS REUSE, unconditionally: those files are read BY TESTS, so a
    dependency graph has nothing to say about them and the honest answer is the conservative one.
    SO DOES THE INTERPRETER -- a different Python is a different runtime for every line of a suite.
    """
    their_dists, their_files = split_manifest(theirs)
    my_dists, my_files = split_manifest(mine)

    changes: list[EnvChange] = []
    for name in sorted(set(their_dists) | set(my_dists)):
        theirv, minev = their_dists.get(name), my_dists.get(name)
        if theirv == minev:
            continue
        if not closure:
            reason = 'this project is not installed, so nothing can be ruled out'
        elif name in closure:
            reason = 'this project depends on it'
        else:
            reason = OUTSIDE_CLOSURE
        changes.append(EnvChange(name, theirv, minev, reason))

    for path in sorted(set(their_files) | set(my_files)):
        theirv, minev = their_files.get(path), my_files.get(path)
        if theirv != minev:
            reason = 'a different interpreter' if path == 'interpreter' else 'a machine-local TEST INPUT'
            changes.append(EnvChange(path, theirv, minev, reason))
    return changes


def env_is_reusable(changes: Iterable[EnvChange]) -> bool:
    """Whether a verdict earned under one manifest may answer for the other."""
    return not any(change.blocks_reuse for change in changes)


def format_env_diff(changes: list[EnvChange], *, limit: int = 12) -> list[str]:
    """The difference as lines a human reads. TRUNCATED, AND THE TRUNCATION SAYS SO.

    A fresh box against an old verdict can differ in a hundred packages, and a report that dumps
    them all is one nobody reads -- the same failure as a section that always prints. Blocking
    entries come FIRST because they are the ones that cost a re-run; a silent `[:12]` would hide
    exactly those behind an alphabetical run of irrelevant ones.
    """
    ordered = sorted(changes, key=lambda c: (not c.blocks_reuse, c.item))
    lines = []
    for change in ordered[:limit]:
        mark = '!' if change.blocks_reuse else ' '
        was = change.theirs if change.theirs is not None else 'ABSENT'
        now = change.mine if change.mine is not None else 'ABSENT'
        lines.append(f'  {mark} {change.item}: {was} -> {now}  ({change.verdict})')
    if len(ordered) > limit:
        blocking = sum(1 for c in ordered[limit:] if c.blocks_reuse)
        lines.append(f'  ... {len(ordered) - limit} more, of which {blocking} block reuse')
    return lines
