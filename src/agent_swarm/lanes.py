"""LANES: a git worktree per parallel agent, created so it can be worked in and removed when it is
provably finished.

WHAT THIS IS. The mechanism half of motronics' `scripts/lanes/new_lane.py` and `prune_lanes.py`,
extracted the day an audit classified every module under `scripts/` as stays / moves / splits.
Creating a worktree, deriving its branch, seeding it, surfacing prior art, and deciding whether one
is safe to delete are things ANY fan-out needs. WHICH files a fresh lane must be seeded with is not:
that list named one project's generated version module, its config file and its case caches, and it
arrives here as an ARGUMENT with no default -- the same removal `default_forge`'s `DEFAULT_REPO`
got, and for the same reason. A default that works is invisible until the day it is wrong.

WHY SEEDING EXISTS AT ALL, since a git worktree is supposed to be complete. It is complete in
TRACKED files. The gitignored ones -- generated version stamps, local config, cached artifacts --
are absent, and their absence does not error: it degrades. MEASURED 2026-07-25: six lanes were
created by hand with

    cp "$R/config/config.toml" "$WT/config/config.toml" 2>/dev/null

and the `2>/dev/null` hid the failure. One lane then ran its suite with no config and took **exactly
one** test down. Not the 122 the documentation predicted. One. A single red in a file the lane never
touched reads exactly like an integration regression, and the agent correctly refused to call its
run green -- but it spent its budget on a defect that was in the SCAFFOLDING. Hence: copy, then
PROVE the copy landed byte-for-byte, and refuse loudly if it did not.

WHY PRUNING IS THE HARDER HALF. Lanes accumulate -- measured 2026-07-27: **28 worktrees, ~2 GB**, of
which 24 were merged and clean. Cleaning them is not the problem; deciding WHICH is safe is, and
doing that by eye is how the wrong one goes. Doing it by hand, three lanes were classified as
"unmerged work" from `git merge-base --is-ancestor`, and TWO OF THE THREE WERE ALREADY MERGED under
different SHAs. Ancestry answers "is this COMMIT upstream"; the question is "is this CHANGE
upstream", and a cherry-picked commit is not an ancestor of where its content landed. `git cherry`
compares patch-ids and answers the real one.

EVERY REFUSAL NAMES ITSELF, and nothing is deleted on a maybe. The one check that can be wrong --
recency -- is labelled a HEURISTIC where it is returned, because recency is evidence of use and not
proof of it. The check that CANNOT be wrong in the safe direction is the process census, and it is
the reason this module depends on :mod:`agent_swarm.procs`: `occupants` answers ``None`` for "I
could not look", which this module treats as a refusal rather than as an empty box.

WHAT IS NOT CLAIMED. Nothing here decides that a lane SHOULD exist, or what work goes in it. And a
lane that passes every check is only proven idle at the instant it was measured -- a caller that
sits on the result and deletes later is deleting on a stale fact.
"""

from __future__ import annotations

import filecmp
import re
import shutil
import subprocess
import time
from collections.abc import Iterable, Sequence
from pathlib import Path

from agent_swarm import procs

#: The operator's git, via PATH. `shutil.which` IS the operator's answer; a guessed absolute path is
#: a second opinion about which git this is, and on a box with two of them it is the wrong one.
GIT = shutil.which('git') or 'git'


class LaneError(RuntimeError):
    """A lane could not be created, seeded or inspected. It carries the reason a human needs.

    RAISED RATHER THAN RETURNED for every seeding failure, because the failure mode being closed is
    a half-built worktree that LOOKS finished. A return value can be ignored by one caller; this
    cannot.
    """


def _git(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    """Run git, letting a failure surface in the return rather than as an exception."""
    return subprocess.run(
        [GIT, *args], cwd=None if cwd is None else str(cwd), capture_output=True, text=True, check=False
    )


def main_checkout_root(start: Path) -> Path:
    """The MAIN checkout's root according to GIT -- and a REFUSAL if ``start`` is in a worktree.

    `--show-toplevel` answers with whatever checkout you are standing in, so running lane creation
    from inside a lane would happily nest the new worktree INSIDE that lane. That is not
    hypothetical: the first revision of this function did exactly that in its own smoke test, reached
    `git worktree add`, and was only stopped by the branch name already existing.

    `--git-common-dir` is the discriminator: it points at the MAIN `.git` from anywhere, while
    `--git-dir` points inside a linked worktree. Different means we are in a worktree, and a lane
    must be seeded from the main checkout's gitignored files -- which a lane does not have.

    ASK GIT, NEVER COUNT PARENTS. A hand-counted number of `.parent` calls has already shipped
    off-by-one bugs pointing at the wrong directory, and it is the same git here that will create the
    worktree, so the two cannot disagree.
    """
    start = Path(start)
    top = _git('-C', str(start), 'rev-parse', '--show-toplevel')
    if top.returncode != 0:
        msg = f'REFUSING: {start} is not inside a git repository (git said: {top.stderr.strip()})'
        raise LaneError(msg)
    here = Path(top.stdout.strip())

    common = _git('-C', str(start), 'rev-parse', '--path-format=absolute', '--git-common-dir')
    if common.returncode == 0:
        main_root = Path(common.stdout.strip()).parent
        if main_root.resolve() != here.resolve():
            msg = (
                f'REFUSING: {here} is a worktree, not the main checkout.\n'
                f'Lanes are created FROM the main checkout ({main_root}), whose gitignored files are\n'
                f'the ones a lane must be seeded with.'
            )
            raise LaneError(msg)
    return here


def ignored_files(repo_root: Path, *paths: str) -> list[Path]:
    """Every gitignored file under ``paths``, ASKED OF GIT rather than listed by hand.

    THE POINT OF DERIVING IT. MEASURED 2026-07-25: one case directory carried a gitignored winding
    file and a cache tree. A fresh worktree had neither, so a test silently fell back to fetching
    from a remote server -- and when that host refused, two tests red in a way indistinguishable from
    a code regression. The lane spent real time exonerating it.

    A hand-written list of such artifacts is correct on the day it is written and silently wrong the
    first time a directory gains one. `git ls-files --others --ignored --exclude-standard` reads the
    same ignore rules the worktree will, so the two cannot disagree about what is missing.

    Returns paths RELATIVE to ``repo_root``, which is the form :func:`seed_lane` takes. Git reports a
    wholly-ignored DIRECTORY as one entry, not as its members; that is handled at the copy.
    """
    listed = _git('-C', str(repo_root), 'ls-files', '--others', '--ignored', '--exclude-standard', '-z', '--', *paths)
    if listed.returncode != 0:
        return []
    return [Path(p) for p in listed.stdout.split('\0') if p]


def seed_lane(repo_root: Path, lane_path: Path, seed: Sequence[Path], *, optional: Sequence[Path] = ()) -> int:
    """Copy the gitignored files a lane cannot work without, then PROVE each copy landed.

    Args:
        repo_root: the main checkout the files are taken from.
        lane_path: the worktree they are copied into.
        seed: paths relative to ``repo_root`` that MUST exist. REQUIRED, and an empty sequence is a
            valid, meaningful answer: it means SEED NOTHING. It does not mean "fall back to a list
            this module knows", because this module knows none -- which files a lane cannot run
            without is a fact about a project, and every list of them this code has seen named one.
        optional: paths whose absence in ``repo_root`` is not an error, typically the output of
            :func:`ignored_files`. Git only lists what is there, so an empty result means this
            checkout has no cached artifacts either -- not that seeding failed.

    Returns:
        How many files were copied, so a caller can SAY the number rather than imply it.

    Raises:
        LaneError: a required file is missing from ``repo_root``, or a copy did not land
            byte-for-byte. Never a warning: a copy whose failure is discarded is indistinguishable
            from one that worked, which is the entire defect this function exists to end.
    """
    repo_root, lane_path = Path(repo_root), Path(lane_path)
    copied = 0
    for rel in seed:
        src = repo_root / rel
        if not src.is_file():
            msg = f'REFUSING: {src} does not exist in the main checkout, so the lane cannot be seeded with it.'
            raise LaneError(msg)
        _copy_and_verify(src, lane_path / rel)
        copied += 1
    for rel in optional:
        src = repo_root / rel
        target = lane_path / rel
        if src.is_dir():
            # A wholly-ignored DIRECTORY is one entry in git's answer. Today every artifact may be a
            # file; one that gains an ignored cache directory must not make this fall over -- or
            # worse, seed nothing while looking like it seeded something.
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(src, target, dirs_exist_ok=True)
            copied += 1
        elif src.is_file():
            _copy_and_verify(src, target)
            copied += 1
    return copied


def _copy_and_verify(src: Path, target: Path) -> None:
    """Copy one file and compare it byte-for-byte. `shallow=False`, or it compares stat and lies."""
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, target)
    if not target.is_file() or not filecmp.cmp(src, target, shallow=False):
        msg = f'REFUSING: {target} did not land byte-for-byte. The lane would run against different data.'
        raise LaneError(msg)


def create_lane(
    repo_root: Path,
    name: str,
    *,
    base: str,
    seed: Sequence[Path],
    optional_seed: Sequence[Path] = (),
    worktrees_dir: Path | None = None,
) -> Path:
    """Create the worktree for lane ``name`` on a new branch, seed it, and return its path.

    THE BRANCH IS THE LANE'S NAME. One string names the directory, the branch and the work, so
    "which branch is that directory" is never a question anyone has to answer from memory -- and
    `fanout.md`'s rule "trust the branch, not the directory name" exists because somebody once had
    to.

    ``base`` IS EXPLICIT AND HAS NO DEFAULT. Branching a lane off a stale default branch is its own
    measured failure mode: the integration branch is ahead of it, and a lane based on the lag reds in
    code it never touched. A caller that has not decided its base has not decided anything.

    Raises:
        LaneError: the destination exists, `git worktree add` refused, or seeding failed. An existing
            destination is refused rather than reused, because reusing one silently inherits whatever
            state it was abandoned in.
    """
    repo_root = Path(repo_root)
    dest = (Path(worktrees_dir) if worktrees_dir else repo_root / '.claude' / 'worktrees') / name
    if dest.exists():
        msg = f'REFUSING: {dest} already exists. Remove it first, or pick another lane name.'
        raise LaneError(msg)
    created = _git('-C', str(repo_root), 'worktree', 'add', '-b', name, str(dest), base)
    if created.returncode != 0:
        msg = f'REFUSING: `git worktree add` failed for base {base!r}:\n{created.stderr.strip()}'
        raise LaneError(msg)
    seed_lane(repo_root, dest, seed, optional=optional_seed)
    return dest


# --------------------------------------------------------------------------- prior art


#: Lane-name words that describe the WORKFLOW rather than the subject. Matching on these retrieves
#: everything, which retrieves nothing. Kept short on purpose -- a long stoplist is a declaration
#: that rots -- and free of any project's own name, which is the consumer's to add via `stopwords`
#: (in a repo whose every note names the product, that word is the emptiest token there is).
STOPWORDS = frozenset(
    {
        'add',
        'and',
        'design',
        'finding',
        'fix',
        'for',
        'lane',
        'new',
        'not',
        'plan',
        'run',
        'test',
        'tests',
        'the',
        'wip',
    }
)

#: A single shared word only counts when it is LONG enough to be a subject rather than a connective.
#: Measured 2026-07-28: `size` (4) matched a mesh note for a lane about a pool size -- a coincidence;
#: `thread` (6) matched a watchdog-thread finding for a thread-pinning lane -- a real hit. Two shared
#: words of any length is already specific enough to stand alone.
MIN_SOLO_TOKEN_LEN = 6

#: Never surface more than this. A retrieval aid with no ceiling becomes a wall, a wall is skipped,
#: and a skipped aid is worth exactly what an unread finding is worth -- which is the defect this
#: whole feature exists to answer.
MAX_SUGGESTIONS = 3


def tokens(text: str, *, stopwords: Iterable[str] = STOPWORDS) -> set[str]:
    """Subject words of a name: lowercase, longer than two characters, workflow words removed."""
    stop = set(stopwords)
    return {t for t in re.split(r'[^a-z0-9]+', text.lower()) if len(t) > 2 and t not in stop}


def related_notes(
    lane: str, notes_dir: Path, *, stopwords: Iterable[str] = STOPWORDS, limit: int = MAX_SUGGESTIONS
) -> list[tuple[float, Path, set[str]]]:
    """Notes whose FILENAME shares subject words with the lane name, rarest word first.

    WHY THIS EXISTS, AND WHY AT LANE CREATION. On 2026-07-28 a lane rediscovered a defect a note had
    documented 11 days earlier -- including its predicted recurrence and a mitigation nobody ran --
    and reproduced two of that note's four recorded errors before stumbling onto it mid-task. The
    finding was correct, retrievable, and unread. **A correct finding nobody retrieves is worth what
    no finding is worth.**

    Lane creation is the ONLY moment that is both before any work is done and already holds a
    description of the work: the lane name. Everything else -- a pre-commit hook, a gate -- runs
    after the thinking is finished. So this attaches to a path every lane already walks, rather than
    adding a tool someone has to remember.

    Scored by INVERSE DOCUMENT FREQUENCY: a word in one filename discriminates, a word in forty does
    not. NAMES ONLY, never file bodies -- bodies would surface a match for almost any lane, and the
    ceiling would then hide the good one behind three weak ones.

    ``notes_dir`` IS REQUIRED and no directory is assumed to exist: a missing one returns nothing,
    because a consumer that keeps no notes is not an error.
    """
    notes_dir = Path(notes_dir)
    if not notes_dir.is_dir():
        return []
    # `worktrees` is tested against the path RELATIVE to the notes directory. An absolute-path test
    # matches the lane's own prefix whenever this runs from inside a worktree and silently excludes
    # EVERY file -- 0 hits, indistinguishable from "no prior art". That exact error was made twice in
    # one day on 2026-07-28; both times the symptom was a confident, empty, plausible answer.
    files = [
        p
        for p in sorted(notes_dir.rglob('*.md'))
        if 'worktrees' not in p.relative_to(notes_dir).parts and not p.name.startswith('_')
    ]
    frequency: dict[str, int] = {}
    for path in files:
        for token in tokens(path.stem, stopwords=stopwords):
            frequency[token] = frequency.get(token, 0) + 1

    # Drop a leading index prefix (`w118-`): the lane NUMBER is not a subject word.
    subject = tokens(lane.split('-', 1)[1] if '-' in lane else lane, stopwords=stopwords)
    scored: list[tuple[float, Path, set[str]]] = []
    for path in files:
        shared = subject & tokens(path.stem, stopwords=stopwords)
        if not shared:
            continue
        if len(shared) < 2 and max(len(t) for t in shared) < MIN_SOLO_TOKEN_LEN:
            continue
        scored.append((sum(1.0 / frequency[t] for t in shared), path, shared))
    scored.sort(key=lambda row: (-row[0], str(row[1])))
    return scored[:limit]


# --------------------------------------------------------------------------- pruning


def worktrees(repo_root: Path) -> list[tuple[Path, str]]:
    """``[(path, branch-or-'detached')]`` for every LINKED worktree; the main one is excluded.

    The main worktree is the FIRST entry of `git worktree list --porcelain` -- git's own definition,
    independent of where the caller stands.

    This was `rev-parse --show-toplevel` until 2026-07-28, and that is wrong in exactly the situation
    this function is for: inside a linked worktree it returns THAT worktree, so the lane you are
    standing in was excluded and **the repository itself was offered as a prune candidate**. Only the
    live-process and idle checks stood between that and removing the main checkout -- and the idle
    check has a documented off switch.
    """
    out: list[tuple[Path, str]] = []
    path: Path | None = None
    for line in _git('-C', str(repo_root), 'worktree', 'list', '--porcelain').stdout.splitlines():
        if line.startswith('worktree '):
            path = Path(line.removeprefix('worktree ').strip())
        elif line.startswith('branch ') and path is not None:
            out.append((path, line.removeprefix('branch refs/heads/').strip()))
            path = None
        elif line.startswith('detached') and path is not None:
            out.append((path, 'detached'))
            path = None
    return out[1:]


def unmerged_commits(path: Path, *, upstream: str) -> list[str]:
    """Commits in this worktree's HEAD whose PATCH is not in ``upstream``.

    `git cherry`, NEVER `merge-base --is-ancestor`: it compares patch-ids, so a commit that was
    cherry-picked or rebased into the upstream branch is correctly reported as already there.
    Ancestry is not, and reading ancestry as content membership is the measured mistake that nearly
    cost two merged lanes their directories -- in the direction of KEEPING them, which is why it was
    survivable, and it is equally capable of the other direction on a branch that was force-updated.

    ``upstream`` IS REQUIRED. The branch a fan-out integrates into is a fact about a fan-out, and a
    default here would compare against a branch the work never targeted -- reporting a lane fully
    merged when nothing of it is.
    """
    listed = _git('cherry', upstream, 'HEAD', cwd=path).stdout
    return [line for line in listed.splitlines() if line.startswith('+')]


#: Directories whose mtimes say nothing about whether a human is working in a lane. Matched against
#: the path RELATIVE to the worktree -- see :func:`idle_minutes`.
NOISE_DIRS = frozenset({'.git', '__pycache__', 'output', '.pytest_cache', '.ruff_cache'})


def idle_minutes(path: Path, *, skip: Iterable[str] = NOISE_DIRS) -> float:
    """Minutes since the most recent mtime under the worktree. ``inf`` when nothing was found.

    THE SKIP NAMES ARE MATCHED RELATIVE TO THE WORKTREE, never against the absolute path, and this is
    the whole correctness content of the function. A lane lives under some parent directory, so every
    child carries that prefix in its parts; matching absolutely means any skip name appearing in the
    PREFIX prunes the ENTIRE walk. The failure is not quiet in its consequences: an empty walk leaves
    nothing newest, this returns ``inf``, the lane reads as infinitely idle, and it is listed as
    removable. A live lane would be deleted.

    Measured 2026-08-05: the identical scheme elsewhere WAS live -- its prefix contained one of the
    names -- and silently scanned nothing: 5 items present, 0 reported. Same defect, opposite blast
    radius; that one failed toward a false green, this one toward deletion.
    """
    path = Path(path)
    skip = set(skip)
    newest = 0.0
    for child in path.rglob('*'):
        if any(part in skip for part in child.relative_to(path).parts):
            continue
        try:
            newest = max(newest, child.stat().st_mtime)
        except OSError:
            continue
    return (time.time() - newest) / 60.0 if newest else float('inf')


def reasons_to_keep(path: Path, *, upstream: str, idle_ceiling: float = 30.0) -> list[str]:
    """Why this lane must NOT be removed. An empty list -- and only that -- means removable.

    EVERY REASON IS RETURNED, not the first: a refusal naming one of three causes sends the reader to
    fix the wrong thing, and here the reader's next move is a deletion.

    THE CHECKS, and what each is worth:

    * live processes inside the tree -- the only FACT in the list, and the only one that needs no
      cooperation from whoever is working there;
    * an unanswerable process census -- ``None`` from :func:`agent_swarm.procs.occupants` is NOT an
      empty box. Treating "could not look" as "nobody is there" is a fail-open whose failure
      direction is deletion, and it is exactly what the earlier version did on every platform it had
      not been written for;
    * tracked modifications, and untracked files -- unfinished work, possibly never staged;
    * commits whose patch is not upstream -- real work not integrated;
    * recency -- a HEURISTIC, labelled as such in the reason it returns. It is the only check that
      can be wrong in the safe direction, and ``idle_ceiling = 0`` disables it for a caller who knows
      the box is quiet.
    """
    status = [line for line in _git('status', '--porcelain', cwd=path).stdout.splitlines() if line.strip()]
    dirty = [line for line in status if not line.startswith('??')]
    untracked = [line for line in status if line.startswith('??')]

    keep: list[str] = []
    live = procs.occupants(path)
    if live is None:
        keep.append('could not enumerate processes -- cannot prove this lane is idle')
    elif live:
        keep.append(f'{len(live)} live process(es) working in this worktree -- IN USE')
    if dirty:
        keep.append(f'{len(dirty)} tracked file(s) modified')
    if untracked:
        keep.append(f'{len(untracked)} untracked file(s)')
    unmerged = unmerged_commits(path, upstream=upstream)
    if unmerged:
        keep.append(f'{len(unmerged)} commit(s) whose patch is NOT in {upstream}')
    if idle_ceiling > 0:
        idle = idle_minutes(path)
        if idle < idle_ceiling:
            keep.append(f'touched {idle:.0f} min ago (< {idle_ceiling:.0f}); may be in use RIGHT NOW -- a HEURISTIC')
    return keep


def survey(repo_root: Path, *, upstream: str, idle_ceiling: float = 30.0) -> list[tuple[Path, str, list[str]]]:
    """``[(path, branch, reasons_to_keep)]`` for every linked worktree, sorted by path.

    A SURVEY, NOT AN ACTION. Reporting and removing are split so the report can be read by a human
    and by :func:`remove`, and so the default behaviour of any CLI over this is to say what it would
    do. A lane with an empty reason list is the removable one.
    """
    return [
        (path, branch, reasons_to_keep(path, upstream=upstream, idle_ceiling=idle_ceiling))
        for path, branch in sorted(worktrees(repo_root))
    ]


def remove(path: Path) -> str | None:
    """Remove one worktree. Returns git's refusal, or ``None`` on success.

    GIT'S OWN SAFETY CHECKS ARE ALLOWED TO WIN. A refusal here means git sees something these checks
    did not, and git is the more authoritative reader of its own repository. Reported, never
    swallowed and never forced.
    """
    out = _git('worktree', 'remove', str(path))
    return None if out.returncode == 0 else (out.stderr.strip() or f'exit {out.returncode}')


def render_survey(surveyed: Sequence[tuple[Path, str, list[str]]]) -> tuple[list[str], list[Path]]:
    """``(report lines, removable paths)`` for a :func:`survey` result.

    EVERY reason is printed, not the first, and that is the whole reason this is not an ``if`` at a
    call site: :func:`reasons_to_keep` returns all of them precisely because a refusal naming one of
    three sends the reader to fix the wrong thing, and a renderer that showed only ``keep[0]`` would
    undo that at the last step.

    RETURNS the removable set rather than removing it. The split is :func:`survey`'s, carried one
    layer further out: a caller that only wants the report never touches the removal, so "print what
    I would do" cannot drift from "do it" -- both read this one list.
    """
    lines: list[str] = []
    removable: list[Path] = []
    for path, branch, keep in surveyed:
        if keep:
            lines.append(f'KEEP   {path.name:34s} [{branch}]')
            lines.extend(f'         - {reason}' for reason in keep)
        else:
            removable.append(path)
            lines.append(f'PRUNE  {path.name:34s} [{branch}]  clean, fully upstream, idle')
    return lines, removable


def remove_all(paths: Sequence[Path]) -> list[tuple[Path, str | None]]:
    """Remove each path, pairing it with :func:`remove`'s refusal (``None`` when it went).

    NEVER STOPS AT THE FIRST REFUSAL. git declining one worktree says nothing about the next, and a
    loop that aborted there would leave the rest of a surveyed-clean set behind with no report -- the
    caller would see one error and no way to tell whether anything else was even attempted.
    """
    return [(path, remove(path)) for path in paths]
