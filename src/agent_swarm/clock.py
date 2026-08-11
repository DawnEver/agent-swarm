"""THE CLOCK: something has to pull a one-shot tick, and it must not be the tick.

WHAT THIS IS. Extracted from motronics' `scripts/ci/ci_loop.py`, where it had been running and
measured for months. A tick is a one-shot by design -- it does at most one thing and exits, so the
lock is the concurrency control and a crash costs one tick rather than a service. This is the
something that pulls it, and it deliberately stays OUTSIDE the tick: each iteration spawns a FRESH
process, so every property the one-shot design buys is preserved -- a tick that dies takes nothing
with it, and this clock holds no state to corrupt.

SESSION-BOUND, ON PURPOSE (user directive, restated 2026-08-09). This is a TERMINAL PROGRAM. You
start it in a window and close the window to stop it, so what is running is visible and directly
manageable. A Windows Scheduled Task version of it existed briefly and was DELETED at the source --
a background runner surviving reboots is exactly the invisible thing the design refuses, and keeping
it as an opt-in would have been a dual entry point.

WHICH IS WHY THE HEARTBEAT MATTERS MORE, NOT LESS. Under a terminal-managed design "nobody started
it" is the failure mode that actually happens, and it used to be invisible: on 2026-08-09 a box had
a registered task reading `LastRunTime=1932`, the never-ran sentinel, with three red groups and
nothing saying they were unrun. So each pass stamps the heartbeat, and a status reader can call the
runner ALIVE / STALE / DEAD. The stamp happens THROUGH a spawn, not only between spawns, because a
tick that claims a slow group owns this clock for up to 30 minutes and a clock that went quiet for
that long would be ignored -- and a false "runner down" is how a reader learns to skip the one line
that matters.

WHAT IT DOES NOT DO. It does not kill a job that is already running. Close the terminal mid-run and
the tick's own child keeps going to completion and publishes its verdict -- it is a separate process
tree, and killing a 25-minute verdict because a window closed would be the worse default.

THE WAKE, AND WHY THE POLL STAYED. A new work item can WAKE an idle clock, so scheduling latency
drops from the poll interval to milliseconds. The original plan said "webhook INSTEAD OF polling"
and that one word is refused here: from the runner's side "the webhook did not arrive" and "there is
no work" are the SAME OBSERVATION -- nothing happened. A design whose failure mode is
indistinguishable from its idle state cannot be monitored, and a missed webhook would be a fleet
that quietly stops. So the poll is the FLOOR and the wake is an ACCELERATOR over it: the clock waits
on an Event with the poll interval as its timeout, and a timeout ticks exactly as before. The wake
carries NO WORK, only "look sooner" -- what exists is the tick's to discover.

THE LISTENER IS NOT A DAEMON, the same directive wearing a new disguise. It serves on a DAEMON
THREAD inside this process, so closing the terminal takes it with the clock; it registers nothing,
writes nothing that outlives the process, and binds LOOPBACK by default -- an unauthenticated
"spawn a tick" endpoint on 0.0.0.0 is not a convenience. A port it cannot bind costs the
acceleration and nothing else.

EVERY COUPLING TO A PROJECT IS A PATH, AND EVERY PATH IS AN ARGUMENT. WHICH tick to spawn, WHERE
the policy is, WHAT maintenance means, WHERE the heartbeat is written: :class:`Clock` takes all of
them and defaults NONE of them. This package has removed a working default once already -- a forge
whose `repo` defaulted to one project, invisible precisely because it worked -- and a defaulted tick
command is the same defect with a bigger blast radius: the day it is wrong, this clock is driving
somebody else's project on somebody else's box, on a cadence nobody chose.

WHAT IS NOT CLAIMED. The clock does not know whether a tick DID anything; a tick's exit status is
information for the next iteration, never a reason to stop pulling. It does not bound how long a
tick runs -- that ceiling belongs to whatever the tick spawns. And it proves its own liveness only
as far as the filesystem allows: an unwritable heartbeat reads as STALE, which is the safe
direction, not as an error.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import socketserver
import subprocess
import sys
import threading
import time
import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast

#: The documented range is 30-60 s. Polling faster buys nothing against jobs of 25+ minutes, and
#: every tick costs a round trip to the control plane -- the machine this whole design refuses to
#: load. Used only when a policy file stays SILENT about the cadence.
DEFAULT_POLL_INTERVAL_S = 45.0

#: How often the clock re-stamps while a tick is running. Independent of the poll interval, which is
#: about how often WORK is looked for; this is about how often LIVENESS is proved, and a long run
#: needs the second far more often than the first.
HEARTBEAT_S = 30.0

#: A maintenance pass is a one-shot that exits in seconds. The ceiling is generous because the pass
#: talks to a forge over the network, and hostile because a pass that hangs would hold the clock.
MAINTENANCE_TIMEOUT_S = 900.0

#: Loopback ONLY by default. `POST /wake` is unauthenticated -- it carries no payload and does
#: nothing but shorten a sleep -- but on 0.0.0.0 it is still a way for anything on the network to
#: make this box spawn ticks. Reaching it from a forge is a deliberate choice, spelled at the call.
WAKE_HOST = '127.0.0.1'

#: The port a forge's webhook posts to. Overridable; a port of 0 means "poll only".
WAKE_PORT = 8787

#: The path a wake is posted to. ONE spelling, so the sender and the listener cannot disagree.
WAKE_PATH = '/wake'

#: The serving thread's name. Named so a test can assert it is a DAEMON, which is the property that
#: keeps this from becoming the background runner the module docstring records as deleted.
WAKE_THREAD_NAME = 'swarm-wake-listener'


def poll_interval(policy_path: Path, *, default: float = DEFAULT_POLL_INTERVAL_S) -> float:
    """The poll interval declared in ``policy_path``, or ``default`` if it declares none.

    READ RATHER THAN HARDCODED so the cadence is decided in the same file as everything else about
    scheduling. The path is REQUIRED: a clock that guessed where its policy lived would read a file
    belonging to whichever checkout it happened to start in.

    AN UNREADABLE OR MALFORMED POLICY FALLS BACK RATHER THAN RAISING, and that direction is chosen:
    the cost of an UNKNOWN cadence is polling at 45 s instead of 30, while the cost of raising is a
    fleet that will not start because somebody left a trailing bracket in a config file.

    **A DECLARED NON-POSITIVE CADENCE IS A DIFFERENT CASE AND IT RAISES.** The sentence above used
    to cover it and was false: `poll_seconds = 0` does not cost "45 s instead of 30", it makes
    `wait_for_work` return instantly forever, so the clock spawns a fresh tick process as fast as
    the machine allows. That is a fork bomb wearing a config key. The distinction is KNOWN-BAD
    versus UNKNOWN: a malformed file leaves the value unknown, and falling back is the humane
    answer; a file that says zero has stated something that cannot be a cadence, and papering over
    it would substitute a number the operator did not choose while their fleet melted.

    Raises:
        ValueError: the policy declares a cadence that is not a positive, finite number of seconds.
    """
    try:
        policy = tomllib.loads(Path(policy_path).read_text(encoding='utf-8'))
    except (OSError, tomllib.TOMLDecodeError):
        return default
    declared = policy.get('schedule', {}).get('poll_seconds', default)
    try:
        seconds = float(declared)
    except (TypeError, ValueError) as exc:
        msg = f'{policy_path} declares poll_seconds = {declared!r}, which is not a number of seconds'
        raise ValueError(msg) from exc
    if not math.isfinite(seconds) or seconds <= 0:
        msg = (
            f'{policy_path} declares poll_seconds = {declared!r}. A non-positive or infinite cadence '
            f'is not a slow clock -- it is a clock that spawns a tick as fast as the machine allows, '
            f'or one that never ticks again. State a positive number of seconds.'
        )
        raise ValueError(msg)
    return seconds


def maintenance_policy(policy_path: Path) -> dict | None:
    """The declared retention policy, or ``None`` when the policy is SILENT.

    SILENCE MEANS DO NOTHING, and that is the whole safety property. Deletion is irreversible, so
    the absence of a declaration must never be read as a default retention window -- unknown is not
    a policy. A repository that says nothing about retention keeps everything, forever, loudly.

    THE HUMAN CONFIRMS THE POLICY, NOT EACH RUN. A prune tool defaults to a dry run and asks for
    `--yes` because an operator at a terminal is confirming a MEASUREMENT. Here there is no
    operator, and the thing a human decided once -- "closed work items are worthless after N days"
    -- is a retention policy rather than a judgement call per item. So the policy IS the consent, it
    lives in the same file as every other scheduling fact, and the pass itself still reports exactly
    what it removed.

    The returned mapping is handed BACK to the caller's command builder unchanged, including any key
    this module does not understand: what a pass needs to be told is the consumer's vocabulary.
    """
    try:
        policy = tomllib.loads(Path(policy_path).read_text(encoding='utf-8'))
    except (OSError, tomllib.TOMLDecodeError):
        return None
    section = policy.get('maintenance') or {}
    days = section.get('prune_closed_after_days')
    if not days or float(days) <= 0:
        return None
    return {**section, 'days': float(days), 'every_hours': float(section.get('every_hours', 24.0))}


def maintenance_due(stamp_path: Path, every_hours: float, *, now: float | None = None) -> bool:
    """Is a pass due? An UNREADABLE stamp means YES, and that direction is chosen deliberately.

    The two failure modes are not symmetric: reading "never run" when a pass did run costs one extra
    idempotent sweep, while reading "recently run" when the stamp is corrupt costs an archive that
    grows forever with nothing saying so. The cheap error is the one to make.
    """
    now = time.time() if now is None else now
    try:
        last = float(json.loads(Path(stamp_path).read_text(encoding='utf-8'))['at'])
    except (OSError, ValueError, KeyError, TypeError):
        return True
    return now - last >= every_hours * 3600.0


#: What a maintenance command builder returns: the argv to spawn, or a STRING which is a REFUSAL.
#:
#: THE TYPE IS THE DISCRIMINATOR, and it is unambiguous rather than clever: a command is a SEQUENCE
#: of arguments and never one string -- this clock will not accept a shell-joined line and split it
#: for you, because that is where quoting bugs become arbitrary execution. So a bare `str` can only
#: be prose, and prose from a builder is the reason no pass ran. The alternative, returning `None`,
#: throws that reason away: "nobody is pruning" is a fact about the fleet, and a caller that wants
#: it -- a status command will -- cannot get it from a line of somebody's stdout.
MaintenanceCommand = Sequence[str] | str


def run_maintenance(
    policy: Mapping[str, object],
    *,
    build_command: Callable[[Mapping[str, object]], MaintenanceCommand],
    stamp_path: Path,
    timeout_s: float = MAINTENANCE_TIMEOUT_S,
) -> str | None:
    """Spawn ONE maintenance pass. Returns WHY it did not happen, or ``None`` on success.

    A FRESH PROCESS, never an in-process call, for the same reason the tick is spawned: this clock
    holds no state that a failing pass can corrupt, and a pass that dies takes nothing with it.

    THE COMMAND IS THE CALLER'S, wholesale. What upkeep means -- which tool, which repo, which
    flags -- is a fact about a project, and a clock that built even part of that command would be
    naming somebody's tooling in fleet infrastructure. `build_command` receives the policy mapping
    and answers with argv, or with a string saying why it cannot.

    IT RETURNS THE REASON rather than logging and answering ``None``. Deletion typically needs a
    credential only one box holds, so on every other box this returns a reason on every pass, which
    is correct and must stay VISIBLE rather than becoming a silent no-op.

    STAMPED ONLY ON SUCCESS. Stamping a failed pass would silence the retry that fixes it.
    """
    command = build_command(policy)
    if isinstance(command, str):
        return command
    argv = [str(part) for part in command]
    if not argv:
        return 'the maintenance command builder produced an empty command'
    try:
        done = subprocess.run(argv, capture_output=True, text=True, encoding='utf-8', timeout=timeout_s, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f'{type(exc).__name__}: {exc}'
    if done.returncode != 0:
        output = (done.stdout or done.stderr or '').strip()
        return output.splitlines()[-1] if output else f'exit {done.returncode}'
    stamp_path = Path(stamp_path)
    stamp_path.parent.mkdir(parents=True, exist_ok=True)
    stamp_path.write_text(json.dumps({'at': time.time()}), encoding='utf-8')
    return None


def stamp_heartbeat(path: Path, *, now: float | None = None, pid: int | None = None) -> None:
    """Record that the clock is alive, as of ``now``.

    BEST-EFFORT BY CONSTRUCTION: a clock that died because it could not write its own liveness file
    would be the joke version of this mechanism, so every failure here is swallowed. The cost of
    swallowing is bounded and self-correcting -- an unwritten beat reads as STALE, which is the safe
    direction and exactly what a reader should be told when the runner cannot write to disk.

    The file belongs under a machine-local directory, never one that is committed: a heartbeat in
    version control is another box's liveness, which is worse than none.
    """
    payload = {'at': time.time() if now is None else now, 'pid': os.getpid() if pid is None else pid}
    try:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding='utf-8')
    except OSError:
        pass


def wait_for_work(wake: threading.Event, interval: float) -> bool:
    """Wait up to ``interval`` for a wake. ``True`` if one arrived, ``False`` if the POLL fired.

    THE FLOOR IS THE TIMEOUT. This is a sleep with an early exit, which is exactly what keeps a lost
    webhook from being a stopped fleet: no wake, no listener, no network -- the clock still ticks on
    its own cadence, and the return value says which of the two happened so a reader is never
    guessing.

    CLEARED ON THE WAY OUT, or the first webhook of the day turns the loop into a busy spin forever.
    """
    woke = wake.wait(timeout=interval)
    wake.clear()
    return woke


class _WakeHandler(BaseHTTPRequestHandler):
    """Sets the clock's Event on `POST /wake`. It reads no body and trusts nothing in the request.

    THE PAYLOAD IS DELIBERATELY IGNORED. The only authority this endpoint has is "look for work
    sooner", and the tick then decides what exists by reading the store itself. A handler that
    believed the request about WHICH work to run would be a second, unauthenticated scheduler.

    POST ONLY. A GET is a browser probe, a health check or a link preview, and none of those is a
    reason to spawn a tick.
    """

    protocol_version = 'HTTP/1.1'

    # `do_POST`/`do_GET`: BaseHTTPRequestHandler dispatches on these exact names.
    def do_POST(self) -> None:
        if self.path.split('?', 1)[0] != WAKE_PATH:
            self.send_error(404)
            return
        with contextlib.suppress(OSError, ValueError):
            self.rfile.read(int(self.headers.get('Content-Length') or 0))
        cast('_WakeServer', self.server).wake_event.set()
        self.send_response(204)
        self.send_header('Content-Length', '0')
        self.end_headers()

    def do_GET(self) -> None:
        self.send_error(405, 'a wake is a POST')

    def log_message(self, *_args: object) -> None:
        """Silent. The clock's stdout is the fleet's status line; a request log would bury it."""


class _WakeServer(ThreadingHTTPServer):
    """A `ThreadingHTTPServer` carrying the clock's Event, and whose worker threads are daemons.

    `daemon_threads` matters as much as the serving thread's own flag: a request in flight when the
    terminal closes must not hold the process open either.
    """

    daemon_threads = True

    #: NOT reusable, and this was found by a test rather than reasoned out. `HTTPServer` sets
    #: `allow_reuse_address = 1`, and on Windows SO_REUSEADDR lets a SECOND process bind a port a
    #: first is actively listening on: two clocks would both "have" a listener while only one ever
    #: received a wake, and the other would silently fall back to its poll without saying so. A
    #: refused bind is the honest answer -- it prints, and degrades to the floor.
    allow_reuse_address = False

    wake_event: threading.Event


def start_wake_listener(
    wake: threading.Event, *, host: str = WAKE_HOST, port: int = WAKE_PORT, out=sys.stdout
) -> _WakeServer | None:
    """Serve `POST /wake` on a DAEMON thread, or return ``None`` having said why.

    RETURNS ``None`` RATHER THAN RAISING when the port is taken -- a second clock on the box, a stray
    process -- because the cost of no listener is bounded and known: the poll interval, which is what
    the fleet ran on before this existed. Dying over a convenience socket would trade 45 s of latency
    for a stopped fleet.

    The thread is a daemon and the server owns nothing outside this process, so closing the terminal
    ends it. That is not an implementation detail: the clock is session-bound by user directive, and
    a listener that outlived the window would reintroduce the deleted background runner under a new
    name.
    """
    if port <= 0:
        return None
    try:
        server = _WakeServer((host, port), _WakeHandler)
    except OSError as exc:
        out.write(f'[clock] no wake listener on {host}:{port} ({exc}); polling only, which is the floor\n')
        out.flush()
        return None
    server.wake_event = wake
    thread = threading.Thread(target=server.serve_forever, name=WAKE_THREAD_NAME, daemon=True)
    thread.start()
    out.write(f'[clock] wake listener on http://{host}:{port}{WAKE_PATH} (POST) -- the poll still runs beneath it\n')
    out.flush()
    return server


def stop_wake_listener(server: socketserver.BaseServer | None) -> None:
    """Shut the listener down. A no-op on ``None``, so a caller never branches on "did it start"."""
    if server is None:
        return
    with contextlib.suppress(OSError):
        server.shutdown()
        server.server_close()


def _no_maintenance(_policy: Mapping[str, object]) -> MaintenanceCommand:
    """The builder a clock gets when its consumer declared no upkeep. It REFUSES, and says so.

    Not an empty command and not a silent skip: "this clock runs no maintenance" is a state an
    operator should be able to read off the status line, and a `None` here would be indistinguishable
    from a pass that quietly did nothing.
    """
    return 'no maintenance command was declared for this clock'


@dataclass(frozen=True)
class Clock:
    """A clock bound to ONE consumer's tick, policy and paths. Every binding is REQUIRED.

    NO FIELD BELOW HAS A DEFAULT THAT REACHES OUTSIDE THIS PROCESS, and that is the design rather
    than caution. The three couplings this was extracted from were all paths -- the tick script, the
    policy file, the maintenance tool -- and each worked, which is why none of them was visible. A
    default that works is invisible until the day it is wrong, and the day it is wrong this clock is
    spawning somebody else's script on a cadence nobody chose.

    Attributes:
        tick_command: argv of the ONE-SHOT to spawn each iteration. A fresh process per tick is the
            whole design; an importable callable is deliberately not accepted, because in-process
            work is state this clock promises not to hold.
        policy_path: the TOML this clock reads its cadence and its upkeep window from.
        heartbeat_path: where liveness is proved. Machine-local; never committed.
        maintenance_stamp: where the last successful upkeep pass is recorded. Same rule.
        build_maintenance_command: turns the policy into argv, or into a string saying why not.
        beat_command: an optional second one-shot run on the HEARTBEAT cadence, for a fleet-visible
            liveness signal that a local file cannot provide. Measured 2026-08-09: without it, a
            runner that pushed its fleet-wide beat once at the start of a long job wrote itself OUT
            of the fleet while the local file still said ALIVE -- two liveness signals contradicting
            each other precisely when the runner is doing the most valuable thing it ever does. ONE
            cadence constant drives both, so they cannot drift apart.
        heartbeat_every_s: the cadence of both beats. A number, not a policy key: how often liveness
            is proved is a property of who READS it, and every reader of this fleet is this package.
    """

    tick_command: Sequence[str]
    policy_path: Path
    heartbeat_path: Path
    maintenance_stamp: Path
    build_maintenance_command: Callable[[Mapping[str, object]], MaintenanceCommand] = field(default=_no_maintenance)
    beat_command: Sequence[str] | None = None
    heartbeat_every_s: float = HEARTBEAT_S

    def __post_init__(self) -> None:
        """Refuse an empty tick command AT CONSTRUCTION, where the caller still is.

        An empty argv would fail per-iteration inside the loop's `OSError` arm, which reports and
        KEEPS GOING by design -- so a misconfigured clock would poll forever, printing, and looking
        exactly like a clock whose ticks are merely finding nothing to do.
        """
        if not list(self.tick_command):
            msg = 'Clock needs a tick command to spawn; there is no default one-shot to fall back on'
            raise ValueError(msg)
        if not math.isfinite(self.heartbeat_every_s) or self.heartbeat_every_s <= 0:
            # REFUSED HERE, BESIDE THE OTHER CONSTRUCTION CHECK, because the failure it prevents is
            # not a slow heartbeat. `run_once_through` waits `min(interval, heartbeat_every_s)`, so
            # a zero cadence turns that poll into a busy spin that stamps the heartbeat file and
            # SPAWNS `beat_command` as fast as the machine allows -- a subprocess per iteration, for
            # the whole length of a tick. It reads as a liveness signal working extremely well.
            msg = (
                f'Clock needs a positive heartbeat cadence, got {self.heartbeat_every_s!r}. A '
                f'non-positive one does not beat slowly; it spins, spawning the beat command as '
                f'fast as the machine allows.'
            )
            raise ValueError(msg)

    def beat(self) -> None:
        """Refresh the fleet-visible liveness signal, if one was declared. Never fatal.

        A SPAWN, not an import: the beat belongs to the consumer's one-shot, whose value is holding
        no state, and importing it here to call one function would drag the scheduler into this
        process -- the property every spawn in this file exists to preserve.
        """
        if not self.beat_command:
            return
        with contextlib.suppress(OSError, subprocess.TimeoutExpired):
            subprocess.run([str(p) for p in self.beat_command], check=False, timeout=self.heartbeat_every_s)

    def run_once_through(self, interval: float) -> str | None:
        """Spawn ONE tick and stamp liveness until it exits. Returns a spawn error, or ``None``.

        POLLED RATHER THAN WAITED ON, and this is the property to keep. A tick that claims a slow
        group occupies this clock for up to 30 minutes; a clock that only stamped BETWEEN spawns
        would look dead for that whole window and would teach every reader to ignore the one line
        that means the fleet stopped.

        THE TICK'S EXIT STATUS IS NOT CHECKED, deliberately: a non-zero exit is information for the
        next iteration, not a reason to stop pulling.
        """
        try:
            proc = subprocess.Popen([str(p) for p in self.tick_command])
        except OSError as exc:
            return str(exc)
        while proc.poll() is None:
            stamp_heartbeat(self.heartbeat_path)
            self.beat()
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=min(interval, self.heartbeat_every_s))
        stamp_heartbeat(self.heartbeat_path)
        return None

    def upkeep(self) -> str | None:
        """Run a maintenance pass if one is DUE, or return why it did not happen.

        MAINTENANCE RIDES THE CLOCK THAT ALREADY EXISTS rather than becoming a second service. It is
        a one-shot that exits in seconds and nobody waits on, so a terminal of its own would be a
        process whose only job is to sleep for a week -- a daemon in disguise, and this design
        deleted one of those at the source.

        SILENCE IN THE POLICY MEANS NO PASS RUNS AT ALL, and that answer is ``None`` here exactly as
        a successful pass is: neither is a reason to print. What must never be silent is a pass that
        was due and REFUSED, which comes back as a string.
        """
        policy = maintenance_policy(self.policy_path)
        if not policy or not maintenance_due(self.maintenance_stamp, float(policy['every_hours'])):
            return None
        return run_maintenance(policy, build_command=self.build_maintenance_command, stamp_path=self.maintenance_stamp)

    def run(
        self,
        *,
        interval: float | None = None,
        once: bool = False,
        wake_host: str = WAKE_HOST,
        wake_port: int = WAKE_PORT,
        out=sys.stdout,
    ) -> int:
        """Tick until interrupted. `Ctrl-C`, or closing the terminal, is the stop.

        A BAD SPAWN IS REPORTED AND THE CLOCK KEEPS GOING. A loop that exits on one failed spawn
        stops being a clock, and stops QUIETLY -- the failure mode this whole design is built to
        make impossible. `__post_init__` already refused the one such failure that is permanent.
        """
        interval = poll_interval(self.policy_path) if interval is None else interval
        out.write(f'[clock] ticking every {interval:g}s -- Ctrl-C, or close this terminal, to stop\n')
        out.flush()
        wake = threading.Event()
        listener = start_wake_listener(wake, host=wake_host, port=wake_port, out=out)
        try:
            while True:
                stamp = datetime.now(UTC).isoformat(timespec='seconds')
                failure = self.run_once_through(interval)
                if failure:
                    out.write(f'[clock] {stamp} could not start a tick: {failure}\n')
                why = self.upkeep()
                if why:
                    # SAID OUT LOUD. A refusal that repeats every pass is the honest state of a box
                    # that lacks the credential, and it must not decay into a silent no-op.
                    out.write(f'[clock] maintenance: {why}\n')
                out.flush()
                if once:
                    return 0
                if wait_for_work(wake, interval):
                    out.write('[clock] woken by a wake request\n')
                    out.flush()
        except KeyboardInterrupt:
            out.write('\n[clock] stopped\n')
            out.flush()
            return 0
        finally:
            stop_wake_listener(listener)
