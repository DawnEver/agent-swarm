r"""PROCESS ACCOUNTING: who is running, whose is it, and how do you stop it without making orphans.

WHAT THIS IS. The mechanism half of motronics' `scripts/proc/proc_probe.py` and
`scripts/proc/stop_sweep.py`, extracted the day an audit classified every module under `scripts/`
as stays / moves / splits. The probe failed the "a MOVES claim carries no project noun" check: it
held `REPO_TOKEN = 'motronics'` and imported `motronics.core.utils.config.repo_root`. Walking the
process table, attributing a process to a tree, and terminating a forest children-first are things
ANY fleet needs; *which* tree is mine is the consumer's question, and it arrives here as an
ARGUMENT. There is no default token and no default root -- a defaulted one would be exactly the
`DEFAULT_REPO` coupling `admission` already deleted, wearing a different noun.

THE PROPERTY THAT MADE THE PROBE NECESSARY, and the one thing not to simplify away:
**an xdist worker names NOTHING on its own command line.** Its argv is

    "<uv-managed>\\python.exe" -u -c "import sys;exec(eval(sys.stdin.readline()))"

-- the interpreter lives under `AppData\\Roaming\\uv\\python\\` and the `-c` payload names nothing at
all. The project token appears only in its PARENT, the `.venv\\Scripts\\python.exe` launcher shim.
So a `CommandLine`-substring census sees the shims and misses every process that actually holds
memory. MEASURED against a live 8-worker gate, the two methods side by side:

    command-line match   12 procs   0.21 GB total   130 MB max
    process tree         22 procs   4.85 GB total   672 MB max

A 23x underestimate, from the command a rules file named as the thing to run BEFORE quoting a
memory figure. Attribution here is therefore STRUCTURAL -- executable inside the tree, working
directory inside the tree, or command line naming it -- then closed over children.

THE SECOND VERSION OF THE SAME DEFECT (2026-08-04), because the first fix could not see an ORPHAN.
Seeding only from processes that NAME the tree and reaching the rest as their children means a
killed controller leaves nothing to descend from: the probe printed `procs=0` against 34 orphans
holding ~9 GB, which then starved three consecutive gate runs into `node down`. Reachability was the
axis nobody varied. No process needs a living relative to be counted; see :func:`is_tree_process`.

AND THE THIRD: a caller that mentions the token on its own command line matches its own query, so a
teardown loop kills the shell that launched it (observed: exit code 15). The exclusion is
STRUCTURAL, not a spelling filter -- see :func:`self_and_ancestors`. There is no query string a
caller can pass that makes it measure, or kill, itself.

``psutil`` IS AN OPTIONAL EXTRA (``pip install "agent-swarm[procs]"``), and the package it belongs
to declares NO runtime dependencies at all. That constraint is real and this module does not weaken
it: the rule is "this layer decides; it does not reach", and the reason THIS module may reach is
that it is not the deciding half -- it is the MEASUREMENT that supplies the facts the deciding half
consumes. `admission.capacity_blocker` takes `available_gib` as an ARGUMENT; something has to go
and read it. An extra is what keeps that asymmetry visible: `import agent_swarm` still installs and
imports with nothing third-party present.

So this module IMPORTS without psutil and refuses to guess at the point of use:

* :func:`occupants` and :func:`image_running` answer ``None`` -- "I could not look", which is NOT an
  empty list and NOT False,
* every enumerating or terminating function raises `RuntimeError` naming the extra.

Neither invents a number, because every caller's failure direction here is destructive: deleting a
worktree someone is working in, admitting a job onto a busy machine, reporting a stop that stopped
nothing. Unknown must cost a refusal rather than a resource.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Callable, Sequence
from pathlib import Path

try:
    import psutil

    HAVE_PSUTIL = True
except ImportError:  # pragma: no cover -- the honest-failure path
    HAVE_PSUTIL = False

#: How long a terminated process is given to actually leave the process table before it is called a
#: SURVIVOR. A CEILING ON THE WAIT, not a cost paid every time: :func:`_wait_gone` returns the
#: instant nothing is left.
#:
#: WHY THIS IS A WAIT AND NOT A SLEEP, which is the one place this extraction changed behaviour. The
#: original settled a flat 2 s and then looked once. That makes the VERDICT a function of the sleep:
#: too short and a process still being torn down is reported as a survivor -- a stop that worked,
#: reported as failed -- and too long and every clean stop pays for the worst case. Measured while
#: writing this module's tests: with the settle shortened, a subtree that died correctly came back
#: as `[40012, 79740]`. A bounded poll is right in both directions.
SETTLE_S = 5.0

#: Grace given to a terminated process before escalating, and to a killed one before it is called a
#: survivor. A worker mid-write to a log gets the chance to finish the line.
REAP_GRACE_S = 8.0
REAP_KILL_S = 5.0


#: What to say when the extra is missing. It names the INSTALL, because "psutil is not installed" in
#: a package that deliberately has no dependencies sends the reader looking for a bug rather than
#: for a command.
_NO_PSUTIL = (
    'agent_swarm.procs needs the optional psutil extra: pip install "agent-swarm[procs]". '
    'No process census is better than a wrong one.'
)


def _require_psutil() -> None:
    """Refuse loudly rather than fail with a `NameError` three frames down.

    RAISED AT THE POINT OF USE, not at import. Raising on import would make `procs` unimportable and
    therefore make `agent_swarm` unimportable the day anything re-exports it -- which would turn an
    OPTIONAL extra into a required dependency by the back door, undoing the whole point of the
    extra.
    """
    if not HAVE_PSUTIL:
        raise RuntimeError(_NO_PSUTIL)


def self_and_ancestors() -> set[int]:
    """This process and every process above it -- a probe is never part of what it measures.

    MEASURED THE HARD WAY, twice in one day. A caller that mentions the token on its own command
    line (`python -c "TOKEN='...'; ..."`) matches its own query, so the census returns the caller
    and the caller's children -- and a teardown loop that kills what it found then kills the caller.
    A negative control asserting that an unmatchable name yields nothing cannot catch this: it tests
    a name NOTHING has, while the defect is a name the CALLER has.

    So the exclusion is structural rather than a spelling filter, and it covers ANCESTORS as well as
    self: the shell, the agent process and the CI wrapper above you are all things a sweep must not
    take out from under itself.
    """
    excluded: set[int] = set()
    if not HAVE_PSUTIL:
        return excluded
    try:
        proc = psutil.Process()
    except psutil.NoSuchProcess:  # pragma: no cover
        return excluded
    excluded.add(proc.pid)
    for parent in proc.parents():
        excluded.add(parent.pid)
    return excluded


def _canonical(path: str | Path | None) -> Path | None:
    """One spelling for one directory, or ``None`` if the path cannot be read at all.

    Just ``resolve()``, which collapses `..`, follows links, and -- MEASURED -- expands a Windows
    8.3 alias: `C:\\PROGRA~1` resolves to `C:\\Program Files`. It does that by asking the FILESYSTEM
    (`GetFinalPathNameByHandle`), not by string surgery.

    NO ``normcase`` HERE, and the absence is deliberate because the obvious argument for it is
    FALSE. It was added on the reasoning that Windows paths are case-insensitive while
    `relative_to` is case-sensitive -- but pathlib's `WindowsPath` already folds case, measured:
    `Path('...\\DELETED-LANE\\SUB').relative_to(Path('...\\Deleted-Lane'))` returns `SUB`. On POSIX
    `normcase` is a no-op. So the line could not be distinguished from its own absence by ANY test,
    and it was removed rather than kept as an untestable comfort with a docstring that lied about
    the reason.

    THE HOLE THIS DOES NOT CLOSE, named rather than implied: an 8.3 alias can only be expanded for a
    path that still EXISTS, because the mapping lives on disk. A process whose working directory has
    been DELETED -- a torn-down worktree, which is routine here -- keeps its short spelling, and
    nothing in this process can recover the long one. Such a process is attributed by its executable
    or its command line, or not at all. It is never silently attributed to a DIFFERENT tree, which
    is the direction that would matter.
    """
    try:
        return Path(path).resolve()
    except (OSError, ValueError):  # pragma: no cover -- an unreadable or malformed path
        return None


def under(path: str | Path | None, root: str | Path | None) -> bool:
    """Is ``path`` inside ``root``? Path-segment containment, NOT a string prefix.

    The distinction is the whole point: ``str.startswith`` would claim a sibling checkout named
    ``<root>-old``, and a fan-out session is exactly where such a sibling exists.

    BOTH SIDES ARE CANONICALISED HERE, and that is a fix rather than tidiness. This resolved only
    the LEFT side, and every caller in this module happens to hand it an already-resolved root -- so
    the scope was right BY ACCIDENT, in a public function whose docstring never said the root had to
    be resolved first. MEASURED: `Path('C:/PROGRA~1/Common Files').relative_to(Path('C:/Program
    Files'))` raises `ValueError` across two spellings of one directory. A caller passing a root
    straight from an environment variable or a config file would therefore get False for EVERY
    process -- and the failure direction is a sweep that finds nothing and reports success, which is
    the instrument-that-lies shape this module exists to prevent.
    """
    if not path or not root:
        return False
    here, tree = _canonical(path), _canonical(root)
    if here is None or tree is None:
        return False
    try:
        here.relative_to(tree)
    except ValueError:
        return False
    return True


def is_tree_process(
    *, token: str, root: Path, exe: str | None = None, cwd: str | None = None, cmdline: Sequence[str] = ()
) -> bool:
    """Does this process belong to ``root``? Decided STRUCTURALLY, never by who its parent is.

    ``token`` and ``root`` are REQUIRED and keyword-only. They are the consumer's two facts, and a
    default for either is the coupling this module was split to remove -- a project noun compiled
    into fleet infrastructure, invisible because it works for that one project.

    Three independent pieces of evidence, any one sufficient:

    * its executable is inside the tree (a `.venv` launcher shim),
    * its working directory is inside the tree -- an xdist worker INHERITS cwd from the controller
      and KEEPS it after the controller dies, which is the only mark an orphaned, uv-managed,
      ``-c``-payload worker carries at all,
    * its command line names the tree (the controller, ``-m pytest``, a gate script).

    The first two are what make this survive orphanhood. MEASURED 2026-08-04: seeding from the third
    alone and closing over CHILDREN reported ``procs=0`` against 34 orphaned processes holding
    ~9 GB, because a killed controller leaves nothing to descend from.

    Every field is optional because ``psutil`` raises ``AccessDenied`` per-field on processes this
    user does not own; a missing field is absence of evidence, not evidence of absence, so it simply
    does not vote.
    """
    # `under` canonicalises both sides itself, so the root is passed through raw: one place decides
    # what "the same directory" means, and a caller cannot get a different answer by pre-resolving.
    if under(exe, root) or under(cwd, root):
        return True
    return any(token in part for part in (cmdline or ()))


def _iter_python(excluded: set[int]):
    """Live python processes, minus this probe's own tree. Fields are best-effort per psutil."""
    for proc in psutil.process_iter(['pid', 'name', 'cmdline', 'exe', 'cwd']):
        try:
            if proc.info['pid'] in excluded or not (proc.info['name'] or '').lower().startswith('python'):
                continue
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        yield proc


def close_over_children(seeds: set[int]) -> list[psutil.Process]:
    """Seeds plus every python descendant, so a process reachable only via its parent is kept.

    Redundancy, not the mechanism: since :func:`is_tree_process` seeds structurally, nothing NEEDS a
    living relative to be found. This still catches a worker whose cwd was changed out from under
    it, which no structural field would then attribute.
    """
    _require_psutil()
    found: dict[int, psutil.Process] = {}
    for pid in seeds:
        # THE LOOKUP AND THE WALK ARE ONE ATTEMPT, because they are two syscalls with a gap between
        # them. `children()` used to sit outside this `try`, so a seed that exited in that gap raised
        # `NoSuchProcess` out of a census -- MEASURED as a flake in
        # `test_a_query_that_matches_OUR_OWN_command_line_does_not_return_us`, once in 1223, and
        # worse on a busy box, which is exactly when a census is being read.
        #
        # A PROCESS VANISHING MID-ENUMERATION IS NORMAL, NOT EXCEPTIONAL. Anything this function
        # walks is by definition something it does not own; the only honest answer is to omit what
        # is already gone. `AccessDenied` is likewise omission and not failure -- a census that
        # raised on the first unreadable process would report nothing about the many it could read.
        try:
            proc = psutil.Process(pid)
            children = proc.children(recursive=True)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        # RECORDED AFTER THE WALK, NOT BEFORE. `psutil.Process(pid)` succeeds against a process that
        # has already exited but not been reaped, and `children()` is where that surfaces -- so
        # recording first would return a pid the caller then kills or reports as live.
        found[pid] = proc
        for child in children:
            try:
                if (child.name() or '').lower().startswith('python'):
                    found[child.pid] = child
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    return list(found.values())


def processes_matching(token: str) -> list[psutil.Process]:
    """Processes whose COMMAND LINE names ``token``, plus their python descendants.

    A SCOPING QUERY, deliberately NOT structural: it answers "which processes did this particular
    invocation start", which is what a test needs to isolate one synthetic tree from every other
    python on the box. :func:`processes_in_tree` is the one that answers "what of this tree is
    running" -- do not use this for that, since a token nothing carries must yield nothing here,
    and an orphan carries nothing.
    """
    _require_psutil()
    excluded = self_and_ancestors()
    seeds = {
        proc.info['pid']
        for proc in _iter_python(excluded)
        if any(token in part for part in (proc.info['cmdline'] or ()))
    }
    return close_over_children(seeds)


def processes_in_tree(*, token: str, root: Path) -> list[psutil.Process]:
    """Every live python process belonging to ``root`` -- the number a fan-out rule asks for.

    Seeds are STRUCTURAL (:func:`is_tree_process`), so no process needs a living relative to be
    counted; the descendant closure remains as redundancy. Both facts are REQUIRED: "which tree is
    this" is the consumer's declaration, and making it optional is how the orphan blind spot got in
    -- a caller that omits it should get a `TypeError`, never a plausible census of somebody else's
    checkout.
    """
    _require_psutil()
    excluded = self_and_ancestors()
    seeds = {
        proc.info['pid']
        for proc in _iter_python(excluded)
        if is_tree_process(
            token=token,
            root=root,
            exe=proc.info.get('exe'),
            cwd=proc.info.get('cwd'),
            cmdline=proc.info.get('cmdline') or (),
        )
    }
    return close_over_children(seeds)


def occupants(tree: Path) -> list[psutil.Process] | None:
    """Who is working INSIDE ``tree`` right now -- or ``None`` if that cannot be answered.

    THE FACT LAYER, and the only one of the three presence layers that may be used as a safety
    premise. It needs zero cooperation: a human who opens an agent TUI in a worktree HAS a process,
    whether or not any hook fired and whether or not the session was spawned through fabric. The
    intent layer (a session-start hook) enriches this and must never gate on it -- a hook can fail.
    The managed layer (sessions this fleet spawned) sees only what it spawned, so requiring it would
    produce invisible work plus the belief that everything is visible.

    ``None`` IS NOT AN EMPTY LIST. Without psutil, or on a box where no process is readable at all,
    the honest answer is "I could not look", and every caller's failure direction here is
    destructive (deleting a worktree, admitting a job onto a busy machine), so unknown must cost a
    refusal rather than a resource.

    NOT restricted to python: an editor, a shell or a vendor GUI open in the tree is an occupant.

    CONTAINMENT IS BY PATH SEGMENT (:func:`under`), never a string prefix -- a prefix test claims a
    sibling checkout named ``<tree>-old``, and a fan-out is exactly where such a sibling exists.
    """
    if not HAVE_PSUTIL:
        return None
    try:
        root = Path(tree).resolve()
    except OSError:
        return None
    excluded = self_and_ancestors()
    found: list[psutil.Process] = []
    readable = 0
    for proc in psutil.process_iter(['pid', 'cwd', 'cmdline']):
        info = proc.info
        if info['pid'] in excluded:
            continue
        readable += 1
        # ABSOLUTE ARGUMENTS ONLY. `under` resolves a relative path against THIS process's cwd, not
        # against the inspected process's -- so a bare `scripts/gate/gate.py` in anyone's argv
        # resolved inside our own tree and matched. Measured: 614 "occupants" of one root, nearly
        # all of them fictional.
        args = [str(arg) for arg in info.get('cmdline') or ()]
        if under(info.get('cwd'), root) or any(under(arg, root) for arg in args if Path(arg).is_absolute()):
            found.append(proc)
    if not readable:
        # An empty census is not a quiet box: it is a sandbox or a permissions regime, and the
        # answer to "is anyone in there" is unknown rather than no.
        return None
    return found


def image_running(names: Sequence[str]) -> bool | None:
    """Is any process with one of these image names alive? ``None`` when that cannot be answered.

    THE NAMES COME FROM THE CALLER, and that is the whole design. Which external tool a machine must
    not double-book is a fact about a PROJECT, never about this module -- bake one in and every
    other project inherits a census that knows about somebody else's software. The mechanism asks;
    the declaration names.

    WHY ADMISSION NEEDS IT AT ALL, given a per-class lock already exists: **a human who starts the
    tool by hand takes no lock.** Admission that consults only the lock is consulting a LEDGER of
    what the fleet started, and the point of measuring is that the ledger and the world differ by
    exactly the humans.

    NOT :func:`processes_matching`, which iterates python processes only -- a foreign binary is not
    python, so that function would answer "no" about a tool plainly on screen: confident, wrong, and
    safe-looking. Matching is on the process NAME, whole and case-insensitive, because that is the
    only field a foreign binary reliably exposes to a user who does not own it, and a SUBSTRING
    match would let one name claim an unrelated helper -- whose failure direction is a machine that
    never accepts that class of work again.

    ``None`` IS NOT FALSE. Without psutil, or where nothing is readable, "nothing is running" is an
    unfounded claim, and its failure direction is admitting exclusive work onto a tool in use.
    """
    if not HAVE_PSUTIL:
        return None
    wanted = {n.lower() for n in names}
    if not wanted:
        # An EMPTY requirement is satisfied, not unknown: a caller that declares no vendor is not
        # asking a question this function can fail to answer.
        return False
    readable = 0
    for proc in psutil.process_iter(['name']):
        name = (proc.info.get('name') or '').lower()
        if not name:
            continue
        readable += 1
        if name in wanted:
            return True
    return None if not readable else False


def subtree(root_pid: int) -> dict[int, int]:
    """``{pid: parent_pid}`` for ``root_pid`` and every descendant; the root maps to 0.

    Empty if the root no longer exists -- a stale pid is reported by the CALLER as "nothing to
    stop", never read as an empty success.
    """
    _require_psutil()
    try:
        proc = psutil.Process(root_pid)
    except psutil.NoSuchProcess:
        return {}
    tree = {root_pid: 0}
    for child in proc.children(recursive=True):
        try:
            tree[child.pid] = child.ppid()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return tree


def children_first(tree: dict[int, int]) -> list[int]:
    """The tree's pids ordered so every child precedes its parent; roots come last.

    THE HALF THAT MUST NOT HALF-SUCCEED. A parent killed before its children is exactly how the
    measured 2026-07-29 orphans were made: the children are REPARENTED and keep running, and nothing
    left names them -- they hold the machine-wide test lane and contend every subsequent
    measurement, while the stop reports success for the one thing it was asked to undo.

    Depth within the tree (edges walked to a root) is the order key, so this holds for any FOREST,
    including the multi-root set a whole-tree discovery returns.
    """

    def depth(pid: int) -> int:
        d = 0
        while tree.get(pid):
            pid = tree[pid]
            d += 1
        return d

    return sorted(tree, key=depth, reverse=True)


def as_forest(procs: Sequence[psutil.Process]) -> dict[int, int]:
    """``{pid: parent_pid}`` for a discovered set, ready for :func:`children_first`.

    A process whose parent cannot be read maps to 0 -- it is treated as a root, which puts it LAST.
    That is the safe direction: an unreadable parent is not a licence to kill it early.
    """
    forest: dict[int, int] = {}
    for proc in procs:
        try:
            forest[proc.pid] = proc.ppid()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            forest[proc.pid] = 0
    return forest


def _terminate(pid: int, *, force: bool) -> None:
    """One terminate/kill via psutil.

    `NoSuchProcess` is PROGRESS here; the post-kill re-check is the only verdict. `AccessDenied` is
    re-raised: a process we may not touch is exactly what the caller must learn about, not a detail
    to swallow into a success.
    """
    try:
        proc = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return
    if force:
        proc.kill()
    else:
        proc.terminate()


def alive(pids: Sequence[int]) -> list[int]:
    """Which of these pids still exist, in the order given."""
    _require_psutil()
    return [p for p in pids if psutil.pid_exists(p)]


def _wait_gone(pids: Sequence[int], timeout_s: float) -> list[int]:
    """Which of ``pids`` are STILL alive after waiting up to ``timeout_s`` for them to go.

    Returns as soon as none are left, so a clean stop costs one process-table read rather than the
    whole ceiling.
    """
    deadline = time.monotonic() + timeout_s
    remaining = alive(pids)
    while remaining and time.monotonic() < deadline:
        time.sleep(0.05)
        remaining = alive(pids)
    return remaining


def kill_ordered(order: Sequence[int]) -> list[int]:
    """Graceful pass over the whole order, then force the survivors. Returns WHO IS LEFT.

    THE RETURN IS A MEASUREMENT, NOT AN EXIT CODE. A stop tool that reports its own kill COUNT is
    reporting what it ATTEMPTED: 2026-08-05 is on record as the day a cleanup reported success while
    36 processes and 7.4 GB stayed up. So this re-checks the process table afterwards and hands back
    the survivors; an empty list is the only thing a caller may read as "stopped".

    ESCALATION is graceful first, always: a console process ignores the graceful form by design, so
    the force pass is the NORM rather than an error -- but a worker mid-write to a log gets the
    chance to finish the line. When the graceful pass finished the job there is no settle and no
    force pass at all.

    ORDER IS PRESERVED THROUGH BOTH PASSES. The force pass FILTERS the children-first order rather
    than rebuilding it, so a survivor's parent is still never touched before the survivor itself.
    """
    _require_psutil()
    order = list(order)
    for pid in order:
        _terminate(pid, force=False)
    survivors = _wait_gone(order, SETTLE_S)
    if not survivors:
        return []
    for pid in order:
        if pid in survivors:
            _terminate(pid, force=True)
    return _wait_gone(order, SETTLE_S)


def reap(targets: Sequence[psutil.Process]) -> list[int]:
    """Terminate a discovered SET and report the pids that survived. Children-first.

    The counterpart to :func:`kill_ordered` for callers holding processes rather than pids: it
    orders them (a set discovered structurally is a FOREST, not a list), escalates, and verifies.

    WAITING, NOT SLEEPING: `wait_procs` returns as soon as they are gone, so the grace is a CEILING
    on the wait rather than a fixed cost. The final answer still comes from re-reading the process
    table, because `wait_procs` reports about the objects it was handed and a caller needs the
    truth about the machine.
    """
    _require_psutil()
    targets = list(targets)
    if not targets:
        return []
    by_pid = {proc.pid: proc for proc in targets}
    order = children_first(as_forest(targets))
    for pid in order:
        with contextlib.suppress(psutil.Error):
            by_pid[pid].terminate()
    _gone, still = psutil.wait_procs(targets, timeout=REAP_GRACE_S)
    for pid in children_first(as_forest(still)):
        with contextlib.suppress(psutil.Error):
            by_pid[pid].kill()
    psutil.wait_procs(still, timeout=REAP_KILL_S)
    return alive(order)


# ------------------------------------------------------------------ the census, and the stop tool
#
# WHAT THESE ARE AND WHY THEY ARE HERE. Everything above answers "which processes" and "make them
# stop". These two answer "how much are they costing" and "run that as ONE operation that cannot
# half-succeed" -- the shapes every consumer of this module had written for itself. Neither knows
# WHICH processes it is about: the population arrives, and the caller who owns the tree supplies it.


def census(processes: Sequence[psutil.Process]) -> dict:
    """One measurement over a supplied population: count, total/max RSS, and system free memory.

    THE POPULATION IS AN ARGUMENT AND HAS NO DEFAULT. "Which processes are mine" is the one question
    this package must never answer on its own -- a census that guessed would be a plausible number
    about somebody else's tree, and plausible is the failure mode that survives review.

    Processes that die mid-walk are DROPPED, not counted at zero: a census is a snapshot of a moving
    table, and attributing 0 bytes to a process that merely exited understates the total in exactly
    the direction that makes a machine look idle.
    """
    _require_psutil()
    rss: list[int] = []
    rows: list[dict] = []
    for proc in processes:
        try:
            info = proc.as_dict(['pid', 'ppid', 'memory_info', 'create_time'])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        value = info['memory_info'].rss
        rss.append(value)
        rows.append(
            {
                'pid': info['pid'],
                'ppid': info['ppid'],
                'rss': value,
                'age_s': round(time.time() - info['create_time'], 1),
            }
        )
    return {
        'time': time.strftime('%H:%M:%S'),
        'count': len(rows),
        'total_rss': sum(rss),
        'max_rss': max(rss, default=0),
        'available': psutil.virtual_memory().available,
        'procs': sorted(rows, key=lambda row: -row['rss']),
    }


def render_census(snapshot: dict) -> list[str]:
    """The summary line plus one line per process, largest first."""
    gb = 1024**3
    lines = [
        (
            f'{snapshot["time"]}  procs={snapshot["count"]}  total={snapshot["total_rss"] / gb:.2f} GB  '
            f'max={snapshot["max_rss"] / 1024**2:.0f} MB  available={snapshot["available"] / gb:.2f} GB'
        )
    ]
    lines.extend(
        f'  pid {row["pid"]:>7} ppid {row["ppid"]:>7}  {row["rss"] / 1024**2:>8.1f} MB  age {row["age_s"]:>8.1f}s'
        for row in snapshot['procs']
    )
    return lines


def reap_and_recount(targets: Sequence[psutil.Process], *, recount: Callable[[], Sequence[psutil.Process]]) -> dict:
    """Reap ``targets``, then take a FRESH census and return it. The verdict is in that census.

    ``recount`` IS REQUIRED AND TAKES NO DEFAULT, and it is the entire point of the function. The
    honest question after a kill is not "did the processes we aimed at go" but "is anything still
    running" -- 2026-08-05 is on record as the day a cleanup reported success while 36 processes and
    7.4 GB stayed up, because it read :func:`reap`'s report about the set it was handed. Re-running
    DISCOVERY is what closes that, and only the caller knows what discovery means for its tree.
    """
    reap(targets)
    return census(recount())


#: What :func:`stop` answers with. A REPORT PLUS A CODE, because the three outcomes -- stopped,
#: nothing to stop, still alive -- are not orderable and a caller that had to re-derive which one
#: happened from the survivor list would be re-deriving the verdict this function exists to give.
STOPPED = 0
STILL_ALIVE = 1
NOTHING_TO_STOP = 2


def stop(
    *,
    pid: int | None,
    discover: Callable[[], Sequence[psutil.Process]],
    dry_run: bool,
) -> tuple[list[str], int]:
    """Stop one subtree or everything ``discover`` finds. Returns ``(report lines, exit code)``.

    CHILDREN-FIRST is not a preference. Killing the parent first is how orphans are made: the
    children are reparented, keep running, and nothing left names them -- the measured defect (three
    times in one session, 2026-07-29) this whole path exists to prevent.

    THE ASYMMETRY BETWEEN THE TWO TARGETS IS THE POINT, and it lives here rather than at a call site
    because getting it wrong is invisible: both spellings report success.

    * A NAMED SUBTREE is verified by :func:`kill_ordered`'s survivor list, and that is the whole
      truth -- there is nothing wider to ask about one pid.
    * ``discover``'s POPULATION is not. Anything discovery still finds afterwards means the sweep is
      not stopped, whether it was aimed at or not; a process that respawned, or that the first walk
      missed, is exactly the case a survivor list cannot see. So this re-runs discovery and folds
      the result in.

    ``discover`` HAS NO DEFAULT. "Which processes are mine" is the one question this package must
    never answer on its own, and it is needed even for the ``pid`` path's absence -- passing it is
    what makes the caller state the scope it is working in.

    A MISSING TARGET IS ``NOTHING_TO_STOP``, NEVER ``STOPPED``. A stale pid means "already gone",
    which must not render identically to "I stopped it".
    """
    if pid is not None:
        tree = subtree(pid)
        recount: Callable[[], Sequence[int]] | None = None
        if not tree:
            return [f'pid {pid} does not exist -- nothing to stop'], NOTHING_TO_STOP
    else:
        tree = as_forest(discover())
        recount = lambda: [found.pid for found in discover()]  # one expression, one use
        if not tree:
            return ['no matching processes found -- nothing to stop'], STOPPED

    order = children_first(tree)
    plan = [f'  pid {each:>7}  (parent {tree.get(each) or "-"})' for each in order]
    if dry_run:
        return [f'--dry-run: would kill {len(order)} process(es), children first:', *plan], STOPPED

    lines = [f'killing {len(order)} process(es), children first:', *plan]
    survivors = kill_ordered(order)
    if recount is not None:
        survivors = sorted({*survivors, *recount()})
    if survivors:
        return [*lines, f'FAILED -- still alive after kill: {survivors}'], STILL_ALIVE
    return [*lines, f'stopped {len(order)} process(es); verified none remain'], STOPPED
