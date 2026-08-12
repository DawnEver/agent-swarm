"""What a test run COST, across runs -- the survivors AND the population no duration table can see.

PROVENANCE. Extracted 2026-08-12 from motronics' `scripts/gate/test_cost_report.py`, which measured
285 own code lines naming that project ZERO times: a mechanism that had been classified as one
project's because it lived in its `scripts/` directory. Nothing in it is about motors. Every
regular expression below matches output that `pytest`, `pytest-timeout`, `pytest-xdist` or CPython's
own `faulthandler` produce for any repository at all.

THE CENSORED POPULATION IS THE WHOLE POINT, and it is why this is not `grep`. A test that is KILLED
leaves NO ``--durations`` entry. Every durations table therefore describes the slowest SURVIVORS,
not the slowest tests -- measured 2026-07-29, a legitimately long test converging normally was
killed at the per-test ceiling while invisible to every table ever collected. Reading such a table
as "the slow tests" is the instrument-that-lies shape: it is not wrong about what it shows, it is
silent about what it cannot.

So the parse also recovers, from the same logs:

* pytest-timeout's faulthandler ``+++ Timeout +++`` blocks -- the killed test from the first
  ``tests/`` frame of the MainThread stack, when one exists;
* native fatal dumps -- ``Windows fatal exception:`` and POSIX's ``Fatal Python error:`` -- the
  crash site and, when visible, the test (see NAMED BOUNDARY);
* xdist's ``worker 'gwN' crashed while running '<nodeid>'`` -- the exact nodeid;
* xdist's ``[gwN] node down: ...`` -- counted only when no Timeout block or fatal dump nearby
  explains it. It prints right AFTER the dump that explains the death, and one death is one event;
* RUN-LEVEL kills the CALLER declares (:func:`parse_log`'s ``run_markers``). A wall-clock budget or
  a deadlock detector is a property of whoever drives the suite, not of pytest, so this module
  holds no list of them.

A report that only shows survivors is exactly the defect above, so the censored section is rendered
even when it is EMPTY: "looked and found none" must not render identically to "did not look".

NAMED BOUNDARY -- the merge rule, stated because getting it wrong double-counts. A native crash
(`Windows fatal exception: access violation`, e.g. a COM fault or a BLAS access violation) is NOT a
Timeout block: no tilde banners, comma-less innermost-first frames, and the crashing `Current
thread` section LAST. The test is recovered from that section's outermost ``tests/`` frame when one
is visible; measured 2026-07-30, a worker that died in numpy fancy indexing showed none, so the
event carries the crash SITE instead. `[gwN] node down` names no test and prints right after the
dump explaining it, so one within `MERGE_WINDOW_LINES` of a Timeout block or a fatal dump is folded
into that event. A death visible in NEITHER shape -- a killed process leaving no dump at all --
remains invisible, and no measurement in the original corpus showed one.

ONLY ``call`` PHASES ENTER THE HEADLINE STATS. ``call`` is the test body; a slow ``setup`` is a
fixture's cost attributed to every test that shares it, which would blame the wrong file.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

__all__ = [
    'KINDS',
    'MERGE_WINDOW_LINES',
    'CensoredEvent',
    'Corpus',
    'DurationRow',
    'Stats',
    'aggregate',
    'dir_of',
    'file_of',
    'load_corpus',
    'parse_log',
    'render',
]

#: Logs carry ANSI colour escapes around progress markers and banners; a row that fails to parse
#: only because of terminal paint is a silent hole in the population.
_ANSI_RE = re.compile(r'\x1b\[[0-9;]*m')

#: A `--durations` row: `244.07s call     tests/unit/...::test_x[param]`. `re.MULTILINE` because it
#: is applied per log TEXT rather than per line.
DURATION_RE = re.compile(r'^[ \t]*(\d+\.\d+)s[ \t]+(call|setup|teardown)[ \t]+(\S+)', re.MULTILINE)

#: pytest-timeout's faulthandler banner. The opener is glued to the progress marker of the test it
#: killed (`ssF+++ Timeout +++`, measured), so the line may carry leading non-space runes.
_TIMEOUT_BANNER_RE = re.compile(r'^\S*\s*\+{3,} Timeout \+{3,}\s*$')

#: `~~~~~~~ Stack of MainThread (27216) ~~~~~~~`. Other threads are dumped too; only MainThread runs
#: the test.
_MAIN_THREAD_RE = re.compile(r'^~+ Stack of MainThread\b.*~+\s*$')
_ANY_THREAD_RE = re.compile(r'^~+ Stack of .+~+\s*$')

#: A faulthandler frame: `  File "D:\repo\tests\test_x.py", line 88, in test_y`.
_FRAME_RE = re.compile(r'^\s*File "([^"]+)", line \d+, in (\S+)\s*$')

#: A NATIVE crash. TWO SPELLINGS, because faulthandler's banner is platform-dependent and a census
#: that knows only one reports "no censored events" on the other. Windows: `Windows fatal
#: exception: access violation`. POSIX (and any CPython fatal path): `Fatal Python error:
#: Segmentation fault`. Both are followed by the same comma-less, innermost-first thread dump, so
#: only the opener differs.
_FATAL_RE = re.compile(r'^\S*\s*(?:Windows fatal exception|Fatal Python error): (.+?)\s*$')
_FATAL_THREAD_RE = re.compile(r'^(Current thread|Thread) 0x[0-9a-fA-F]+ \(')
_FATAL_FRAME_RE = re.compile(r'^\s*File "([^"]+)", line \d+ in (\S+)\s*$')

#: xdist's OTHER death message: `[gw5] node down: Not properly terminated`. It names no test.
_NODE_DOWN_RE = re.compile(r'^\[gw\d+\] node down: (.+?)\s*$')
MERGE_WINDOW_LINES = 200

#: pytest-xdist names the test a crashed worker was running -- the one censored signal carrying an
#: exact nodeid, parametrization included.
_WORKER_CRASH_RE = re.compile(r"worker '\S+' crashed while running '(\S+)'")

#: The kinds this module can find on its own, in report order. A caller's ``run_markers`` kinds are
#: appended after these, in the caller's own order -- see :func:`render`.
KINDS = ('timeout', 'fatal-exception', 'node-down', 'worker-crash')

#: The path segment a test file lives under. A PARAMETER of :func:`parse_log` rather than a constant,
#: because "which directory holds the tests" is a layout decision; but it has a value here because
#: every consumer so far spells it the same and a literal in one place beats one at each call.
DEFAULT_TESTS_DIR = 'tests'


@dataclass(frozen=True)
class DurationRow:
    """One parsed ``--durations`` entry."""

    seconds: float
    phase: str  # 'call' | 'setup' | 'teardown'
    test_id: str  # full nodeid, parametrization included
    log: str  # log file name the row was read from


@dataclass(frozen=True)
class CensoredEvent:
    """One kill the durations table cannot see.

    ``test_id`` is ``None`` when the marker does not name the test (run-level kills, and timeout
    blocks whose stack shows no test frame). Stack-derived ids name file and function only: a stack
    frame carries no class qualifier and no parametrization.
    """

    kind: str
    test_id: str | None
    log: str
    detail: str


@dataclass(frozen=True)
class Stats:
    """The cost of one key across runs. ``spread`` is max/min -- 1.0 for a single run, ``inf`` if
    the cheapest run measured 0.
    """

    runs: int
    total_s: float
    mean_s: float
    min_s: float
    max_s: float
    spread: float


@dataclass(frozen=True)
class Corpus:
    """Everything read from the logs handed to :func:`load_corpus`."""

    logs: tuple[str, ...]
    rows: tuple[DurationRow, ...]
    events: tuple[CensoredEvent, ...]
    zero_duration_logs: tuple[str, ...]


def _stats(values: list[float]) -> Stats:
    total = sum(values)
    low, high = min(values), max(values)
    return Stats(
        runs=len(values),
        total_s=total,
        mean_s=total / len(values),
        min_s=low,
        max_s=high,
        spread=high / low if low > 0 else math.inf,
    )


def file_of(test_id: str) -> str:
    """The path part of a nodeid: ``tests/unit/x.py::Cls::test_y[p]`` -> ``tests/unit/x.py``."""
    return test_id.split('::', 1)[0]


def dir_of(test_id: str) -> str:
    return str(PurePosixPath(file_of(test_id)).parent)


def _node_key(test_id: str) -> tuple[str, str]:
    """``(file, function)`` -- the join key between a stack-derived censored id (no class, no
    params) and a durations nodeid (both present when applicable).
    """
    return file_of(test_id), test_id.rsplit('::', maxsplit=1)[-1].split('[', 1)[0]


def _rel_test_path(path: str, tests_dir: str) -> str | None:
    r"""``D:\repo\tests\unit\test_x.py`` -> ``tests/unit/test_x.py``; ``None`` if the frame is not
    under ``tests_dir`` (site-packages, frozen modules, the interpreter's own stack).
    """
    parts = path.replace('\\', '/').split('/')
    for i, part in enumerate(parts):
        if part == tests_dir:
            return '/'.join(parts[i:])
    return None


def _timeout_event(frames: list[tuple[str, str]], log: str, tests_dir: str) -> CensoredEvent:
    """Name the killed test from the OUTERMOST test frame of the MainThread stack.

    Walking DOWN from pytest's entry, that frame is the test function itself; the frames below it
    are whatever it never returned from.
    """
    for path, func in frames:
        rel = _rel_test_path(path, tests_dir)
        if rel is not None:
            return CensoredEvent('timeout', f'{rel}::{func}', log, f'stack top: {path}:{func}')
    return CensoredEvent('timeout', None, log, 'timeout block whose MainThread stack shows no test frame')


def _death_events(lines: list[str], log: str, tests_dir: str) -> list[CensoredEvent]:
    """One pass over the death markers: Timeout blocks, native fatal dumps, node-downs."""
    events: list[CensoredEvent] = []
    last_explained_idx = -MERGE_WINDOW_LINES * 2
    in_block = in_main = False
    frames: list[tuple[str, str]] = []
    in_fatal = in_current = False
    fatal_reason = ''
    fatal_frames: list[tuple[str, str]] = []

    def close_fatal(idx: int) -> None:
        nonlocal in_fatal, in_current, fatal_frames, last_explained_idx
        # The dump is innermost-FIRST: frame[0] is the crash site, and walking the section in
        # REVERSE (outermost-in) gives the first test frame the same meaning the timeout path's
        # walking-down has -- the test function itself.
        test_id = next(
            (
                f'{rel}::{func}'
                for path, func in reversed(fatal_frames)
                if (rel := _rel_test_path(path, tests_dir)) is not None
            ),
            None,
        )
        where = f'crash site: {fatal_frames[0][0]}:{fatal_frames[0][1]}' if fatal_frames else 'no frames captured'
        events.append(CensoredEvent('fatal-exception', test_id, log, f'{fatal_reason} ({where})'))
        fatal_frames = []
        in_fatal = in_current = False
        last_explained_idx = idx

    for idx, line in enumerate(lines):
        if (found := _FATAL_RE.match(line)) and not in_block:
            if in_fatal:  # a second dump before any node-down: close the first, they are two deaths
                close_fatal(idx)
            in_fatal = True
            fatal_reason = found.group(1)
            continue
        if in_fatal:
            if (thread := _FATAL_THREAD_RE.match(line)) is not None:
                in_current = thread.group(1) == 'Current thread'
                continue
            if in_current and (frame := _FATAL_FRAME_RE.match(line)):
                fatal_frames.append((frame.group(1), frame.group(2)))
                continue
        if (down := _NODE_DOWN_RE.match(line)) is not None:
            if in_fatal:  # the dump this node-down explains
                close_fatal(idx)
            elif in_block or idx - last_explained_idx <= MERGE_WINDOW_LINES:
                pass  # an open Timeout block or a recent close already accounts for it
            else:
                events.append(CensoredEvent('node-down', None, log, down.group(1)))
                last_explained_idx = idx
            continue
        if _TIMEOUT_BANNER_RE.match(line):
            if in_block:  # closing banner: emit what the block's stack showed
                events.append(_timeout_event(frames, log, tests_dir))
                frames = []
                in_main = False
                last_explained_idx = idx
            in_block = not in_block
            continue
        if not in_block:
            continue
        if _MAIN_THREAD_RE.match(line):
            in_main = True
            continue
        if _ANY_THREAD_RE.match(line):
            in_main = False
            continue
        if in_main and (frame := _FRAME_RE.match(line)):
            frames.append((frame.group(1), frame.group(2)))
    if in_block:  # a log cut mid-block is still a kill -- the censorship, not less of it
        events.append(_timeout_event(frames, log, tests_dir))
    if in_fatal:  # a log cut mid-dump: the crash is certain even if xdist never reported it
        close_fatal(len(lines))
    return events


def parse_log(
    text: str,
    log: str,
    *,
    run_markers: Mapping[str, str],
    tests_dir: str = DEFAULT_TESTS_DIR,
) -> tuple[list[DurationRow], list[CensoredEvent]]:
    """Parse one log into its duration rows and its censored events.

    ``run_markers`` IS REQUIRED AND HAS NO DEFAULT: ``{kind: marker text}`` for the run-level kills
    the CALLER's harness prints -- a wall-clock budget, a deadlock detector, an out-of-memory
    guard. Those are properties of whoever drives the suite, and a default list here would be this
    package quietly knowing about one project's runner. Pass ``{}`` to look for none, which is a
    decision the empty mapping states.

    DEDUPED PER LOG, because a harness typically prints a run-level marker twice -- once at the kill
    and again in its summary -- and one kill is one event. Worker crashes are deduped per
    ``(log, test)``: xdist restarts re-report the same test up to ``--max-worker-restart`` times,
    and repeats of one kill inflate the census.
    """
    lines = [_ANSI_RE.sub('', line) for line in text.splitlines()]
    rows = [DurationRow(float(m.group(1)), m.group(2), m.group(3), log) for m in DURATION_RE.finditer('\n'.join(lines))]
    events = _death_events(lines, log, tests_dir)
    seen: set[tuple[str, str | None]] = {(e.kind, e.test_id) for e in events}
    for line in lines:
        candidates: list[tuple[str, str | None, str]] = []
        if (crash := _WORKER_CRASH_RE.search(line)) is not None:
            candidates.append(('worker-crash', crash.group(1), line.strip()))
        candidates.extend((kind, None, line.strip()) for kind, marker in run_markers.items() if marker in line)
        for kind, test_id, detail in candidates:
            if (kind, test_id) not in seen:
                seen.add((kind, test_id))
                events.append(CensoredEvent(kind, test_id, log, detail))
    return rows, events


def load_corpus(
    paths: Iterable[Path],
    *,
    run_markers: Mapping[str, str],
    tests_dir: str = DEFAULT_TESTS_DIR,
) -> Corpus:
    """Parse every path, recording which logs yielded NO duration rows.

    A log with zero rows is not a boring log: it is whole-run censorship, and it is reported
    separately for exactly the reason the module docstring gives.
    """
    rows: list[DurationRow] = []
    events: list[CensoredEvent] = []
    logs: list[str] = []
    zero: list[str] = []
    for path in paths:
        log_rows, log_events = parse_log(
            path.read_text(encoding='utf-8', errors='replace'),
            path.name,
            run_markers=run_markers,
            tests_dir=tests_dir,
        )
        logs.append(path.name)
        rows.extend(log_rows)
        events.extend(log_events)
        if not log_rows:
            zero.append(path.name)
    return Corpus(tuple(logs), tuple(rows), tuple(events), tuple(zero))


def aggregate(rows: Iterable[DurationRow], key: Callable[[DurationRow], str], phase: str = 'call') -> dict[str, Stats]:
    """Per-key stats over the ``phase`` rows. Default is ``call`` -- the test body; see the module
    docstring for why setup/teardown stay out of the headline.
    """
    values: dict[str, list[float]] = {}
    for row in rows:
        if row.phase == phase:
            values.setdefault(key(row), []).append(row.seconds)
    return {name: _stats(seconds) for name, seconds in values.items()}


def _ranked(items: dict[str, Stats]) -> list[tuple[str, Stats]]:
    return sorted(items.items(), key=lambda kv: (-kv[1].total_s, kv[0]))


def _table(title: str, items: list[tuple[str, Stats]], top: int) -> list[str]:
    lines = [
        f'{title} (of {len(items)} measured)',
        f'{"runs":>5} {"total_s":>10} {"mean_s":>10} {"min_s":>10} {"max_s":>10} {"spread":>7}  key',
    ]
    for key, stat in items[:top]:
        spread = f'{stat.spread:.2f}' if math.isfinite(stat.spread) else 'inf'
        lines.append(
            f'{stat.runs:>5} {stat.total_s:>10.2f} {stat.mean_s:>10.2f} {stat.min_s:>10.2f} '
            f'{stat.max_s:>10.2f} {spread:>7}  {key}'
        )
    if not items:
        lines.append('(none)')
    return lines


def _censored_lines(corpus: Corpus, order: Mapping[str, int]) -> list[str]:
    out = ['CENSORED POPULATION -- killed or timed out; the durations table never sees these']
    if not corpus.events:
        out.append('none found in the logs read (looked and found none, not "did not look")')
        return out
    survivors: dict[tuple[str, str], int] = {}
    for row in corpus.rows:
        if row.phase == 'call':
            key = _node_key(row.test_id)
            survivors[key] = survivors.get(key, 0) + 1
    groups: dict[tuple[str, str | None], list[CensoredEvent]] = {}
    for event in corpus.events:
        groups.setdefault((event.kind, event.test_id), []).append(event)
    for (kind, test_id), events in sorted(groups.items(), key=lambda kv: (order.get(kv[0][0], 99), kv[0][1] or '')):
        logs = sorted({event.log for event in events})
        if test_id is not None:
            count = survivors.get(_node_key(test_id), 0)
            out.append(
                f'{kind} x{len(events)}: {test_id} -- {count} surviving call row(s) '
                f'across the corpus [{", ".join(logs)}]'
            )
        else:
            out.append(f'{kind} x{len(events)}: (in-flight test not named by this marker) [{", ".join(logs)}]')
            out.append(f'    detail: {events[0].detail[:140]}')
    return out


def render(corpus: Corpus, *, top: int = 10, run_kinds: Iterable[str] = ()) -> str:
    """The whole report as text. ``run_kinds`` orders the caller's run-level kinds after this
    module's own, so a report never sorts an unknown kind to an arbitrary place.
    """
    order = {kind: i for i, kind in enumerate((*KINDS, *run_kinds))}
    call = [row for row in corpus.rows if row.phase == 'call']
    lines = [
        'test cost report',
        (
            f'logs read: {len(corpus.logs)} ({len(corpus.logs) - len(corpus.zero_duration_logs)} with '
            f'duration rows, {len(corpus.zero_duration_logs)} with none)'
        ),
        (
            f'duration rows: {len(corpus.rows)} ({len(call)} call-phase); '
            f'total call seconds accounted: {sum(row.seconds for row in call):.2f}'
        ),
    ]
    if corpus.zero_duration_logs:
        shown = corpus.zero_duration_logs[:8]
        more = f' (+{len(corpus.zero_duration_logs) - len(shown)} more)' if len(corpus.zero_duration_logs) > 8 else ''
        lines.append(f'logs with NO duration rows (whole-run censorship): {", ".join(shown)}{more}')
    lines.append('')
    lines += _table(f'TOP {top} TESTS BY TOTAL CALL SECONDS', _ranked(aggregate(corpus.rows, lambda r: r.test_id)), top)
    lines.append('')
    lines += _table(
        f'TOP {top} FILES BY TOTAL CALL SECONDS', _ranked(aggregate(corpus.rows, lambda r: file_of(r.test_id))), top
    )
    lines.append('')
    lines += _table(
        f'TOP {top} DIRECTORIES BY TOTAL CALL SECONDS',
        _ranked(aggregate(corpus.rows, lambda r: dir_of(r.test_id))),
        top,
    )
    lines.append('')
    lines += _censored_lines(corpus, order)
    return '\n'.join(lines)
