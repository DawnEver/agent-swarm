"""The production entry point: assemble a fleet from a config file and take ONE turn.

    python -m agent_swarm.fleet_cli --config fleet.toml

WHY THIS FILE IS THE ONE THAT MAKES THE REST REACHABLE
======================================================

Measured 2026-08-11 and confirmed 2026-08-12: `FabricSessionRunner`, `AgentTaskExecutor`, `run_one`
and `Fleet` were each built, each tested, and **constructed only in tests, in both repositories.**
The collaboration half of this system was complete and unreachable -- not missing a feature, missing
a door. `tick` is the driver and `clock` is what repeats; neither assembles a fleet, because
assembly is the OPERATOR's and had no home.

**ONE TICK, THEN EXIT.** No loop, no sleep, no retry schedule. Those belong to `clock`, which spawns
a fresh process per pass so that a tick which dies takes nothing with it -- and putting any of them
here would be the second scheduler this design refuses, in the file least likely to be read as one.
If you want it repeated, repeat it from outside.

AND WHAT REPEATS IT IS A HUMAN, IN A TERMINAL. ALWAYS -- THAT IS THE DESIGN, NOT A GAP
======================================================================================

USER DIRECTIVE, restated 2026-08-12: every loop in this system is started by a person at a keyboard
and runs in a window they can see and close. **7x24 DOES NOT MEAN UNATTENDED** -- it means the loop
is long-lived and VISIBLE, and uptime comes from it running continuously, never from nobody having
started it. `lifetime.bind_children_to_this_process()` makes that structural rather than customary:
a Windows Job Object with `KILL_ON_JOB_CLOSE`, SIGHUP to the process group on POSIX, so **closing
the window stops the service by construction**. `ci_loop` REFUSES TO START unbound and prints which
mechanism bound it.

SO DO NOT "IMPROVE" THIS FILE INTO A SERVICE. No `--daemon`, no `--detach`, no `--background`, no
sleep-and-repeat, no scheduled-task installer -- not even behind a flag, because a flag is a second
entry point and this project has deleted one for exactly that reason. **A one-shot CANNOT become an
unattended runner, and that impossibility is the feature.** Anything long-running added here would
have to bind first and REFUSE TO START if it could not; the honest move is to add nothing, because
the loop is `clock`'s and `clock` already exists.

WHY, so this is defensible rather than merely obeyed: an unattended runner is INVISIBLE. It survives
reboots, nobody remembers starting it, and the measured failure on this project was a scheduled task
whose `LastRunTime` read **1932** while a source-tree guard reported the runner present and correct.
A guard going green is not evidence the design was respected. Terminal-bound means "is it running?"
is answered by "is there a window?", which a person can see. `test_the_fleet_has_a_door.py` carries
a guard over this file's own source, because documentation is not a control.

NOTHING HERE DECIDES ANYTHING, which is the same promise `tick` makes and for the same reason. This
file reads TOML, constructs objects, and calls `tick` once. Ordering is `allocator.rank`, admission
is `Box.blockers`, the verdict is the executor's. A default in this file is a decision, which is why
there are so few and why the ones that exist are argued below.

WHAT IT REFUSES TO GUESS
========================

`repo`, `namespace` and `owner` have NO defaults. `Fleet`'s own docstring says it is "assembled by
the operator, never guessed here", and each of these is a way to be confidently wrong about someone
else: a defaulted `repo` writes work items into a stranger's tracker, a defaulted `namespace` makes
two unrelated fleets contend for one claim space, and a defaulted `owner` answers a job as somebody
else -- which the claim protocol has no way to detect, since owning a claim IS being that name.

`available_gib` has no default EITHER, and that one is a judgement call worth stating. `Box`
documents that `None` REFUSES capacity-limited work rather than allowing it, so a default of `None`
would produce a fleet that starts cleanly, reports no error, and never runs anything -- "a fleet that
looks busy and completes nothing", which is the exact failure `_executor_for` raises to prevent one
line away. Reading free memory here is not an option: this package is stdlib-only by construction.
So the operator states it, and a box that genuinely cannot measure passes `0` and takes only work
that declares no appetite.

WHAT IT DOES NOT WIRE, and this is a REPORTED GAP rather than an oversight
=========================================================================

Only `AGENT_TASK` gets an executor, because `AgentTaskExecutor` is the only `Executor` this package
implements. A roadmap yielding a `TEST_RUN` will therefore fail at the FIRST tick with
`_executor_for`'s `KeyError` -- loudly, naming the kind and the claim key. That is the designed
behaviour for an unconfigured kind and it is deliberately not smoothed over here: the deterministic
gate runner is the consuming project's (it reads that project's log format), so inventing one in a
vendor-neutral package would be the layering mistake this repo has removed three times.
"""

from __future__ import annotations

import argparse
import sys
import tomllib
from pathlib import Path
from typing import Any

from agent_swarm.adapters import CommandVerifier, TreeWorkspace
from agent_swarm.agent_executor import AgentTaskExecutor, StaticBrief
from agent_swarm.fabric import FabricSessionRunner
from agent_swarm.forge_store import ForgeStore, Role
from agent_swarm.item_index import ItemIndex
from agent_swarm.job import AGENT_TASK
from agent_swarm.loop import Box
from agent_swarm.roadmap import loads as load_roadmap
from agent_swarm.spool import ForgePublisher, Spool
from agent_swarm.status import StatusPublisher
from agent_swarm.tick import Fleet, TickReport, submit, tick

#: Keys that must be present and have no sane default. See the module docstring for why each one is
#: a way to be confidently wrong about somebody else rather than merely an inconvenience.
REQUIRED = ('repo', 'namespace', 'owner', 'roadmap', 'available_gib')


class ConfigError(RuntimeError):
    """The config cannot produce a fleet. Always names the key."""


def load_config(path: Path) -> dict[str, Any]:
    """Read the TOML and refuse anything that cannot make a fleet, BEFORE touching the network.

    EVERY MISSING KEY AT ONCE, not the first. An operator fixing a config one error per run against a
    remote forge pays a round trip for each, and the second error was knowable at the same instant as
    the first.
    """
    try:
        raw = path.read_bytes()
    except OSError as exc:
        msg = f'cannot read {path}: {exc}'
        raise ConfigError(msg) from None
    try:
        config = tomllib.loads(raw.decode('utf-8'))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        msg = f'{path} is not valid TOML: {exc}'
        raise ConfigError(msg) from None
    missing = [key for key in REQUIRED if config.get(key) in (None, '')]
    if missing:
        msg = f'{path} is missing required key(s): {", ".join(missing)}. None of these may be defaulted -- see the module docstring.'
        raise ConfigError(msg)
    return config


def build_fleet(config: dict[str, Any], *, base_dir: Path) -> Fleet:
    """Turn config into a `Fleet`. Constructs, never decides.

    THE TWO STORES SHARE ONE `ItemIndex` ON PURPOSE -- that is what `test_end_to_end` demonstrates
    and what makes the second store's lookups warm. They do NOT share a role: `Fleet.__post_init__`
    refuses a fleet whose submitter and runner are interchangeable, because handing one store to both
    is the configuration that produced eight duplicate items per round.
    """
    from agent_swarm.forge import default_forge  # noqa: PLC0415 -- the only door to the network

    repo = config['repo']
    namespace = config['namespace']
    state = _resolve(base_dir, config.get('state_dir', '.swarm'))
    index = ItemIndex(state / 'index.json')

    def store(role: Role, account: str = 'agent') -> ForgeStore:
        return ForgeStore(namespace, default_forge(account, repo=repo), role=role, index=index)

    session_cfg = dict(config.get('session') or {})
    project = session_cfg.pop('project', None)
    session = FabricSessionRunner(project=project, **session_cfg)

    # `workspace=None` WHENEVER THE SESSION IS REMOTE, and `AgentTaskExecutor.__init__` refuses the
    # other combination outright rather than letting it degrade. This is not a workaround for that
    # guard, it is the guard's instruction: a local fingerprint beside a remote session answers
    # "changed nothing" about a tree the session never touched -- confidently, and in the direction
    # that looks safe. `FabricSessionRunner.executes_remotely()` is unconditionally True today, so
    # this is always None for a fabric fleet; see `report_known_limits`.
    workspace = None if session.executes_remotely() or project is None else TreeWorkspace(Path(project))

    verifier_cfg = config.get('verifier') or {}
    if not verifier_cfg.get('argv'):
        msg = "config needs [verifier] argv = [...] -- the definition of done is the operator's, never this package's"
        raise ConfigError(msg)

    executor = AgentTaskExecutor(
        session=session,
        verifier=CommandVerifier(
            verifier_cfg['argv'],
            timeout_s=float(verifier_cfg.get('timeout_s', 1800.0)),
            cwd=_resolve(base_dir, verifier_cfg['cwd']) if verifier_cfg.get('cwd') else None,
        ),
        workspace=workspace,
        brief=StaticBrief(**(config.get('brief') or {})),
    )

    return Fleet(
        roadmap=load_roadmap(_resolve(base_dir, config['roadmap']).read_text(encoding='utf-8')),
        submitter=store(Role.SUBMITTER),
        runner=store(Role.RUNNER),
        spool=Spool(state / 'spool'),
        publisher=ForgePublisher(store(Role.SUBMITTER)),
        executors={AGENT_TASK: executor},
        owner=config['owner'],
        status=_status_publisher(config, repo),
    )


def _status_publisher(config: dict[str, Any], repo: str) -> StatusPublisher | None:
    """A commit status publisher, or `None` -- and `None` IS A LEGITIMATE FLEET MEMBER.

    Only a box holding the verifier credential may mark a commit, so most boxes have none and must
    still be able to take a turn. Building one unconditionally would make every box that cannot
    publish fail at construction, which is the opposite of what `Fleet.status`'s optionality is for.
    """
    status_cfg = config.get('status') or {}
    if not status_cfg:
        return None
    from agent_swarm.forge import default_forge  # noqa: PLC0415 -- as above

    missing = [key for key in ('context', 'runner') if not status_cfg.get(key)]
    if missing:
        msg = f'[status] needs {", ".join(missing)}; omit the whole section if this box does not publish'
        raise ConfigError(msg)
    return StatusPublisher(
        default_forge('verifier', repo=repo), context=status_cfg['context'], runner=status_cfg['runner']
    )


def _resolve(base_dir: Path, value: str) -> Path:
    """Paths in the config are relative TO THE CONFIG FILE, not to the caller's cwd.

    A config that means something different depending on where it was invoked from is the same class
    of defect as a CWD-relative test scan: it works for the person who wrote it and silently reads
    another tree for everyone else.
    """
    path = Path(value)
    return path if path.is_absolute() else base_dir / path


def report_known_limits(fleet: Fleet, box: Box | None = None) -> list[str]:
    """What this fleet CANNOT do, printed every run rather than discovered from an empty backlog.

    A limit an operator meets as a puzzling result is worse than one they read at startup. Both of
    these are real today, neither is a defect in this file, and both stop being true when the work
    they name lands -- at which point this function's list shortens on its own.
    """
    limits = []
    executor = fleet.executors.get(AGENT_TASK)
    if isinstance(executor, AgentTaskExecutor) and executor.workspace is None:
        limits.append(
            'agent tasks cannot reach PASS on this fleet: the session runs on another node, so '
            'nothing here can say whether it changed anything, and the executor refuses to judge on '
            'evidence it does not have. Every agent task will answer INCONCLUSIVE until the '
            'transport reports SessionOutcome.changed itself.'
        )
    unconfigured = sorted({item.job.kind.value for item in fleet.roadmap.items} - {k.value for k in fleet.executors})
    if unconfigured:
        limits.append(
            f'the roadmap schedules {", ".join(unconfigured)} and this fleet has no executor for '
            f'it -- the first tick that reaches one will FAIL, by design, naming the claim key.'
        )
    if box is not None and fleet.roadmap.items:
        # THE QUIETEST WAY TO SHIP A DEAD BOX, and it was found by writing this file rather than by
        # reading it: an `expensive` job with no `ram_gib` is priced at an ASSUMED 12.5 GiB plus a
        # 2 GiB reserve, so any `available_gib` under ~14.5 blocks every job on the roadmap. The tick
        # then completes cleanly, considers nothing, reports idle and exits 0 -- indistinguishable
        # from a healthy box with an empty backlog, forever. `capacity_blocker` is right to refuse;
        # what was missing is anybody SAYING so at the point the number is chosen.
        blocked = [
            f'{item.key}: {"; ".join(box.blockers(item.job))}' for item in fleet.roadmap.items if box.blockers(item.job)
        ]
        if len(blocked) == len(fleet.roadmap.items):
            limits.append(
                f'NOTHING on this roadmap fits this box, so every tick will report idle while work '
                f'is waiting. available_gib={box.available_gib}. First blocker -- {blocked[0]}'
            )
    return limits


def render(report: TickReport) -> str:
    """One pass, in the order it happened. Empty sections are shown rather than omitted, so 'idle'
    is distinguishable from 'this part did not run'."""
    lines = [
        f'submitted:      {report.submitted or "-"}',
        f'considered:     {report.considered or "-"}',
        f'outcomes:       {{{", ".join(f"{k}: {v.value}" for k, v in report.outcomes.items()) or "-"}}}',
        f'published:      {report.published or "-"}',
        f'drain failures: {report.drain_failures or "-"}',
    ]
    return '\n'.join(lines)


def main(argv: list[str] | None = None) -> int:
    """One tick. Exit 0 for a completed pass INCLUDING an idle one; 2 for a configuration error.

    **IDLE IS NOT A FAILURE.** A box with nothing to do is the normal state of most of a fleet most
    of the time, and a non-zero exit for it would make `clock` -- or any supervisor -- read a healthy
    quiet box as broken and thrash on it. The distinction that matters is "could not be configured"
    versus "ran and found nothing", and only the first is an error.

    A CONFIGURATION ERROR IS NOT A TRACEBACK. `ConfigError` names the key; letting it propagate would
    hand an operator a stack through `tomllib` for a missing line in their own file.
    """
    parser = argparse.ArgumentParser(
        prog='python -m agent_swarm.fleet_cli',
        description="Assemble a fleet from a config file and take ONE turn. Repeating is `clock`'s job.",
    )
    parser.add_argument('--config', type=Path, required=True, help='the TOML describing this box')
    parser.add_argument('--sha', default=None, help="the commit this box's checkout stands at, for the status")
    parser.add_argument('--retry', action='store_true', help='allow retrying jobs that already have a verdict')
    parser.add_argument('--submit', action='store_true', help='also ensure every roadmap item has a work item')
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)
        fleet = build_fleet(config, base_dir=args.config.resolve().parent)
    except ConfigError as exc:
        sys.stderr.write(f'configuration error: {exc}\n')
        return 2

    box = Box(available_gib=float(config['available_gib']))
    for limit in report_known_limits(fleet, box):
        sys.stderr.write(f'[limit] {limit}\n')

    report = TickReport()
    if args.submit:
        # THE SUBMITTER HALF IS OPT-IN, because it must run on exactly ONE box. `submit`'s own
        # docstring names concurrent submission as the residual that no re-read closes, so a flag
        # that every box in a fleet would set by default is the wrong default to offer.
        report.submitted = submit(fleet)

    result = tick(fleet, box, retry=args.retry, sha=args.sha)
    result.submitted = report.submitted
    sys.stdout.write(render(result) + '\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
