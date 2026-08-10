"""rem findings <-> `roadmap.toml` intent: read both, report drift, write NEITHER.

THE PROBLEM. The backlog had three homes -- `roadmap.toml`, rem's `manual.md`/`sharp-review.md`
findings under `.claude/memory/`, and prose notes -- with no relation between them. Three homes for
one fact class is this session's dominant defect at workflow scale: a finding could be worked and
closed while its roadmap twin still queued, and a roadmap item could carry no trace of the
observation that justified it, so nobody could tell admitted intent from a forgotten leftover.

ONE WRITER PER FACT, and each layer owns exactly one question:

===========================  =========================================  ======================
question                     home                                       writer
===========================  =========================================  ======================
was it NOTICED, still open?  rem findings in `.claude/memory/**`         rem's own harness
was it ADMITTED as intent?   `roadmap.toml`, the `rem =` field           the human, by hand
what is its WORK STATE?      the forge issue                            the swarm (contended)
what did the gate decide?    `refs/verdicts/` + the commit status       the verifier
===========================  =========================================  ======================

SO THIS MODULE WRITES NOTHING. Admission is a human act -- that is the entire reason `rem` is a
required roadmap field rather than something inferred -- and a bridge that promoted findings
automatically would turn "noticed" into "committed to" without anyone deciding. It reads both sides
and reports. `retire_command` goes one step further and prints the rem command rather than running
it, for the same reason in the other direction.

REUSING REM'S HARNESS RATHER THAN REIMPLEMENTING IT. The finding format (`MANUAL-<date>-<n>` /
`SR-<n>` lines, the checkbox, the severity, the module headings) is parsed by rem's `task-lib.mjs`,
and a second parser here would be a duplicated scheme -- the exact defect class this project names.
`scan` therefore SHELLS OUT to that library through a one-expression Node shim and reads JSON back.
The cost is a Node dependency on the scanning path only; the pure half (`check`, `unpromoted`) takes
a list and is reachable with no Node at all, which is what the tests exercise.

AND A MISSING HARNESS RAISES. `scan` never answers `[]` when it could not look: unknown is not zero,
and an empty list here would read as "no findings", quietly reporting a clean backlog on any box
where the plugin is not installed.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from agent_swarm.roadmap import HUMAN_PROVENANCE, Roadmap, loads

#: Where rem's scanning library lives, overridable because the plugin cache path carries a version.
REM_SCRIPTS_ENV = 'REM_SCRIPTS'

#: The shim, with `%s` for the library's file URL. It imports rem's OWN scanner and re-emits the
#: result as JSON, so no finding-format knowledge exists on this side at all.
_SHIM = (
    'import {scanManualTasks, scanMemoryForFindings} from %s;'
    'const d = process.argv[1];'
    'process.stdout.write(JSON.stringify([...scanManualTasks(d), ...scanMemoryForFindings(d)]));'
)

#: rem's word for a finding that is no longer open. Both of its scanners emit `status`, not the raw
#: checkbox, so this is the one place the vocabularies meet.
_CLOSED = 'fixed'


class BridgeError(RuntimeError):
    """The bridge could not READ one of the two sides. Never raised for drift, only for blindness."""


@dataclass(frozen=True, slots=True)
class RemTask:
    """One rem finding, as its harness reports it. `checked` is rem's own closed/open bit."""

    id: str
    summary: str
    checked: bool
    severity: str


def _rem_scripts_dir() -> Path:
    """rem's `scripts/` directory. RAISES with an instruction rather than degrading to no findings."""
    if override := os.environ.get(REM_SCRIPTS_ENV):
        found = Path(override)
        if not (found / 'task-lib.mjs').is_file():
            msg = f'{REM_SCRIPTS_ENV}={override} has no task-lib.mjs'
            raise BridgeError(msg)
        return found
    cache = Path.home() / '.claude' / 'plugins' / 'cache' / 'cc-market' / 'rem'
    # Newest version wins: the cache keeps old ones, and scanning with a stale parser is the silent
    # half of a format change. Sorted lexically, which is right for the zero-padded scheme in use.
    versions = sorted((p for p in cache.glob('*/scripts') if (p / 'task-lib.mjs').is_file()), reverse=True)
    if not versions:
        msg = (
            f"rem's task-lib.mjs was not found under {cache}. Install the rem plugin, or point "
            f'{REM_SCRIPTS_ENV} at its scripts/ directory. Refusing to report an empty backlog '
            'from a scan that never ran.'
        )
        raise BridgeError(msg)
    return versions[0]


def scan(root: Path, *, scripts: Path | None = None) -> list[RemTask]:
    """Every rem finding under ``root``'s `.claude/memory`, via rem's OWN parser.

    ``root`` is the repo whose memory is being read -- worktree-local, so pass the tree you mean.
    """
    scripts = scripts or _rem_scripts_dir()
    if not (scripts / 'task-lib.mjs').is_file():
        # CHECKED HERE TOO, not only in `_rem_scripts_dir`: an explicit `scripts=` bypasses that
        # path entirely, and the alternative is a Node module-resolution error arriving as
        # "rem scan failed" with the real cause four lines into a stderr tail.
        msg = f'no task-lib.mjs under {scripts}. Refusing to report an empty backlog from a scan that never ran.'
        raise BridgeError(msg)
    node = shutil.which('node')
    if not node:
        msg = "node is not on PATH, and rem's scanner is a Node library. Refusing to guess the backlog."
        raise BridgeError(msg)
    lib = json.dumps((scripts / 'task-lib.mjs').as_uri())
    memory = root / '.claude' / 'memory'
    try:
        out = subprocess.run(
            [node, '--input-type=module', '-e', _SHIM % lib, str(memory)],
            capture_output=True,
            text=True,
            # UTF-8 EXPLICITLY. `text=True` decodes with the locale codec, which on a zh-CN Windows
            # box is GBK -- and findings in this project are routinely written in Chinese. MEASURED
            # 2026-08-10: the first real scan died with `'gbk' codec can't decode byte 0xaa`, and it
            # died in subprocess's READER THREAD, so stdout came back None rather than raising
            # anywhere a caller could see. Node always writes UTF-8; the locale is never the answer.
            encoding='utf-8',
            check=True,
            timeout=120,
        ).stdout
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        stderr = getattr(exc, 'stderr', '') or ''
        msg = f'rem scan failed: {type(exc).__name__}: {stderr[:400]}'
        raise BridgeError(msg) from exc
    return [
        RemTask(
            id=raw['id'],
            summary=raw.get('summary', ''),
            checked=raw.get('status') == _CLOSED,
            severity=raw.get('severity', 'MEDIUM'),
        )
        for raw in json.loads(out)
    ]


def check(roadmap: Roadmap, tasks: list[RemTask]) -> list[str]:
    """The INCONSISTENCIES, one line each. Empty means the two sides agree.

    Two of them, and both are real defects rather than untidiness:

    * a `rem =` naming a finding NOBODY RECORDED -- the declaration-that-lies shape, a field that
      looks like evidence and can be followed to none;
    * a CLOSED finding still backing a roadmap item -- rem says the observation is settled while the
      swarm keeps spending on it, which on a 7x24 fleet is spend with no end condition.

    A finding that is open and NOT on the roadmap is deliberately not here: that is the normal state
    of a backlog, and mixing it in would drown the two above. See `unpromoted`.
    """
    by_id = {t.id: t for t in tasks}
    problems = []
    for item in roadmap.items:
        if item.rem == HUMAN_PROVENANCE:
            continue
        task = by_id.get(item.rem)
        if task is None:
            problems.append(
                f'{item.key}: rem = {item.rem!r} names no recorded finding. '
                f"Fix the id, or say rem = '{HUMAN_PROVENANCE}' if it was your own idea."
            )
        elif task.checked:
            problems.append(
                f'{item.key}: its finding {item.rem} is closed in rem, but the item is still on the roadmap'
            )
    return problems


def unpromoted(roadmap: Roadmap, tasks: list[RemTask]) -> list[str]:
    """Open findings that no roadmap item was admitted from. NOT a problem -- the backlog itself.

    Most findings never earn admission, and that is the point of admission being a human act. This
    exists so the un-admitted set is VISIBLE rather than implicit, which is the difference between
    a backlog and a pile.
    """
    admitted = {i.rem for i in roadmap.items}
    return [t.id for t in tasks if not t.checked and t.id not in admitted]


def retire_command(task_id: str) -> str:
    """The rem command that closes ``task_id``. PRINTED, never run -- see the module docstring.

    Closing a finding asserts the observation no longer holds, which is a claim about the world
    rather than about the queue. A PASS says the acceptance criterion was met; it does not say the
    thing that was noticed is gone, and letting the swarm conflate them would retire findings on
    the strength of whatever criterion someone happened to write.
    """
    return f'todo mark {task_id} fixed'


def main(argv: list[str] | None = None) -> int:
    """`python -m agent_swarm.rem_bridge <repo>` -- reconcile the two sides and report.

    EXIT 1 ON A PROBLEM, 0 ON A MERELY LARGE BACKLOG. The distinction is the whole design: an
    inconsistency between the two homes is a defect and should stop something, while un-admitted
    findings are the normal state of a project and must never fail a check -- a gate that reddens on
    having a backlog is a gate everyone learns to ignore.
    """
    parser = argparse.ArgumentParser(prog='rem-bridge', description=__doc__.splitlines()[0])
    parser.add_argument('repo', type=Path, help='the repo whose .claude/memory and roadmap.toml to read')
    parser.add_argument('--roadmap', type=Path, default=None, help='default: <repo>/roadmap.toml')
    parser.add_argument('--show-backlog', action='store_true', help='list the open findings nobody admitted')
    args = parser.parse_args(argv)

    roadmap_path = args.roadmap or args.repo / 'roadmap.toml'
    if not roadmap_path.is_file():
        sys.stderr.write(f'no roadmap at {roadmap_path}\n')
        return 2
    roadmap = loads(roadmap_path.read_text(encoding='utf-8'))
    tasks = scan(args.repo)

    problems = check(roadmap, tasks)
    open_ids = unpromoted(roadmap, tasks)
    sys.stdout.write(
        f'{len(roadmap.items)} roadmap items, {len(tasks)} rem findings, '
        f'{len(open_ids)} open and un-admitted, {len(problems)} inconsistencies\n'
    )
    for line in problems:
        sys.stdout.write(f'  PROBLEM  {line}\n')
    if args.show_backlog:
        for task_id in open_ids:
            sys.stdout.write(f'  backlog  {task_id}\n')
    return 1 if problems else 0


if __name__ == '__main__':
    raise SystemExit(main())
