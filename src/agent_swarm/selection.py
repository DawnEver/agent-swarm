"""Which tests cover a diff -- so a check between edits costs a second instead of a full run.

PROVENANCE. Extracted 2026-08-12 from motronics' `scripts/gate/covering_tests.py`. The measurement
that justifies the tool: one source file was edited 18 times in two days, and ONE meta-gate
assertion failed with the same message SIX times. The invariant was written down the whole time, in
a test file, and was rediscovered by COLLISION once per round -- each collision costing a 6-13
minute full run instead of the sub-second targeted test that encodes it. The information was not
missing. It was only readable in one DIRECTION.

WHAT IS GENERIC, AND IT IS ALL OF THIS FILE. "Map a changed path to the tests that cover it" is a
question any repository with a test suite has. The TIERS below are heuristics over file names, file
contents and a declared map; none of them knows what the code under test does. What a CONSUMER
supplies -- and what this module therefore takes as arguments with no defaults -- is where its
source and tests live, which prefixes are scanned by which tests, and which tests are too expensive
to be part of a fast loop.

THE NAMED BLIND SPOT, and it is the one to read before trusting a result. **A test that SCANS covers
every file in its scan and NAMES none of them.** Every per-file tier keys on a FILE -- a mirrored
path, a mention of the path -- so a scanning test is invisible to all of them. Measured three times,
and the third is the instructive one: a `_SCANNED` map written to close this exact blind spot was
declared `dict[str, str]`, one test per prefix, so adding a second scanner for a prefix CLOSED the
row and HID its neighbour -- and the hidden neighbour was the file that then went red. A map written
to close a coverage blind spot had a blind spot of the same shape, in its own type. It is a TUPLE
per prefix now, and :func:`derived_scanners` exists because a hand-written map can still be
incomplete: measured, 21 test files scanned one glob and the declared row named 2 of them.

AN AUDIT IS ONE-DIRECTIONAL AND SAYING SO IS THE POINT. :func:`audit_declared` checks every LISTED
row is TRUE. Nothing checks that every TRUE row is LISTED, so a clean audit proves non-rot, never
completeness. A derived row cannot fall behind, because there is no list to update.

THREE OUTCOMES, NOT TWO. A selector has to distinguish "nothing covers this diff" (the answer is no)
from "this diff reaches the whole suite, I am the wrong instrument". Rendering the second as a
failure is the same defect as rendering a failure as a pass: the caller cannot tell what happened.
:data:`DEFER_RC` is the third code, and the sentinel is a TEXT channel for a wrapper reading captured
output, which survives a pipe, a tee, and a shell that loses `$?`.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

__all__ = [
    'DEFER_RC',
    'audit_declared',
    'by_convention',
    'by_mention',
    'by_scan',
    'changed_files',
    'covering',
    'derived_scanners',
    'split_by_cost',
]

#: Distinct from 0 (which would make "nothing ran" read as a green) and from 1 (a real red).
DEFER_RC = 3


def changed_files(root: Path, *, staged: bool = False, rev_range: str | None = None) -> list[Path]:
    """Changed files: from a REV RANGE, the index, or the working tree -- in that precedence.

    ``rev_range`` EXISTS FOR THE PRE-PUSH STAGE, and its absence was a measured defect. A pre-commit
    hook must judge what is being COMMITTED, so it asks the index -- but at PUSH time the index is
    EMPTY by construction, since everything being pushed is already committed. The selector then
    reported "no changed files", the run produced no summary line, and the guard correctly called
    that unfinished, so the hook failed every push on a clean tree.

    "What am I pushing" is a different question from "what is in my index".

    ``cwd=root`` IS LOAD-BEARING: git resolves the repository from the WORKING DIRECTORY, so without
    it a worktree's selector invoked by path from another checkout reports the OTHER checkout's
    diff -- silently, in the half that decides which files changed.
    """
    if rev_range:
        args = ['git', 'diff', '--name-only', rev_range]
    else:
        args = ['git', 'diff', '--name-only', *(['--cached'] if staged else [])]
    out = subprocess.run(args, capture_output=True, text=True, check=False, cwd=root).stdout
    return [Path(line) for line in out.splitlines() if line.strip()]


def by_convention(path: Path, *, tests_root: Path, package_root: Sequence[str]) -> list[Path]:
    """Test files the naming convention predicts for ``path``, most-specific first.

    ``package_root`` is the path prefix under which a source file MIRRORS into ``tests_root``/unit --
    e.g. ``('src', 'mypkg')``. It has no default: guessing it would make this tier silently return
    nothing for a differently-laid-out repo, and an empty tier is indistinguishable from a file with
    no tests.

    THREE SPELLINGS, AND THE SECOND TWO ARE NOT GENEROSITY. An exact-stem convention is the one a
    repo mostly follows and NOT the one it always follows, and a suggester that silently drops the
    real covering test is worse than none -- it makes "nothing covers this" indistinguishable from
    "I only looked for one spelling". Measured twice in one session: editing an adapter produced "NO
    covering test found" while `test_<vendor>_adapter.py` sat right there.

    `test_*_<stem>.py` alone would be far too wide, so it is SCOPED to the mirrored package
    directory: a test in the mirror of a source file's own package is about that package by
    construction, whatever prefix its filename carries.

    Only paths that EXIST are returned -- a predicted path that is not there is a prediction.
    """
    if path.suffix != '.py':
        return []
    stem = path.stem
    if stem == '__init__':
        return []
    out: list[Path] = []
    parts = path.parts
    mirrored_under = len(parts) > len(package_root) and tuple(parts[: len(package_root)]) == tuple(package_root)
    if mirrored_under:
        mirrored = tests_root / 'unit' / Path(*parts[len(package_root) : -1]) / f'test_{stem}.py'
        if mirrored.is_file():
            out.append(mirrored)
    out += [p for p in sorted(tests_root.rglob(f'test_{stem}.py')) if p not in out]
    out += [p for p in sorted(tests_root.rglob(f'test_{stem}_*.py')) if p not in out]
    if mirrored_under:
        mirror_dir = tests_root / 'unit' / Path(*parts[len(package_root) : -1])
        if mirror_dir.is_dir():
            out += [p for p in sorted(mirror_dir.glob(f'test_*_{stem}.py')) if p not in out]
    return out


def by_mention(path: Path, *, tests_root: Path, basename_prefixes: Sequence[str] = ()) -> list[Path]:
    """Last resort: test files whose SOURCE names this path. A heuristic, and labelled as one.

    Deliberately last, and only when the precise tiers found nothing: it matches on TEXT, so it can
    pick up a file that merely mentions the path in a comment. An over-inclusive suggestion costs a
    few seconds; a false "untested" costs a wrong decision -- it states as fact that a file has no
    tests when it is merely spelled unexpectedly, and that is a claim someone will act on.

    ``basename_prefixes`` NAMES THE PATH PREFIXES WHOSE FILES ARE ALSO MATCHED BY BASENAME, and it
    is not a loosening for its own sake. A directory that is not an importable package can only be
    tested by loading its files BY PATH::

        spec_from_file_location('x', root / 'scripts' / 'thing.py')

    The literal `scripts/thing.py` never appears in that idiom, so a full-path needle cannot see the
    only way such files are testable. MEASURED: the path needle found 4 of the 20 tests that load
    one such module, and 17 of the 60 that load another.
    """
    needles = {path.as_posix()}
    if path.suffix == '.py' and any(path.as_posix().startswith(prefix) for prefix in basename_prefixes):
        needles.add(path.name)
    hits = []
    for test in sorted(tests_root.rglob('test_*.py')):
        try:
            text = test.read_text(encoding='utf-8', errors='replace')
        except OSError:
            continue
        if any(needle in text for needle in needles):
            hits.append(test)
    return hits


def derived_scanners(prefix: str, *, patterns: Mapping[str, str], tests_root: Path, root: Path) -> list[Path]:
    """Test files whose SOURCE marks them as scanners of ``prefix``. Empty if the tier is absent.

    DERIVED, NOT WRITTEN, and that is the whole reason this exists next to a declared map: the
    declared one was widened from one test per prefix to a tuple, which fixed the TYPE and left the
    POPULATION hand-written -- so it could still be incomplete, and was. A derived row cannot fall
    behind, because there is no list to update.

    The pattern should be the distinguishing GLOB rather than the bare prefix: many test files
    mention a directory and are already selected by the per-file tiers, but only a test that
    iterates EVERY file under it is affected by a change to any one of them.
    """
    pattern = patterns.get(prefix)
    if pattern is None or not tests_root.is_dir():
        return []
    rx = re.compile(pattern)
    return [
        found.relative_to(root)
        for found in sorted(tests_root.rglob('test_*.py'))
        if rx.search(found.read_text(encoding='utf-8', errors='replace'))
    ]


def by_scan(
    path: Path,
    *,
    declared: Mapping[str, Sequence[str]],
    patterns: Mapping[str, str],
    tests_root: Path,
    root: Path,
) -> list[Path]:
    """Tests that scan a directory this path lives under -- declared rows UNION derived ones.

    Deduplicated while preserving order: a file may be reached both ways, and a duplicate would put
    the same path on the command line twice.
    """
    posix = path.as_posix()
    out: list[Path] = []
    seen: set[str] = set()
    for prefix in declared.keys() | patterns.keys():
        if not posix.startswith(prefix):
            continue
        derived = derived_scanners(prefix, patterns=patterns, tests_root=tests_root, root=root)
        for rel in (*declared.get(prefix, ()), *(p.as_posix() for p in derived)):
            if rel not in seen:
                seen.add(rel)
                out.append(Path(rel))
    return out


def covering(
    paths: Sequence[Path],
    *,
    tiers: Sequence[Callable[[Path], list[Path]]],
    mention: Callable[[Path], list[Path]],
    additive_prefixes: Sequence[str] = (),
) -> tuple[list[Path], list[Path]]:
    """``(tests to run, paths nothing covers)`` -- the second list is the honest half.

    A file with no covering test is REPORTED, never silently dropped: "the targeted run was green"
    has to be distinguishable from "there was nothing to run", and those two look identical if the
    uncovered paths are swallowed here.

    ``mention`` IS SEPARATE FROM ``tiers`` because it is a FALLBACK -- any precise hit suppresses it
    -- except under ``additive_prefixes``, where it is added regardless. That distinction was a real
    defect: a single coarse declared row covering a whole directory fired for every file under it
    and HID the tests that actually load them, selecting 1 test where 20 exercise the file, and the
    1 did not import it. A coarse row that hides a precise tier cannot be fixed by adding rows; a
    richer row would shadow just as hard.

    A CHANGED TEST MEASURES ITSELF -- except a `conftest.py`, which is not a test and collects ZERO.
    Selecting one produced a run that passed while measuring nothing: a vacuous green, from the tool
    built to avoid one. A conftest applies to everything BELOW it, so that subtree is what it covers.
    """
    tests: list[Path] = []
    uncovered: list[Path] = []
    for path in paths:
        posix = path.as_posix()
        if posix.startswith('tests/'):
            tests.append(path.parent if path.name == 'conftest.py' else path)
            continue
        found: list[Path] = []
        for tier in tiers:
            found += tier(path)
        # `is_dir()` too: a declared row may name a whole TIER directory. Filtering on `is_file()`
        # alone silently dropped it -- the row would be present, audited, true, and never selected.
        found = [p for p in found if p.is_file() or p.is_dir()]
        if any(posix.startswith(prefix) for prefix in additive_prefixes):
            found += [p for p in mention(path) if p not in found]
        if not found:
            found = mention(path)
        if found:
            tests += found
        else:
            uncovered.append(path)
    seen: dict[str, Path] = {}
    for test in tests:
        seen.setdefault(test.as_posix(), test)
    return list(seen.values()), uncovered


def split_by_cost(tests: Sequence[Path], *, slow_prefixes: Sequence[str]) -> tuple[list[Path], list[Path]]:
    """Split covering tests into ``(cheap enough to run between edits, not)``.

    NAMED RATHER THAN TIMED AT RUNTIME, because timing them is the cost this tool exists to avoid.
    A SILENT exclusion would be a false-cheap claim, so the caller is expected to LIST what it
    withheld.

    A test file at the ROOT of the tests tree counts as slow: in the repo this came from that is the
    case-library level, which re-runs a whole case per parameter. A tier DIRECTORY is judged by its
    prefix ALONE and never by that depth heuristic -- `tests/architecture` matches the shape of a
    root-level file while being neither a file nor slow.
    """

    def is_slow(test: Path) -> bool:
        posix = test.as_posix()
        if test.is_dir():
            return posix.startswith(tuple(slow_prefixes))
        return posix.startswith(tuple(slow_prefixes)) or posix.count('/') == 1

    return [t for t in tests if not is_slow(t)], [t for t in tests if is_slow(t)]


def audit_declared(declared: Mapping[str, Sequence[str]], root: Path) -> list[str]:
    """Every declared row that names a test which is gone, or no longer mentions its prefix.

    A hand-written map needs a check that consults reality, or it rots in the one direction that
    matters: claiming coverage that stopped existing. A row may name a FILE or a whole TIER
    DIRECTORY; the directory form exists so a tier cannot outgrow a hand-written list, and it is
    held to the same standard one level up -- at least one test under it must really mention the
    prefix, or the row claims coverage that is not there.

    RETURNS THE PROBLEMS rather than printing or asserting: the caller owns its report format, and a
    function that exits cannot be tested without a subprocess.
    """
    bad: list[str] = []
    for prefix, rels in declared.items():
        for rel in rels:
            test = root / rel
            if test.is_dir():
                files = sorted(test.rglob('test_*.py'))
                needle = prefix.rstrip('/')
                if not files:
                    bad.append(f'{prefix} -> {rel}/: directory holds no test files')
                elif not any(needle in f.read_text(encoding='utf-8', errors='replace') for f in files):
                    bad.append(f'{prefix} -> {rel}/: no test under it mentions {needle!r}')
            elif not test.is_file():
                bad.append(f'{prefix} -> {rel}: no such test file')
            elif prefix.rstrip('/') not in test.read_text(encoding='utf-8', errors='replace'):
                bad.append(f'{prefix} -> {rel}: that test never mentions {prefix.rstrip("/")!r}')
    return bad
