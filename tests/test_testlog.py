"""Reading a test-run log: the aggregation, and the population no durations table can see.

PROVENANCE. These tests came with `testlog.py` out of motronics' `scripts/gate/test_cost_report.py`
on 2026-08-12. The log fragments below are MEASURED shapes, reproduced from real runs -- the
ANSI-painted Timeout opener glued to a progress marker, the comma-less innermost-first native dump
with `Current thread` last, xdist's node-down printing right after the dump that explains it. They
are not invented examples, and a simplified fixture would agree with a parser that cannot read a
real log.

THE PROJECT NOUNS ARE GONE FROM THE FIXTURES on purpose. The originals named one project's source
tree in every stack frame, and a reader could not tell which parts of the parse depended on that.
Nothing does: what matters is that a frame is under the tests directory, and the parameter says so.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_swarm import testlog

#: No run-level markers. The originals passed a project's two gate markers; here the empty mapping
#: is the DEFAULT-FREE spelling of "look for none", and the tests that care pass their own.
_NO_RUN_MARKERS: dict[str, str] = {}

_SWEEP = 'tests/unit/workflow/test_sweep_axis_unification.py::test_cli_resolves'
_MNA = 'tests/unit/circuit/test_case_library.py::test_matches_analytic[buck_closed_pi]'
_AXIAL = 'tests/unit/workflow/test_rotation_sweep.py::test_reversing_rotation'

# Two runs of one suite: the same tests measured twice at different costs, plus setup and teardown
# phases that must NOT enter the call-phase headline.
_LOG_A = f"""\
[run aaa] start
============================ slowest 25 durations =============================
244.07s call     {_SWEEP}
143.16s call     {_MNA}
53.85s setup    {_AXIAL}
2.10s teardown {_MNA}
[run aaa] end
"""

_LOG_B = f"""\
[run bbb] start
============================ slowest 25 durations =============================
512.96s call     {_SWEEP}
[run bbb] end
"""

# The censored half: a faulthandler Timeout block. The opener is glued to the killed test's progress
# marker with ANSI paint (the measured shape); a non-main thread is dumped before MainThread; the
# test frame sits between site-packages frames above and source frames below.
_LOG_TIMEOUT = (
    '[run ccc] start\n'
    '\x1b[33ms\x1b[0m\x1b[31mF\x1b[0m+++++++++++++++++++++++++++++++++++ Timeout +++++++++++++++++++++++++++++++++++\n'
    '~~~~~~~~~~~~~~~~~~~~~ Captured stderr ~~~~~~~~~~~~~~~~~~~~~\n'
    'iteration 51/100, residual 0.0027%\n'
    '~~~~~~~~~~~~~~~~ Stack of Thread-1 (_monitor) (31076) ~~~~~~~~~~~~~~~~\n'
    '  File "C:\\py\\Lib\\threading.py", line 355, in wait\n'
    '~~~~~~~~~~~~~~~~~~~~~~~~~ Stack of MainThread (27216) ~~~~~~~~~~~~~~~~~~~~~~~~~\n'
    '  File "<frozen runpy>", line 198, in _run_module_as_main\n'
    '  File "D:\\repo\\.venv\\Lib\\site-packages\\_pytest\\python.py", line 167, in pytest_pyfunc_call\n'
    '  File "D:\\repo\\tests\\unit\\solver\\test_slow_convergence.py", line 88, in test_converges_within_budget\n'
    '  File "D:\\repo\\src\\pkg\\solver.py", line 171, in solve_it\n'
    '+++++++++++++++++++++++++++++++++++ Timeout +++++++++++++++++++++++++++++++++++\n'
    '[run ccc] end\n'
)

_KILLED_ID = 'tests/unit/solver/test_slow_convergence.py::test_converges_within_budget'

# A native crash, modelled on a real 2026-07-30 log: no tilde banners, comma-less frames,
# innermost-first, `Current thread` LAST, and xdist's node-down right after the dump.
_LOG_FATAL = (
    '[gw5] win32 -- Python 3.12.12\n'
    '.............................. [ 99%]\n'
    'Windows fatal exception: access violation\n'
    'Thread 0x00007cb4 (most recent call first):\n'
    '  File "C:\\py\\Lib\\threading.py", line 359 in wait\n'
    'Current thread 0x00006348 (most recent call first):\n'
    '  File "D:\\repo\\src\\pkg\\fem\\_assemble.py", line 231 in _assemble_iteration\n'
    '  File "D:\\repo\\src\\pkg\\nonlinear\\__init__.py", line 406 in solve\n'
    '  File "D:\\repo\\.venv\\Lib\\site-packages\\_pytest\\python.py", line 167 in pytest_pyfunc_call\n'
    '  File "D:\\repo\\tests\\integration\\test_energy.py", line 55 in test_static_energy_matches\n'
    '[gw5] node down: Not properly terminated\n'
)


def _parse(text: str, log: str, **kwargs):
    return testlog.parse_log(text, log, run_markers=_NO_RUN_MARKERS, **kwargs)


def _load(paths, **kwargs):
    return testlog.load_corpus(paths, run_markers=_NO_RUN_MARKERS, **kwargs)


def _write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding='utf-8')
    return path


# --- the durations half: cross-run aggregation ---------------------------------------------


def test_duration_rows_are_parsed_with_phase_seconds_and_nodeid():
    rows, events = _parse(_LOG_A, 'a.log')
    assert events == []
    by_phase = {(r.phase, r.test_id): r.seconds for r in rows}
    assert by_phase[('call', _SWEEP)] == pytest.approx(244.07)
    assert by_phase[('setup', _AXIAL)] == pytest.approx(53.85)
    assert by_phase[('teardown', _MNA)] == pytest.approx(2.10)
    assert all(r.log == 'a.log' for r in rows)


def test_aggregation_across_logs_gives_runs_total_mean_and_spread(tmp_path: Path):
    corpus = _load([_write(tmp_path, 'a.log', _LOG_A), _write(tmp_path, 'b.log', _LOG_B)])
    stats = testlog.aggregate(corpus.rows, lambda r: r.test_id)
    sweep = stats[_SWEEP]
    assert sweep.runs == 2
    assert sweep.total_s == pytest.approx(244.07 + 512.96)
    assert sweep.mean_s == pytest.approx((244.07 + 512.96) / 2)
    assert sweep.min_s == pytest.approx(244.07)
    assert sweep.max_s == pytest.approx(512.96)
    # The spread is the point: 2.10x between two runs of one test, and a single reading of either
    # would have been an anecdote with a decimal point.
    assert sweep.spread == pytest.approx(512.96 / 244.07)
    # A test seen once has spread exactly 1.0 -- "no spread measured", not "no spread".
    assert stats[_MNA].spread == pytest.approx(1.0)


def test_setup_and_teardown_stay_out_of_the_call_headline(tmp_path: Path):
    """The 53.85s SETUP must not inflate a call cost: a fixture's time is shared by every test that
    uses it, so charging it to one file blames the wrong file.
    """
    corpus = _load([_write(tmp_path, 'a.log', _LOG_A)])
    stats = testlog.aggregate(corpus.rows, lambda r: r.test_id)
    assert _AXIAL not in stats
    assert stats[_MNA].total_s == pytest.approx(143.16)


def test_file_and_directory_aggregates_sum_their_tests(tmp_path: Path):
    corpus = _load([_write(tmp_path, 'a.log', _LOG_A), _write(tmp_path, 'b.log', _LOG_B)])
    by_file = testlog.aggregate(corpus.rows, lambda r: testlog.file_of(r.test_id))
    sweep_file = 'tests/unit/workflow/test_sweep_axis_unification.py'
    assert by_file[sweep_file].runs == 2
    assert by_file[sweep_file].total_s == pytest.approx(244.07 + 512.96)
    mna_file = 'tests/unit/circuit/test_case_library.py'
    assert by_file[mna_file].runs == 1
    by_dir = testlog.aggregate(corpus.rows, lambda r: testlog.dir_of(r.test_id))
    assert by_dir['tests/unit/circuit'].total_s == pytest.approx(143.16)
    assert by_dir['tests/unit/workflow'].total_s == pytest.approx(244.07 + 512.96)


def test_ansi_paint_does_not_hide_a_duration_row():
    """A row that fails to parse only because of terminal paint is a silent hole in the population."""
    painted = '\x1b[33m244.07s\x1b[0m call     tests/unit/test_x.py::test_y\n'
    rows, _ = _parse(painted, 'p.log')
    (row,) = rows
    assert row.seconds == pytest.approx(244.07)
    assert row.test_id == 'tests/unit/test_x.py::test_y'


# --- the censored half: kills the durations table cannot see -------------------------------


def test_a_timeout_block_yields_the_stack_top_test_id():
    _, events = _parse(_LOG_TIMEOUT, 't.log')
    (event,) = [e for e in events if e.kind == 'timeout']
    assert event.test_id == _KILLED_ID
    assert event.log == 't.log'


def test_a_killed_test_is_reported_with_zero_surviving_duration_rows(tmp_path: Path):
    """THE MEASUREMENT THIS MODULE EXISTS FOR: the killed test appears in the CENSORED section, and
    its surviving-row count is 0 even though other tests' rows exist in the corpus.
    """
    corpus = _load([_write(tmp_path, 'a.log', _LOG_A), _write(tmp_path, 't.log', _LOG_TIMEOUT)])
    censored = testlog.render(corpus).split('CENSORED TESTS', 1)[1]
    assert _KILLED_ID in censored
    line = next(line for line in censored.splitlines() if _KILLED_ID in line)
    assert '0 surviving call row(s)' in line
    assert 't.log' in line


def test_the_killed_test_never_enters_the_survivor_tables(tmp_path: Path):
    """The other half, and the reason the two sections are separate: a killed test in the survivor
    tables would be a cost figure for a run that never finished.
    """
    corpus = _load([_write(tmp_path, 'a.log', _LOG_A), _write(tmp_path, 't.log', _LOG_TIMEOUT)])
    survivors = testlog.render(corpus).split('CENSORED TESTS', 1)[0]
    assert _KILLED_ID not in survivors


def test_a_timeout_without_a_test_frame_is_unattributed_but_counted():
    """A stack with no test frame still censors a test -- we just cannot name it. Dropping the event
    would understate the censored population; naming it would fabricate. 'Unattributed' is honest.
    """
    block = (
        '+++ Timeout +++\n'
        '~~~~~ Stack of MainThread (1) ~~~~~\n'
        '  File "C:\\py\\Lib\\threading.py", line 355, in wait\n'
        '+++ Timeout +++\n'
    )
    _, events = _parse(block, 'u.log')
    (event,) = [e for e in events if e.kind == 'timeout']
    assert event.test_id is None


def test_an_unclosed_timeout_block_is_still_a_kill():
    """A log cut mid-block -- the kill IS why the log ends -- is still one event."""
    block = (
        '+++ Timeout +++\n'
        '~~~~~ Stack of MainThread (1) ~~~~~\n'
        '  File "D:\\repo\\tests\\unit\\test_x.py", line 9, in test_cut_mid_block\n'
    )
    _, events = _parse(block, 'cut.log')
    assert [e.test_id for e in events] == ['tests/unit/test_x.py::test_cut_mid_block']


def test_a_crashed_worker_names_its_test_exactly():
    """xdist restarts re-report the same test; one kill is one event, or a restarted run would count
    its worst test `--max-worker-restart` times.
    """
    crash = (
        "worker 'gw0' crashed while running 'tests/integration/test_cutcell.py::test_route[a-b]'\n"
        "worker 'gw1' crashed while running 'tests/integration/test_cutcell.py::test_route[a-b]'\n"
    )
    _, events = _parse(crash, 'c.log')
    crashes = [e for e in events if e.kind == 'worker-crash']
    assert [e.test_id for e in crashes] == ['tests/integration/test_cutcell.py::test_route[a-b]']


def test_a_fatal_dump_recovers_the_test_from_the_current_thread_section():
    _, events = _parse(_LOG_FATAL, 'f.log')
    (event,) = [e for e in events if e.kind == 'fatal-exception']
    # The dump is innermost-FIRST, so the test function is the OUTERMOST test frame.
    assert event.test_id == 'tests/integration/test_energy.py::test_static_energy_matches'
    assert 'access violation' in event.detail


def test_the_node_down_after_a_fatal_dump_is_the_same_death():
    """It prints because of the dump, not in addition to it. Counting both doubles every crash."""
    _, events = _parse(_LOG_FATAL, 'f.log')
    assert len(events) == 1


def test_a_fatal_dump_without_a_test_frame_carries_the_crash_site():
    """Measured 2026-07-30: a worker died in a native library with no test frame visible. The event
    must still exist, carrying where it died -- unattributed, but not invisible.
    """
    log = (
        'Windows fatal exception: access violation\n'
        'Current thread 0x1 (most recent call first):\n'
        '  File "D:\\repo\\src\\pkg\\fem.py", line 9 in assemble\n'
        '[gw2] node down: Not properly terminated\n'
    )
    _, events = _parse(log, 'g.log')
    (event,) = [e for e in events if e.kind == 'fatal-exception']
    assert event.test_id is None
    assert 'fem.py' in event.detail


def test_a_posix_fatal_dump_is_parsed_too():
    """THE DISCRIMINATING PLATFORM CASE. faulthandler's banner is platform-dependent, so a census
    that knows only the Windows spelling reports "no censored events" for every segfault on
    Linux/macOS. Same dump shape below the banner; only the opener differs.
    """
    log = (
        'Fatal Python error: Segmentation fault\n'
        'Current thread 0x1 (most recent call first):\n'
        '  File "/repo/src/pkg/fem.py", line 9 in assemble\n'
        '  File "/repo/tests/unit/test_x.py", line 5 in test_seg\n'
        '[gw1] node down: Not properly terminated\n'
    )
    _, events = _parse(log, 'p.log')
    (event,) = events  # the node-down folds into the dump: one death, one event
    assert event.kind == 'fatal-exception'
    assert event.test_id == 'tests/unit/test_x.py::test_seg'
    assert 'Segmentation fault' in event.detail


def test_a_bare_node_down_is_its_own_event():
    """The converse of the merge rule: with no dump to explain it, the node-down IS the only record
    that a worker died, and folding it into nothing would lose the death entirely.
    """
    _, events = _parse('[gw3] node down: Not properly terminated\n', 'n.log')
    (event,) = [e for e in events if e.kind == 'node-down']
    assert event.test_id is None
    assert 'Not properly terminated' in event.detail


def test_a_node_down_long_after_a_timeout_is_a_second_death():
    """The merge window has to have an edge, and this is it: far enough from the dump, it is a
    different worker dying, not the same one being reported twice.
    """
    log = _LOG_TIMEOUT + ('.\n' * (testlog.MERGE_WINDOW_LINES + 5)) + '[gw4] node down: Not properly terminated\n'
    _, events = _parse(log, 'm.log')
    assert sorted(e.kind for e in events) == ['node-down', 'timeout']


def test_the_censored_section_says_so_when_empty(tmp_path: Path):
    """'Looked and found none' must not render identically to 'did not look'."""
    text = testlog.render(_load([_write(tmp_path, 'a.log', _LOG_A)]))
    assert 'CENSORED TESTS' in text
    assert 'none found' in text


def test_logs_with_no_duration_rows_are_named_as_whole_run_censorship(tmp_path: Path):
    """A killed run leaves no durations table AT ALL, so it contributes nothing and looks like a
    boring log. Naming those logs is the run-level half of the same correction.
    """
    corpus = _load([_write(tmp_path, 'a.log', _LOG_A), _write(tmp_path, 't.log', _LOG_TIMEOUT)])
    assert corpus.zero_duration_logs == ('t.log',)
    assert 't.log' in testlog.render(corpus)


# --- what the CALLER supplies ---------------------------------------------------------------


def test_run_markers_are_REQUIRED_and_have_no_default():
    """THE COUPLING THIS SPLIT REMOVED. The original held one project's two gate markers as module
    constants. A default here would mean every consumer silently inherits another project's runner
    vocabulary -- the `DEFAULT_REPO` shape, which is invisible precisely because it works.
    """
    with pytest.raises(TypeError):
        testlog.parse_log('x\n', 'a.log')  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        testlog.load_corpus([])  # type: ignore[call-arg]


def test_a_declared_run_marker_becomes_an_unattributed_event():
    """A run-level kill names no test by construction -- the harness dies, the in-flight test is
    whatever it was. It must still be counted, or a killed run reads as a clean one.
    """
    log = '[myrunner:budget] EXCEEDED: killed after 30.1 min.\n'
    _, events = testlog.parse_log(log, 'k.log', run_markers={'budget-kill': '[myrunner:budget] EXCEEDED'})
    (event,) = events
    assert event.kind == 'budget-kill'
    assert event.test_id is None


def test_a_run_marker_printed_twice_is_ONE_event():
    """A harness typically prints its kill marker at the kill and again in its summary. One kill is
    one event, or every budget kill is counted double.
    """
    log = '[myrunner:budget] EXCEEDED: killed.\n[verdict] INCONCLUSIVE -- [myrunner:budget] EXCEEDED.\n'
    _, events = testlog.parse_log(log, 'k.log', run_markers={'budget-kill': '[myrunner:budget] EXCEEDED'})
    assert [e.kind for e in events] == ['budget-kill']


def test_an_empty_marker_mapping_finds_no_run_level_kills():
    """The control, and the reason `{}` is a legal argument rather than an oversight: a consumer
    whose runner prints no such markers says so, and gets no invented events.
    """
    log = '[myrunner:budget] EXCEEDED: killed.\n'
    _, events = testlog.parse_log(log, 'k.log', run_markers={})
    assert events == []


def test_the_TESTS_DIRECTORY_is_a_parameter(tmp_path: Path):
    """A stack frame is attributed to a test by the directory it sits under, and which directory
    that is is a LAYOUT decision. Pinned so the default cannot quietly become a requirement.
    """
    block = (
        '+++ Timeout +++\n'
        '~~~~~ Stack of MainThread (1) ~~~~~\n'
        '  File "D:\\repo\\spec\\unit\\test_x.py", line 9, in test_elsewhere\n'
        '+++ Timeout +++\n'
    )
    _, default_events = _parse(block, 'd.log')
    assert default_events[0].test_id is None, 'a `spec/` frame is not under the default `tests/`'

    _, events = _parse(block, 'd.log', tests_dir='spec')
    assert events[0].test_id == 'spec/unit/test_x.py::test_elsewhere'
