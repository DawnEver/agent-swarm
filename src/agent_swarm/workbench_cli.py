"""The way IN to the pull surface. Without this, the third executor kind is unusable.

`list` / `take` / `report` existed as Python objects and no human could reach them, which for going
live is indistinguishable from not having built them. This is the screen.

WHY THIS LIVES IN THIS PACKAGE AND NOT IN THE CONSUMER -- a boundary call, so it is argued rather
than assumed. Every verb here is L1 vocabulary: a claim key, a lease, a heartbeat, one of gate.py's
three verdict words. A CLI in the consumer would have to re-derive the identity grammar, the verdict
namespace and the lease policy, which is the duplicated scheme this package spent a refactor
deleting. What is genuinely the CONSUMER's is *which command does the work* and *what this box can
do* -- so both arrive as arguments and neither is named here.

THE ONE THING THIS CLI REFUSES TO OFFER, AND IT IS THE VERB EVERYONE ASKS FOR FIRST
===================================================================================

**There is no detached `take`.** You cannot claim an item, get your prompt back, and wander off.
That verb is the parking defect wearing a friendly name: nothing would be beating the lease, so the
item is unavailable until it expires, and the fleet cannot tell a person who is working from a
person who went to lunch. `ci_tick.claim` shipped exactly that and a closed laptop lid parked jobs
for hours.

So **a claim is only ever held by a LIVE PROCESS**. `take` claims, beats, runs your command as a
child, reports the verdict, and releases -- one process, one lifetime, no window in which a claim
outlives the thing that took it. Close the terminal and you lose the ticket promptly, which is the
correct outcome and is enforced twice over: the beater dies with the process, and `lifetime` binds
the child so the work dies with it too.

WHY `lifetime` RATHER THAN A SECOND MECHANISM. It is this package's kernel-level answer to exactly
this problem for the CI runner -- a Windows job object with KILL_ON_JOB_CLOSE, a POSIX SIGHUP
handler that kills the group -- and the failure it fixes is the one this CLI would otherwise
recreate: a stopped wrapper leaving orphaned workers running for twenty minutes, measured, taking a
workstation from 8.6 GiB free to 2.06. **It solves the CHILD half and not the LEASE half**, which is
why both are here: `lifetime` guarantees the work stops, the beater stopping guarantees the claim is
released. Neither substitutes for the other, and saying so is cheaper than someone later deleting
one because "the other covers it".

`LifetimeUnavailable` is NOT fatal here, and that is a difference from the runner. The runner is a
service whose operator was promised "closing the terminal stops it"; this is one interactive
command, and refusing to run work because a job object could not be created would be a worse trade
for the person in front of it. It is REPORTED on stderr and the run continues -- the lease still
bounds the damage, which is the property the runner does not get to rely on.

EVERY FAILURE IS IN THE EXIT CODE, not only in the printing
===========================================================

A CLI whose failures live in prose is a CLI nothing can script. :class:`Exit` is the whole
vocabulary and every path returns one of them; `--json` prints the same facts as data. In
particular **an empty result and an unreachable forge are different exit codes**, because they are
different facts: one is "I looked and saw nothing", the other is "I could not look". Collapsing
them is `Claimable`'s ban defeated at the last possible moment, with a friendly face.

NOTE ON A NAME: there is no `ForgeUnreachable` in this package. `forge.ForgeError` is what an
unreachable forge raises, and `ForgeStore.claimable` documents that it propagates rather than
returning an empty result. This CLI maps it to :attr:`Exit.FORGE_UNREACHABLE`; the distinction the
name was reaching for is real and is preserved here, it just was not a type.

    python -m agent_swarm.workbench_cli list
    python -m agent_swarm.workbench_cli take test-run/abc -- pytest -q
    python -m agent_swarm.workbench_cli report test-run/abc --verdict PASS --detail 'ran by hand'

NO LAUNCHER SCRIPTS, unlike `swarmctl`. Those exist because swarmctl runs on the Gitea host, which
has no venv and nothing installed, so finding a usable Python is its problem. A pull executor is a
person at a workstation that already has this package importable; a second pair of fifty-line
interpreter-probing scripts would be duplication bought for nobody.
"""

from __future__ import annotations

import argparse
import contextlib
import enum
import getpass
import json
import os
import platform
import shutil
import subprocess
import sys
import threading
from collections.abc import Sequence
from dataclasses import dataclass

from agent_swarm import roles
from agent_swarm.claim import LeaseLost
from agent_swarm.forge import DEFAULT_GITEA_BASE_URL, ForgeError, GiteaForge
from agent_swarm.forge_store import ForgeStore, Role, decode_claim_key
from agent_swarm.job import TEST_RUN, JobKind
from agent_swarm.pull import MissingCapability, Ticket, Workbench
from agent_swarm.store import VERDICTS

#: How long a claim survives without a beat, FOR AN INTERACTIVE EXECUTOR. Five minutes, against the
#: store's three-hour default, and the difference is the whole point of hosting a beater: the long
#: default exists to outlast the longest gate on a runner that may not beat at all, while this
#: process beats every 75 s and can therefore promise that a closed terminal frees the item within
#: five minutes rather than within an afternoon.
INTERACTIVE_LEASE_SECONDS = 300.0

#: Rows of terminal chrome the listing spends on things that are not jobs: the header line, the
#: blank line under it, and the summary footer. Subtracted when K is derived from the real screen.
_CHROME_ROWS = 4

#: What `K` falls back to when the terminal size cannot be read -- a pipe, a CI log, a dumb
#: terminal. 20 is the classic 24-row screen minus this chrome, so the fallback is the same number a
#: default terminal would have produced rather than a fresh invention.
_FALLBACK_ROWS = 24


class Exit(enum.IntEnum):
    """Every way this CLI can end. **The failure vocabulary, not a printing detail.**

    A SEPARATE CODE PER FACT, because a caller that cannot tell "somebody beat me" from "I cannot
    reach the forge" will retry both -- and retrying the second forever is how an outage reads as
    contention. The two that matter most are `OK` and `FORGE_UNREACHABLE`: an empty listing exits
    `OK` because looking and seeing nothing IS success, while failing to look is not.

    `USAGE = 2` is argparse's own, not a choice: argparse exits 2 on a bad command line before this
    module gets a say, so any other value here would mean one CLI with two usage codes.
    """

    OK = 0
    WORK_FAILED = 1
    USAGE = 2
    FORGE_UNREACHABLE = 3
    LOST_THE_RACE = 4
    MISSING_CAPABILITY = 5
    LEASE_LOST = 6
    INCONCLUSIVE = 7


def visible_rows() -> int:
    """How many jobs a person can see in ONE view of this terminal. **This is K.**

    DERIVED FROM THE SCREEN, NOT CHOSEN. `available`'s bound went unimplemented until this function
    existed, because K is not a taste question: the claimed-filter costs one round trip per job
    examined, and the only defensible number of jobs to examine is the number the reader can
    actually act on without scrolling. A constant would have been an invention; this is a
    measurement of the thing the argument was about.

    FALLS BACK RATHER THAN FAILING when there is no terminal -- a pipe, a CI log, `--json`.
    `shutil.get_terminal_size` already answers `COLUMNS`/`LINES` or its fallback, so the degenerate
    case is a small K and never a crash.
    """
    rows = shutil.get_terminal_size(fallback=(80, _FALLBACK_ROWS)).lines
    return max(1, rows - _CHROME_ROWS)


@dataclass(frozen=True, slots=True)
class Settings:
    """Everything the CLI needs that it must not decide for itself."""

    base_url: str
    repo: str
    namespace: str
    owner: str
    capabilities: frozenset[str]
    kind: JobKind
    lease_seconds: float


def default_owner() -> str:
    """`user@host`. STABLE ACROSS PROCESSES, which is what makes `report` able to follow a `take`.

    NOT a random id per invocation: the claim is owner-checked, so a fresh identity each run would
    make a person unable to report on their own work, and the second process would read as a
    stranger trying to steal a live claim. It is also the identity a human recognises in the forge
    UI, which is where they will go looking when something is stuck.
    """
    try:
        who = getpass.getuser()
    except (OSError, KeyError):
        # getpass consults the environment and then the password database; in a container with
        # neither, it raises. An unnamed executor cannot be released or reported on, so this
        # substitutes a marker that is visibly a fallback rather than inventing a plausible name.
        who = 'unknown-user'
    return f'{who}@{platform.node() or "unknown-host"}'


def build_workbench(settings: Settings) -> Workbench:
    """A RUNNER-role workbench. NO I/O -- construction never touches the network."""
    # NOT a literal: this was the fourth spelling of the account scheme, and the barest one. A change
    # of prefix would have left this workbench authenticating as an account nobody issues.
    forge = GiteaForge(settings.base_url, settings.repo, username=roles.account_for('agent'))
    store = ForgeStore(
        settings.namespace,
        forge,
        role=Role.RUNNER,
        lease_seconds=settings.lease_seconds,
    )
    return Workbench(store, owner=settings.owner, capabilities=settings.capabilities)


class Beater:
    """Keeps a ticket alive while work runs, and STOPS THE WORK when it cannot.

    A DAEMON THREAD, so it cannot keep this process alive past its work -- the whole promise is that
    closing the terminal loses the ticket, and a non-daemon beater would be a thread politely
    holding the claim open while nobody is watching.

    **A LOST LEASE IS NOT LOGGED AND SHRUGGED OFF.** When `renew` raises, another executor may
    already have taken the job, so continuing to run is doing work whose answer somebody else will
    publish. `on_lost` is called and the caller kills the child. That is the difference between a
    heartbeat and a progress bar: this one has consequences.

    **IT BEATS ON THE CADENCE THE CLAIM DEFINES** (`ForgeStore.beat_every` -> `claim.beat_interval`),
    never on a number chosen here. This class ALREADY GOT THAT WRONG once: it floored its own
    interval at 1.0 s, so against any lease under four seconds the first beat landed after the lease
    had already expired -- a heartbeat that cannot fire in time, which reads in a diff exactly like
    one that can. The floor never bound at the production lease, so only a test with a short lease
    exposed it. The interval is now supplied by the store and this class derives nothing.
    """

    def __init__(self, ticket: Ticket, *, on_lost, interval: float) -> None:
        self.ticket = ticket
        self.on_lost = on_lost
        self.interval = interval
        self.lost: LeaseLost | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name='claim-beater', daemon=True)

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.ticket.beat()
            except LeaseLost as exc:
                self.lost = exc
                self.on_lost(exc)
                return
            except ForgeError:
                # A TRANSIENT FAILURE IS NOT A LOST LEASE, and treating it as one would kill healthy
                # work every time the forge hiccups. The lease is what bounds this: beats run at a
                # quarter of it, so three consecutive failures can be absorbed before the claim
                # genuinely lapses -- and if they keep failing, `renew` will raise LeaseLost on its
                # own and the branch above fires. Silence here is bounded by that, not by hope.
                continue

    def __enter__(self) -> Beater:
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stop.set()
        self._thread.join(timeout=self.interval)


def _bind_children() -> str:
    """Make this process's death kill the work it starts. Returns what was established.

    NOT FATAL WHEN IT FAILS, unlike in the runner -- see the module docstring. The string is
    returned rather than logged so the caller can put it where a human sees it: the two mechanisms
    make different promises, and a caller that reported "bound" without naming which one would be
    reporting a guarantee it may not hold.
    """
    from agent_swarm.lifetime import LifetimeUnavailable, bind_children_to_this_process  # noqa: PLC0415

    # LAZY, and this is one of the two cases the no-lazy-import rule allows: `lifetime` binds the
    # PROCESS, so importing it at module scope would make merely importing this CLI -- to read its
    # help, or to run its tests -- create a job object and enrol the interpreter. A test suite that
    # imported it would bind pytest's own process tree.
    try:
        return bind_children_to_this_process().mechanism
    except LifetimeUnavailable as exc:
        return f'none ({exc})'


def cmd_list(bench: Workbench, args: argparse.Namespace) -> int:
    """Print what this box can take, and say what was actually OBSERVED.

    **THE EMPTY CASE IS THE WHOLE REASON THIS FUNCTION IS LONGER THAN ONE LINE.** "Nothing to do" is
    the laundering `Claimable` bans, with a friendly face: four different situations produce zero
    offers -- an empty queue, no capability, everything claimed, or the bound stopping the look --
    and a person acts differently on each. So the summary always states what was seen, and never
    concludes anything about what exists.
    """
    limit = args.limit if args.limit is not None else visible_rows()
    survey = bench.available(args.kind, limit=limit)
    jobs = survey.offered.jobs

    if args.json:
        print(
            json.dumps(
                {
                    'offered': [job.claim_key() for job in jobs],
                    'requires': {j.claim_key(): sorted(survey.offered.requirements_for(j)) for j in jobs},
                    'visible': survey.visible,
                    'capable': survey.capable,
                    'examined': survey.examined,
                    'limit': survey.limit,
                    'bound_bit': survey.bound_bit,
                    'owner': bench.owner,
                }
            )
        )
        return Exit.OK

    if jobs:
        print(f'{len(jobs)} item(s) you can take as {bench.owner}:')
        print()
        for job in jobs:
            needs = ', '.join(sorted(survey.offered.requirements_for(job)))
            print(f'  {job.claim_key()}{f"   requires: {needs}" if needs else ""}')
    else:
        # EVERY CLAUSE NAMES A NUMBER. A sentence a person can act on -- "18 visible, 0 you can do"
        # sends them to their capabilities, "18 visible, 18 you can do, 0 free" sends them to the
        # board -- and neither is reachable from the word "nothing".
        print(f'No item is currently free for you to take ({bench.owner}).')
    print()
    print(
        f'  observed: {survey.visible} open item(s) of kind {args.kind.value}, '
        f"{survey.capable} within this box's capabilities, {survey.examined} checked for a claim."
    )
    if survey.bound_bit:
        print(
            f'  NOT A COMPLETE ANSWER: the look stopped at {survey.limit} '
            f'(this screen fits {visible_rows()}). {survey.capable - survey.examined} more could be '
            f'checked with --limit.'
        )
    print(
        '  A forge listing can lag, so this is what was VISIBLE and never proof of what exists. '
        'An item may be taken by someone else between this line and your next command.'
    )
    return Exit.OK


def cmd_take(bench: Workbench, args: argparse.Namespace) -> int:
    """Claim, beat, run the command, report the verdict, release. ONE process, ONE lifetime.

    THE ORDER IS THE CONTRACT. The claim is taken BEFORE the child starts, the beater is running
    BEFORE the child starts, and the verdict is reported BEFORE the claim is released. Every other
    ordering leaves a window: work running under a claim nobody is holding, or an item free and
    unanswered while its result exists only in this process's memory.
    """
    job = decode_claim_key(args.key, kind=args.kind)
    if job is None:
        print(f'{args.key!r} is not a {args.kind.value} claim key', file=sys.stderr)
        return Exit.USAGE

    try:
        ticket = bench.take(job)
    except MissingCapability as exc:
        print(f'{exc}', file=sys.stderr)
        return Exit.MISSING_CAPABILITY
    if ticket is None:
        holder = bench.store.claim_owner(job)
        print(
            f'{args.key} is already held by {holder or "another executor"}; nothing was started.',
            file=sys.stderr,
        )
        return Exit.LOST_THE_RACE

    mechanism = _bind_children()
    print(f'holding {args.key} as {bench.owner} (child lifetime: {mechanism})', file=sys.stderr)

    child: subprocess.Popen[bytes] | None = None
    lost: list[LeaseLost] = []

    def on_lost(exc: LeaseLost) -> None:
        # STOP THE WORK. The claim is gone, so another executor may already be running this job and
        # will publish an answer; whatever this child produces is now unpublishable at best and a
        # duplicate at worst. Killing is the honest response to having lost the right to run.
        lost.append(exc)
        print(f'\nLEASE LOST: {exc}\nstopping the work.', file=sys.stderr)
        if child is not None:
            with contextlib.suppress(OSError):
                child.kill()

    try:
        with Beater(ticket, on_lost=on_lost, interval=bench.store.beat_every):
            child = subprocess.Popen(args.command)  # noqa: S603 -- the operator's own command line
            returncode = child.wait()
    except OSError as exc:
        # The command could not be started at all: not a FAIL, because nothing ran and a FAIL is a
        # statement about the work. INCONCLUSIVE is exactly this case in gate.py's vocabulary.
        ticket.report(verdict='INCONCLUSIVE', detail=f'could not start {args.command!r}: {exc}')
        print(f'could not start {args.command[0]!r}: {exc}', file=sys.stderr)
        return Exit.INCONCLUSIVE

    if lost:
        # NOTHING IS REPORTED. We do not hold the claim, so writing a verdict would overwrite an
        # answer that may already be there -- `report` refuses this too, and it is refused here
        # rather than attempted-and-caught so the reason is visible at the call site.
        return Exit.LEASE_LOST

    verdict = 'PASS' if returncode == 0 else 'FAIL'
    detail = f'{" ".join(args.command)} exited {returncode} (run by {bench.owner})'
    try:
        ticket.report(verdict=verdict, detail=detail)
    except LeaseLost as exc:
        print(f'{exc}', file=sys.stderr)
        return Exit.LEASE_LOST
    print(f'{verdict}: {detail}', file=sys.stderr)
    return Exit.OK if verdict == 'PASS' else Exit.WORK_FAILED


def cmd_report(bench: Workbench, args: argparse.Namespace) -> int:
    """Answer an item this owner still holds.

    USEFUL ONLY WHILE SOMETHING IS BEATING, and that is stated rather than papered over: a claim
    with no live holder lapses, and this then exits `LEASE_LOST` instead of overwriting whatever
    answer the next executor published. It exists for the operator whose work happened outside a
    `take` -- and its refusal is the honest half.
    """
    job = decode_claim_key(args.key, kind=args.kind)
    if job is None:
        print(f'{args.key!r} is not a {args.kind.value} claim key', file=sys.stderr)
        return Exit.USAGE
    ticket = Ticket(workbench=bench, job=job, owner=bench.owner)
    try:
        ticket.report(verdict=args.verdict, detail=args.detail)
    except LeaseLost as exc:
        print(f'{exc}', file=sys.stderr)
        return Exit.LEASE_LOST
    print(f'recorded {args.verdict} on {args.key}', file=sys.stderr)
    return Exit.OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='python -m agent_swarm.workbench_cli',
        description='Take work as a human or a TUI agent, on the same claim the CI runner uses.',
    )
    parser.add_argument('--base-url', default=os.environ.get('SWARM_BASE_URL') or DEFAULT_GITEA_BASE_URL)
    parser.add_argument('--repo', default=os.environ.get('SWARM_REPO'), help='OWNER/NAME')
    parser.add_argument('--namespace', default=os.environ.get('SWARM_NAMESPACE'))
    parser.add_argument('--owner', default=os.environ.get('SWARM_OWNER') or default_owner())
    parser.add_argument(
        '--capability',
        action='append',
        default=None,
        help='what this box HAS; repeatable. Defaults to SWARM_CAPABILITIES (comma-separated).',
    )
    parser.add_argument('--kind', choices=[k.value for k in JobKind], default=TEST_RUN.value)
    parser.add_argument('--lease-seconds', type=float, default=INTERACTIVE_LEASE_SECONDS)

    verbs = parser.add_subparsers(dest='verb', required=True)

    lister = verbs.add_parser('list', help='what this box can take')
    lister.add_argument(
        '--limit',
        type=int,
        default=None,
        help='how many candidates to check for a claim. Defaults to what this terminal can show.',
    )
    lister.add_argument('--json', action='store_true', help='the same facts as data')

    taker = verbs.add_parser('take', help='claim, run a command under the claim, report the verdict')
    taker.add_argument('key', help='a claim key, e.g. test-run/branch/abc')
    taker.add_argument('command', nargs=argparse.REMAINDER, help='-- the command to run')

    reporter = verbs.add_parser('report', help='answer an item this owner still holds')
    reporter.add_argument('key')
    reporter.add_argument('--verdict', required=True, choices=sorted(VERDICTS))
    reporter.add_argument('--detail', default='')
    return parser


def settings_from(args: argparse.Namespace) -> Settings:
    """Turn a parsed command line into settings, or raise `SystemExit` naming what is missing."""
    if not args.repo:
        raise SystemExit('--repo OWNER/NAME is required (or SWARM_REPO)')
    if not args.namespace:
        raise SystemExit('--namespace is required (or SWARM_NAMESPACE)')
    declared = args.capability
    if declared is None:
        raw = os.environ.get('SWARM_CAPABILITIES', '')
        declared = [token.strip() for token in raw.split(',') if token.strip()]
    return Settings(
        base_url=args.base_url,
        repo=args.repo,
        namespace=args.namespace,
        owner=args.owner,
        capabilities=frozenset(declared),
        kind=JobKind(args.kind),
        lease_seconds=args.lease_seconds,
    )


def main(argv: Sequence[str] | None = None, *, workbench: Workbench | None = None) -> int:
    """Parse, build, dispatch. Returns an :class:`Exit`; never raises for an expected failure.

    `workbench` IS AN INJECTION SEAM AND IT IS NOT DECORATION. Without it the only way to test this
    module would be to reach a real forge, so the tests would either not exist or be `live_forge` --
    and the human-versus-runner race, which is the property the whole surface rests on, would never
    be raced through the entry point that humans actually use. A test double supplied here runs the
    REAL argument parsing, the REAL dispatch and the REAL exit codes.
    """
    args = build_parser().parse_args(argv)
    args.kind = JobKind(args.kind)
    if workbench is None:
        workbench = build_workbench(settings_from(args))
    if args.verb == 'take':
        # argparse.REMAINDER keeps the `--` when it is the first token, and handing it to the shell
        # as a program name is the failure motronics measured in its own installer: uv read `--` as
        # a package and blamed the payload for a defect in the wrapper.
        if args.command and args.command[0] == '--':
            args.command = args.command[1:]
        if not args.command:
            print('take needs a command: ... take <key> -- <command>', file=sys.stderr)
            return Exit.USAGE

    handlers = {'list': cmd_list, 'take': cmd_take, 'report': cmd_report}
    try:
        return handlers[args.verb](workbench, args)
    except ForgeError as exc:
        # THE DISTINCTION THIS CLI EXISTS TO PRESERVE, at the last place it can be lost. "I looked
        # and saw nothing" exits OK; "I could not look" exits FORGE_UNREACHABLE. A CLI that rendered
        # both as an empty list would be `Claimable`'s ban defeated on the final line.
        print(f'could not reach the forge: {exc}', file=sys.stderr)
        return Exit.FORGE_UNREACHABLE


if __name__ == '__main__':
    sys.exit(main())
