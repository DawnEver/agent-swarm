"""Process accounting: attribution, self-exclusion, and a stop that cannot half-succeed.

PROVENANCE. Every function under test was extracted from motronics' `scripts/proc/proc_probe.py`
and `scripts/proc/stop_sweep.py`, where it had been running and measured for months. The numbers in
these docstrings are measurements, not invented examples.

THESE TESTS SPAWN REAL PROCESSES on purpose. The three defects this module exists to prevent are
all invisible to a mocked process table: a worker that names nothing on its own command line, a
caller that matches its own query, and a parent killed before its children. Each of those is a
property of the actual table, so a double that answers from a dict would agree with any
implementation, including the broken ones.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest

from agent_swarm import procs

#: How long a spawned helper lives if a test fails to clean it up. Long enough to be found, short
#: enough that a crashed run does not leave the box holding sleepers for the afternoon.
_HELPER_LIFETIME_S = 60

#: A spawned process is not in the table the instant `Popen` returns, and a grandchild is spawned by
#: a python interpreter that has to start first. Poll to a deadline rather than sleeping a guess --
#: a fixed sleep is either flaky or slow, and usually both.
_DISCOVERY_DEADLINE_S = 20.0


def _until(predicate, deadline_s: float = _DISCOVERY_DEADLINE_S):
    """Poll ``predicate`` until it returns something truthy, or give up and return its last answer."""
    end = time.monotonic() + deadline_s
    answer = predicate()
    while not answer and time.monotonic() < end:
        time.sleep(0.05)
        answer = predicate()
    return answer


@pytest.fixture
def spawn(tmp_path):
    """Spawn helper python processes and guarantee they are gone afterwards.

    The teardown is deliberately blunt (kill the whole subtree, force, no grace): a test fixture is
    not the thing being measured, and a leaked sleeper would contend every subsequent measurement --
    which is the exact failure these tests are about.
    """
    started: list[subprocess.Popen] = []

    def _spawn(*argv: str, cwd: Path | None = None, with_child: bool = False) -> subprocess.Popen:
        if with_child:
            # The parent NAMES the marker; the child names something else entirely and is reachable
            # only as a descendant. That is the xdist shape: the shim carries the project token, the
            # worker's argv is `-c "import sys;exec(eval(sys.stdin.readline()))"` and names nothing.
            code = (
                'import subprocess, sys, time\n'
                f'subprocess.Popen([sys.executable, "-c", "import time; time.sleep({_HELPER_LIFETIME_S})",'
                ' "anonymous-worker"])\n'
                f'time.sleep({_HELPER_LIFETIME_S})\n'
            )
        else:
            code = f'import time; time.sleep({_HELPER_LIFETIME_S})'
        proc = subprocess.Popen([sys.executable, '-c', code, *argv], cwd=str(cwd) if cwd else None)
        started.append(proc)
        return proc

    yield _spawn

    for proc in started:
        try:
            parent = psutil.Process(proc.pid)
            victims = [*parent.children(recursive=True), parent]
        except psutil.NoSuchProcess:
            proc.poll()
            continue
        for victim in victims:
            with contextlib.suppress(psutil.Error):
                victim.kill()
        psutil.wait_procs(victims, timeout=5)
        proc.poll()


class TestAttributionIsStructural:
    """A process belongs to a tree because of WHERE it runs, not because of what it is called.

    THE MEASUREMENT THIS EXISTS FOR (motronics, 2026-07-28), against a live 8-worker gate:

        command-line match   12 procs   0.21 GB total   130 MB max
        process tree         22 procs   4.85 GB total   672 MB max

    A 23x underestimate, produced by the command a rules file named as the thing to run BEFORE
    quoting a memory figure.
    """

    def test_a_process_that_NAMES_NOTHING_is_attributed_by_its_cwd(self, tmp_path):
        """THE DISCRIMINATING CASE. An xdist worker's argv is
        `-u -c "import sys;exec(eval(sys.stdin.readline()))"` under a uv-managed interpreter: no
        token anywhere. Its inherited working directory is the only mark it carries, and it KEEPS
        that mark after the controller dies.
        """
        root = tmp_path / 'checkout'
        (root / 'sub').mkdir(parents=True)
        anonymous = ['C:/uv/python.exe', '-u', '-c', 'import sys;exec(eval(sys.stdin.readline()))']

        assert procs.is_tree_process(token='mytoken', root=root, cwd=str(root / 'sub'), cmdline=anonymous)

    def test_the_same_anonymous_process_elsewhere_is_NOT_ours(self, tmp_path):
        """The control for the test above: without it, a function returning True unconditionally
        would pass.
        """
        root = tmp_path / 'checkout'
        root.mkdir()
        elsewhere = tmp_path / 'somebody-else'
        elsewhere.mkdir()
        anonymous = ['C:/uv/python.exe', '-u', '-c', 'import sys;exec(eval(sys.stdin.readline()))']

        assert not procs.is_tree_process(token='mytoken', root=root, cwd=str(elsewhere), cmdline=anonymous)

    def test_an_executable_inside_the_tree_votes(self, tmp_path):
        """The `.venv` launcher shim, whose cwd may be anywhere."""
        root = tmp_path / 'checkout'
        (root / '.venv' / 'Scripts').mkdir(parents=True)
        exe = root / '.venv' / 'Scripts' / 'python.exe'
        exe.touch()

        assert procs.is_tree_process(token='mytoken', root=root, exe=str(exe), cwd=str(tmp_path))

    def test_a_command_line_naming_the_tree_votes(self, tmp_path):
        root = tmp_path / 'checkout'
        root.mkdir()
        assert procs.is_tree_process(token='mytoken', root=root, cmdline=['python', '-m', 'pytest', 'mytoken/src'])

    def test_a_SIBLING_checkout_is_not_in_the_tree(self, tmp_path):
        """Containment is by path SEGMENT. `str.startswith` would claim `<root>-old`, and a fan-out
        session is exactly where such a sibling exists.
        """
        root = tmp_path / 'lane'
        root.mkdir()
        sibling = tmp_path / 'lane-old'
        sibling.mkdir()

        assert not procs.under(sibling, root.resolve())
        assert not procs.is_tree_process(token='mytoken', root=root, cwd=str(sibling))

    def test_the_ROOT_need_not_be_pre_resolved(self, tmp_path):
        """THE SCOPE THAT WAS RIGHT BY ACCIDENT. `under` used to canonicalise only the LEFT side,
        and every caller in this module happened to hand it an already-resolved root -- so the gap
        was invisible while the docstring said nothing about the requirement.

        A root arriving from an environment variable, a config file or a `%TEMP%` expansion is not
        canonical, and the failure direction is the bad one: False for EVERY process, i.e. a sweep
        that finds nothing and reports success.
        """
        root = tmp_path / 'checkout'
        (root / 'sub').mkdir(parents=True)
        # `..`, NOT `.` -- pathlib silently drops a `.` component at construction, so a root spelled
        # with one is already canonical and the test would assert nothing. Mutation testing caught
        # exactly that: reverting the fix left this test green.
        non_canonical = root / '..' / 'checkout'

        assert procs.under(root / 'sub', non_canonical)
        assert procs.is_tree_process(token='nope', root=non_canonical, cwd=str(root / 'sub'))

    def test_the_two_sides_may_differ_in_CASE(self, tmp_path):
        """psutil hands back whatever spelling each process reported, so the two sides do differ in
        case -- including for a path that no longer exists, where `resolve()` cannot repair it.

        WHAT SUPPLIES THIS PROPERTY IS PATHLIB, NOT US, and saying so is the point of the test. I
        added an `os.path.normcase` here on the reasoning that `relative_to` is case-sensitive.
        That reasoning is FALSE: `WindowsPath` already folds case, measured --
        `Path('...\\DELETED-LANE\\SUB').relative_to(Path('...\\Deleted-Lane'))` returns `SUB`, and on
        POSIX `normcase` is a no-op. So the line could not be distinguished from its own absence by
        any test I could write, and it was deleted rather than kept as an untestable comfort.

        This test survives it because the PROPERTY is what matters and is genuinely relied on: it
        still goes red if containment is ever rewritten as a string comparison, which is the change
        that would actually take it away.
        """
        gone = tmp_path / 'Deleted-Lane'
        worker_cwd = str(gone / 'sub').upper()

        assert procs.under(worker_cwd, gone)
        assert not procs.under(worker_cwd, tmp_path / 'other-lane')

    @pytest.mark.skipif(not Path(r'C:\PROGRA~1').exists(), reason='needs a Windows 8.3 alias')
    def test_a_WINDOWS_SHORT_PATH_names_the_same_directory(self):
        """A REAL alias, not a constructed one: `C:\\PROGRA~1` IS `C:\\Program Files`, and it exists
        even on boxes where 8.3 CREATION has since been disabled.

        MEASURED, and it is why the fix is in `under` rather than in some replacement for
        `resolve()`: `resolve()` DOES expand the alias (it asks the filesystem via
        `GetFinalPathNameByHandle`), so a canonicalised pair compares equal. What raises `ValueError`
        is an UNRESOLVED root against a resolved path -- the exact asymmetry this function had.
        """
        short = Path(r'C:\PROGRA~1')
        long = Path(r'C:\Program Files')

        assert procs.under(short / 'Common Files', long)
        assert procs.under(long / 'Common Files', short)
        assert not procs.under(long, short / 'Common Files'), 'containment is not symmetric'

    def test_a_path_that_no_longer_EXISTS_is_still_compared_by_segment(self, tmp_path):
        """A NAMED HOLE, pinned so that it stays named. `resolve()` can only expand an alias for a
        path that EXISTS, because the mapping lives on disk -- so a process whose worktree was
        deleted keeps whatever spelling it had. Case and `..` still normalise (`normcase` does not
        touch the disk); an 8.3 alias cannot.

        What must hold either way, and is the direction that would matter: a vanished path is never
        attributed to the WRONG tree.
        """
        root = tmp_path / 'checkout'
        gone = root / 'deleted-worktree'

        assert procs.under(gone, root), 'a segment comparison does not require the leaf to exist'
        assert not procs.under(gone, tmp_path / 'other')

    def test_missing_fields_do_not_vote(self, tmp_path):
        """`psutil` raises `AccessDenied` per FIELD on a process this user does not own. A missing
        field is absence of evidence, not evidence of absence.
        """
        assert not procs.is_tree_process(token='mytoken', root=tmp_path, exe=None, cwd=None, cmdline=())

    def test_the_TOKEN_and_the_ROOT_are_REQUIRED(self, tmp_path):
        """THE REASON THIS MODULE WAS SPLIT OUT. The original held `REPO_TOKEN = 'motronics'` and
        called `repo_root()` -- one project's two facts compiled into fleet infrastructure, invisible
        because it worked for that project. A DEFAULT would restore exactly that: a caller who omits
        the tree would get a plausible census of somebody else's checkout instead of an error.
        """
        with pytest.raises(TypeError):
            procs.is_tree_process(cwd='/somewhere')  # type: ignore[call-arg]
        with pytest.raises(TypeError):
            procs.is_tree_process(token='mytoken')  # type: ignore[call-arg]
        with pytest.raises(TypeError):
            procs.is_tree_process(root=tmp_path)  # type: ignore[call-arg]

    def test_they_are_KEYWORD_only(self, tmp_path):
        """So a call site cannot silently swap the two, which would type-check and mean nothing."""
        with pytest.raises(TypeError):
            procs.is_tree_process('mytoken', tmp_path)  # type: ignore[misc]


class TestTheCensusExcludesItself:
    """A probe is never part of what it measures -- and a sweep must not kill the shell that ran it.

    MEASURED THE HARD WAY, twice in one day (motronics, 2026-08-04). A caller mentioning the token
    on its own command line matched its own query, so a teardown loop killed the caller: exit code
    15. The morning's fix elsewhere had a negative control asserting an unmatchable name yields 0 --
    which cannot catch this, because it tests a name NOTHING has while the defect is a name the
    CALLER has.
    """

    def test_this_process_and_its_ancestors_are_all_excluded(self):
        excluded = procs.self_and_ancestors()
        assert os.getpid() in excluded
        assert os.getppid() in excluded, 'the shell/agent that launched the sweep must not be a target'

    def test_a_query_that_matches_OUR_OWN_command_line_does_not_return_us(self):
        """THE DISCRIMINATING TEST, and it uses a token our own argv certainly carries: the name of
        the interpreter running this suite is `cmdline[0]` of this very process. A name-match census
        would return us and our parents; this one must return neither.

        Note what this does NOT assert: that the query is empty. It should find every OTHER python
        on the box, which is what makes the exclusion structural rather than a filter that
        accidentally matches nothing.

        AND IT NAMES THE PIDS DIRECTLY rather than intersecting with `self_and_ancestors()`. Written
        that way, sabotaging the exclusion to return an empty set passes -- the same broken function
        supplies both sides, so the assertion agrees with any implementation. Measured: that is
        exactly what happened when this was checked by mutation.
        """
        token = Path(sys.executable).name
        found = {proc.pid for proc in procs.processes_matching(token)}

        assert os.getpid() not in found, 'the census returned the process doing the censusing'
        assert os.getppid() not in found, 'a sweep would have killed the shell that launched it'
        assert not (found & procs.self_and_ancestors())

    def test_the_descendant_closure_finds_a_process_that_names_NOTHING(self, spawn, tmp_path):
        """The xdist shape, spawned for real: a parent carrying the marker, and a child whose argv
        carries something else entirely. Only the closure over children reaches the child -- and the
        child is the one that holds the memory.
        """
        marker = f'swarm-procs-{os.getpid()}-{time.time_ns()}'
        parent = spawn(marker, cwd=tmp_path, with_child=True)

        def both_found():
            found = {p.pid for p in procs.processes_matching(marker)}
            return found if parent.pid in found and len(found) >= 2 else None

        found = _until(both_found)
        assert found, 'the parent and its anonymous child should both be in the census'
        assert parent.pid in found
        anonymous = found - {parent.pid}
        assert anonymous, 'the child names nothing; only the child-closure can reach it'


class TestTheTreeCensus:
    """`processes_in_tree` answers "what of MY tree is running", structurally and per-orphan."""

    def test_a_process_running_in_the_tree_is_found_without_naming_it(self, spawn, tmp_path):
        """No living relative is needed and no token is carried: cwd alone attributes it. That is
        the axis nobody varied in the first fix, and it cost 34 uncounted orphans holding ~9 GB
        (motronics, 2026-08-04).
        """
        root = tmp_path / 'checkout'
        root.mkdir()
        child = spawn('anonymous-worker', cwd=root)

        found = _until(lambda: {p.pid for p in procs.processes_in_tree(token='no-such-token', root=root)})
        assert child.pid in found

    def test_a_process_OUTSIDE_the_tree_is_not(self, spawn, tmp_path):
        root = tmp_path / 'checkout'
        root.mkdir()
        elsewhere = tmp_path / 'somebody-else'
        elsewhere.mkdir()
        stranger = spawn('anonymous-worker', cwd=elsewhere)

        # Wait for it to exist at all, so the assertion is about attribution and not about timing.
        _until(lambda: psutil.pid_exists(stranger.pid))
        found = {p.pid for p in procs.processes_in_tree(token='no-such-token', root=root)}
        assert stranger.pid not in found

    def test_the_TOKEN_and_the_ROOT_are_REQUIRED_here_too(self, tmp_path):
        with pytest.raises(TypeError):
            procs.processes_in_tree()  # type: ignore[call-arg]
        with pytest.raises(TypeError):
            procs.processes_in_tree(root=tmp_path)  # type: ignore[call-arg]


class TestOccupants:
    """The FACT layer: who is working inside this directory, needing zero cooperation."""

    def test_a_process_working_in_the_tree_is_an_occupant(self, spawn, tmp_path):
        lane = tmp_path / 'lane'
        lane.mkdir()
        worker = spawn('anonymous', cwd=lane)

        found = _until(lambda: {p.pid for p in (procs.occupants(lane) or [])})
        assert worker.pid in found

    def test_a_SIBLING_lane_has_no_occupants(self, spawn, tmp_path):
        """`lane-old` is not inside `lane`. A prefix test says otherwise, and a fan-out is exactly
        where such a sibling exists -- with the failure direction "delete a worktree someone is in".
        """
        lane = tmp_path / 'lane'
        lane.mkdir()
        old = tmp_path / 'lane-old'
        old.mkdir()
        worker = spawn('anonymous', cwd=old)

        _until(lambda: psutil.pid_exists(worker.pid))
        assert worker.pid not in {p.pid for p in (procs.occupants(lane) or [])}

    def test_the_probe_does_not_report_ITSELF_as_an_occupant(self, tmp_path):
        """This test process's cwd is the repo, not `tmp_path` -- but its ancestors are excluded
        structurally, which is what stops a sweep of an occupied tree from taking out the shell.
        """
        assert not ({p.pid for p in (procs.occupants(Path.cwd()) or [])} & procs.self_and_ancestors())


class TestImageRunning:
    """Which tools a box must not double-book is the CONSUMER's declaration, never this module's."""

    def test_an_empty_requirement_is_SATISFIED_not_unknown(self):
        """A caller that declares no vendor is not asking a question this function can fail."""
        assert procs.image_running([]) is False

    def test_a_running_image_is_seen(self):
        assert procs.image_running([Path(sys.executable).name]) is True

    def test_an_absent_image_is_absent_not_unknown(self):
        assert procs.image_running(['no-such-tool-a5f3e1.exe']) is False

    def test_the_match_is_WHOLE_NAME_not_a_substring(self):
        """A substring match lets one declaration claim an unrelated helper, and its failure
        direction is a machine that never accepts that class of work again.
        """
        stem = Path(sys.executable).name[:3]
        assert procs.image_running([stem]) is False

    def test_the_match_is_case_insensitive(self):
        assert procs.image_running([Path(sys.executable).name.upper()]) is True

    def test_no_vendor_name_is_compiled_into_this_module(self):
        """The boundary an earlier engine broke by carrying a consumer's two FEA tools in its class
        set. The mechanism asks; the declaration names.

        SCOPE, stated because the reader will otherwise supply "everything": this checks the WHOLE
        source text of `procs.py` for VENDOR tool names only. It deliberately does not forbid the
        name of the project this module was extracted from -- that appears in provenance prose
        ("measured on motronics, 2026-07-28"), and a measurement whose origin is deleted is a number
        nobody can re-take. What must not appear is an executable consumer fact, and the tests above
        pin that positively: `token` and `root` have no defaults.
        """
        source = Path(procs.__file__).read_text(encoding='utf-8').lower()
        for vendor in ('femm', 'jmag', 'matlab', 'ansys', 'comsol'):
            assert vendor not in source, f'{vendor} is a consumer fact and must arrive as an argument'


class TestTheOptionalExtra:
    """`psutil` is an EXTRA. The package it lives in declares no runtime dependencies, and this
    module must not quietly make it one.

    THE ABSENT PATH IS TESTED BY SIMULATING THE ABSENCE, not by uninstalling: `HAVE_PSUTIL` is the
    single flag every entry point consults, so flipping it exercises exactly the branch a box
    without the extra would take. What that does NOT cover is an unguarded `import psutil` added
    elsewhere in the file later -- so the last test here checks the source for one.
    """

    def test_the_module_IMPORTS_without_the_extra(self):
        """If it raised on import, anything re-exporting `procs` would make `agent_swarm` itself
        unimportable -- an optional extra promoted to a hard dependency by the back door.
        """
        import agent_swarm

        assert agent_swarm is not None
        assert hasattr(procs, 'HAVE_PSUTIL')

    def test_a_census_answers_NONE_not_an_empty_list(self, monkeypatch, tmp_path):
        """`None` IS NOT AN EMPTY LIST, and the difference is a worktree deletion. "Nobody is in
        there" and "I could not look" have opposite safe responses.
        """
        monkeypatch.setattr(procs, 'HAVE_PSUTIL', False)

        assert procs.occupants(tmp_path) is None
        assert procs.image_running(['anything.exe']) is None

    def test_an_enumeration_RAISES_and_names_the_extra(self, monkeypatch, tmp_path):
        monkeypatch.setattr(procs, 'HAVE_PSUTIL', False)

        with pytest.raises(RuntimeError, match=r'agent-swarm\[procs\]'):
            procs.processes_in_tree(token='t', root=tmp_path)
        with pytest.raises(RuntimeError, match=r'agent-swarm\[procs\]'):
            procs.kill_ordered([1])

    def test_self_exclusion_without_psutil_excludes_NOTHING_and_that_is_safe(self, monkeypatch):
        """The one function that degrades to an empty answer rather than raising. It is safe ONLY
        because every caller of it raises first -- pinned above. Stated here so the next reader does
        not mistake the empty set for a licence to sweep.
        """
        monkeypatch.setattr(procs, 'HAVE_PSUTIL', False)
        assert procs.self_and_ancestors() == set()

    def test_psutil_is_imported_ONLY_inside_the_guarded_block(self):
        """The guard is worth nothing if a second, unguarded `import psutil` appears later in the
        file: the module would then fail at import on a box without the extra, and every test above
        would still pass HERE, where it IS installed. That is the shape where the check is present
        and the property is absent.
        """
        lines = Path(procs.__file__).read_text(encoding='utf-8').splitlines()
        imports = [i for i, line in enumerate(lines) if line.strip() == 'import psutil']
        assert len(imports) == 1, f'expected exactly one `import psutil`, found {len(imports)}'
        assert lines[imports[0] - 1].strip() == 'try:', 'the sole import must sit inside the try/except guard'


class TestTerminationIsChildrenFirst:
    """The half that must not half-succeed.

    MEASURED 2026-07-29, three times in one session: stopping a wrapper did not stop the pytest it
    launched. The wrapper died, the subtree survived as an ORPHAN, held the machine-wide test lane
    and contended every subsequent measurement -- while the stop reported success for the one thing
    it was asked to undo. Killing the parent first is exactly how those orphans were made: the
    children get reparented and nothing left names them.
    """

    def test_every_child_precedes_its_parent(self):
        tree = {100: 0, 101: 100, 102: 101}
        assert procs.children_first(tree) == [102, 101, 100]

    def test_it_holds_for_a_FOREST_not_only_a_single_root(self):
        """A whole-tree discovery returns many roots; ordering by depth is what makes one function
        serve both the `--pid` and the `--all` shapes.
        """
        order = procs.children_first({1: 0, 2: 1, 10: 0, 11: 10, 12: 11})
        assert order.index(2) < order.index(1)
        assert order.index(12) < order.index(11) < order.index(10)
        assert order[-2:] in ([1, 10], [10, 1])

    def test_an_unreadable_parent_is_ordered_LAST(self):
        """`as_forest` maps an unreadable parent to 0, i.e. a root. That is the SAFE direction: not
        knowing who the parent is, is not a licence to kill it early.
        """
        assert procs.children_first({7: 0}) == [7]

    def test_a_REAL_subtree_is_killed_children_first(self, spawn, tmp_path, monkeypatch):
        """Not a synthetic dict: the ordering is only worth anything if `subtree` discovers the
        descendants that `children_first` then orders.
        """
        parent = spawn('anonymous', cwd=tmp_path, with_child=True)

        def the_whole_subtree():
            tree = procs.subtree(parent.pid)
            return tree if len(tree) >= 2 else None

        tree = _until(the_whole_subtree)
        assert tree and len(tree) >= 2, 'the helper should have spawned a child'

        killed: list[int] = []
        real_terminate = procs._terminate

        def recording(pid: int, *, force: bool) -> None:
            killed.append(pid)
            real_terminate(pid, force=force)

        monkeypatch.setattr(procs, '_terminate', recording)

        survivors = procs.kill_ordered(procs.children_first(tree))

        first_touched: dict[int, int] = {}
        for index, pid in enumerate(killed):
            first_touched.setdefault(pid, index)
        assert first_touched[parent.pid] == max(first_touched.values()), (
            f'the root was touched before one of its descendants: {killed}'
        )
        assert survivors == [], f'the stop is only stopped when NOTHING remains: {survivors}'

    def test_a_stale_root_yields_an_EMPTY_tree_not_a_quiet_success(self, spawn, tmp_path):
        """The caller reports "nothing to stop"; an empty tree must never be read as a stop that
        succeeded.
        """
        doomed = spawn('anonymous', cwd=tmp_path)
        proc = psutil.Process(doomed.pid)
        proc.kill()
        psutil.wait_procs([proc], timeout=5)

        assert procs.subtree(doomed.pid) == {}


class TestTerminationVERIFIES:
    """The verdict comes from re-reading the process table, never from the kill's exit code.

    2026-08-05 is on record as the day a cleanup reported success while 36 processes and 7.4 GB
    stayed up. A stop that reports its own kill COUNT is reporting what it ATTEMPTED.
    """

    def test_a_kill_that_did_nothing_is_reported_as_a_SURVIVOR(self, spawn, tmp_path, monkeypatch):
        """THE DISCRIMINATING TEST. Every termination call returns cleanly and the process is still
        there -- exactly the shape of `taskkill` reporting success against a process it could not
        touch. An implementation trusting its own return value passes every other test here and
        fails this one.
        """
        survivor = spawn('anonymous', cwd=tmp_path)
        _until(lambda: psutil.pid_exists(survivor.pid))

        monkeypatch.setattr(procs, '_terminate', lambda pid, *, force: None)
        monkeypatch.setattr(procs, 'SETTLE_S', 0.2)

        assert procs.kill_ordered([survivor.pid]) == [survivor.pid]

    def test_a_finished_graceful_pass_does_not_force_anything(self, spawn, tmp_path, monkeypatch):
        """The force pass is the NORM (a console process ignores the graceful form by design), but
        it must not run when the graceful pass already emptied the order.
        """
        doomed = spawn('anonymous', cwd=tmp_path)
        proc = psutil.Process(doomed.pid)
        proc.kill()
        psutil.wait_procs([proc], timeout=5)

        forced: list[int] = []
        monkeypatch.setattr(procs, '_terminate', lambda pid, *, force: forced.append(pid) if force else None)

        assert procs.kill_ordered([doomed.pid]) == []
        assert forced == []

    def test_a_process_STILL_DYING_is_not_called_a_survivor(self, spawn, tmp_path):
        """WHY THE WAIT IS BOUNDED RATHER THAN A FIXED SLEEP, and this is the one behaviour the
        extraction changed. The original settled a flat 2 s and then looked ONCE, which makes the
        verdict a function of the sleep: measured while writing this file, a subtree that died
        correctly was reported as `[40012, 79740]` when the settle was shortened -- a stop that
        worked, reported as failed. A verdict that flips with an unrelated constant is not a
        verdict.
        """
        doomed = spawn('anonymous', cwd=tmp_path)
        _until(lambda: psutil.pid_exists(doomed.pid))

        assert procs.kill_ordered([doomed.pid]) == []

    def test_the_force_pass_KEEPS_the_children_first_order(self, monkeypatch):
        """The force pass FILTERS the order rather than rebuilding it, so a survivor's parent is
        still never touched before the survivor itself.
        """
        order = [102, 101, 100]
        seen: list[tuple[int, bool]] = []
        monkeypatch.setattr(procs, 'SETTLE_S', 0.0)
        monkeypatch.setattr(procs, '_terminate', lambda pid, *, force: seen.append((pid, force)))
        monkeypatch.setattr(procs, 'alive', lambda pids: list(pids))

        procs.kill_ordered(order)

        assert [pid for pid, force in seen if force] == order


class TestReap:
    """Terminate a discovered SET and report what survived."""

    def test_an_empty_target_set_is_a_no_op(self):
        assert procs.reap([]) == []

    def test_a_real_forest_is_reaped_and_verified(self, spawn, tmp_path):
        parent = spawn('anonymous', cwd=tmp_path, with_child=True)

        def the_whole_family():
            tree = procs.subtree(parent.pid)
            if len(tree) < 2:
                return None
            with contextlib.suppress(psutil.NoSuchProcess):
                return [psutil.Process(pid) for pid in tree]
            return None

        targets = _until(the_whole_family)
        assert targets and len(targets) >= 2

        assert procs.reap(targets) == []
        assert not procs.alive([p.pid for p in targets])


class TestASeedThatExitsMidWalkIsOmittedNotRaised:
    """MEASURED 2026-08-11: one full-suite run in 1223 died with `psutil.NoSuchProcess (pid=18116)`
    out of `close_over_children` -- a seed that exited between `psutil.Process(pid)` and
    `proc.children()`, two syscalls with a gap between them.

    A CENSUS WALKS PROCESSES IT DOES NOT OWN, so one vanishing mid-enumeration is the normal case,
    not the exceptional one. Raising there converts somebody else's ordinary exit into a failure of
    OUR read -- and it does so most often on a busy box, which is precisely when the census is being
    consulted. The flake also lands on whoever is gating at the time, reading as their defect.
    """

    def test_children_raising_NoSuchProcess_omits_the_seed(self, monkeypatch):
        class _Vanished:
            pid = 999999

            def children(self, recursive=False):  # noqa: ARG002 -- psutil's signature
                raise psutil.NoSuchProcess(self.pid)

        monkeypatch.setattr(procs.psutil, 'Process', lambda _pid: _Vanished())
        assert procs.close_over_children({999999}) == []

    def test_a_LIVE_seed_is_still_returned(self, monkeypatch):
        """The control. `except: continue` around the whole block passes the test above even if it
        also swallows every living process -- an omission that would report an empty fleet as
        calmly as a correct one."""

        class _Alive:
            pid = 4242

            def children(self, recursive=False):  # noqa: ARG002 -- psutil's signature
                return []

        monkeypatch.setattr(procs.psutil, 'Process', lambda _pid: _Alive())
        assert [p.pid for p in procs.close_over_children({4242})] == [4242]
