"""The fabric-backed `SessionRunner`: L0 session transport, and nothing above it.

MEASURED, NOT ASSUMED. Every claim in this module was checked from a plain bash shell with no Claude
session involved, against fabric 0.1.17 on 2026-08-10:

    node scripts/ping.mjs        3 nodes ALIVE (G, WS1, WS2), 0/64 sessions each
    spawn+send+close, codex      ok, exitCode 0, reply 'BANANA', spawn 633 ms, total 7306 ms
    spawn+send+close, claude     ok, exitCode 1, reply = a model-selection ERROR, total 2489 ms
    node down (bad host/port)    ECONNREFUSED in 18 ms -- fast, not a hang
    unknown provider             JSON-RPC -32000 naming the four configured providers
    bogus model, claude          exitCode 1, fluent English explaining the problem
    bogus model, codex           exitCode **0** and an EMPTY reply

**FABRIC IS CALLABLE FROM A LIBRARY PROCESS.** That was the open question and the answer is the good
one: a headless runner can drive agent work unattended, so 7x24 does not require a human-attended
Claude session. The transport is an ES module, so the bridge is `node` plus one small driver script;
`node` is a hard dependency OF THIS ADAPTER and deliberately not of `agent_swarm`, whose offline
tier stays green with no node installed.

THE SEAM SURVIVED TWO PROVIDERS, WHICH IS THE ONLY TEST THAT MATTERS. `claude` and `codex` are
genuinely different backends and they took the SAME fields -- provider, model, write, project -- and
answered in the same `{text, turn, usage?}` shape. No provider-specific branch exists here, and if
one ever seems necessary the seam is in the wrong place and that is the thing to report.

THE CLAUDE RESULT IS THE INTERESTING ONE, and it is why `agent_executor` refuses to read a session's
own words. That session STARTED, ran a turn, returned fluent English and closed -- and the English
was "There's an issue with the selected model". A layer that treated "the session answered" as "the
work happened" would have called that a completed task. `exitCode` told the truth (1) where the text
did not, so `completed` is keyed to the exit code and never to the prose.

AND THE EXIT CODE IS NOT SUFFICIENT EITHER, which a later probe corrected me on. Given the same
bogus model, `codex` returns exitCode 0 with an EMPTY reply -- a session that ended cleanly having
done nothing at all. The two providers therefore fail with different signatures and NEITHER signal
is trustworthy alone: the code catches claude and misses codex, the text catches codex and is
actively misleading for claude. This layer does not try to reconcile them. `completed` means exactly
"the session process ended cleanly" and nothing more, and the guard that refuses to call an inert
session a success is `AgentTaskExecutor`'s unchanged-workspace check -- which is above this layer
precisely because it needs to know what the JOB was.

WHAT THIS MODULE MUST NEVER GROW: a scheduler. fabric reports capacity facts and refuses a spawn
only at the static per-node `maxSessions` ceiling. All dynamic admission belongs to
`agent_swarm.admission`. `fleet_capacity` below returns FACTS and decides nothing -- if it ever
starts choosing a node by free memory, the second scheduler this design refuses has been built.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from agent_swarm.agent_executor import SessionOutcome
from agent_swarm.job import Job

#: Where the plugin cache lives. A default, not a discovery protocol: an operator with a different
#: layout passes `plugin_dir` explicitly rather than having this module go looking.
DEFAULT_PLUGIN_ROOT = Path.home() / '.claude' / 'plugins' / 'cache' / 'cc-market' / 'fabric'

#: fabric's own spawn deadline is 180 s. The driver must be allowed to exceed it and REPORT, so this
#: sits above it: a Python-side kill would lose the diagnostic the driver was about to print.
DEFAULT_TIMEOUT_SECONDS = 900.0

#: MEASURED per-node session ceiling, and the number that actually binds. Three nodes at 64 is 192
#: slots against ~56 GB free; at the design's ~200 MB per claude session memory would allow ~280.
#: So the operator-declared ceiling is the real limit today and is the knob to raise -- not the RAM.
MEASURED_SESSIONS_PER_NODE = 64

_DRIVER = Path(__file__).parent / '_fabric_driver.mjs'
_PING_LINE = re.compile(
    r'^(?P<node>\S+)\s+ALIVE\s+v(?P<version>\S+)\s+up=(?P<uptime>\d+)s\s+cpu=(?P<cpu>\d+)\s+'
    r'free=(?P<free_mb>\d+)MB\s+sessions=(?P<sessions>\d+)/(?P<max_sessions>\d+)'
)


class SessionTransportUnavailable(RuntimeError):
    """The transport cannot be used here -- no node, no plugin, no configured fleet.

    A RUNTIME condition, never an import error. `agent_swarm` must import and its offline tier must
    stay green on a box with no node installed, so the absence of the transport is discovered when
    something tries to USE it. `AgentTaskExecutor` turns that into INCONCLUSIVE, which is the honest
    verdict: nobody ran the work, so nobody knows.
    """


@dataclass(frozen=True, slots=True)
class NodeCapacity:
    """What one fabric node reports about itself. FACTS ONLY -- this type decides nothing.

    It exists so `admission` can be handed real numbers instead of guesses. The moment something
    here starts picking a node, the scheduler has been forked.
    """

    name: str
    version: str
    cpu: int
    free_mb: int
    sessions: int
    max_sessions: int

    @property
    def free_slots(self) -> int:
        return max(0, self.max_sessions - self.sessions)


def latest_plugin_dir(root: Path = DEFAULT_PLUGIN_ROOT) -> Path:
    """The highest installed fabric version.

    SORTED NUMERICALLY, not lexically: `0.1.9` must not beat `0.1.17`, and a string sort says it
    does. That is the entire reason this is a function rather than a `max()` at a call site -- the
    wrong answer here silently pins the fleet to a version whose behaviour was never measured.
    """
    if not root.is_dir():
        msg = f'no fabric plugin cache at {root}'
        raise SessionTransportUnavailable(msg)
    versions = [path for path in root.iterdir() if path.is_dir() and re.fullmatch(r'[\d.]+', path.name)]
    if not versions:
        msg = f'no fabric version directories under {root}'
        raise SessionTransportUnavailable(msg)
    return max(versions, key=lambda path: tuple(int(part) for part in path.name.split('.')))


class FabricSessionRunner:
    """Drives one fabric session per `run`, through `node` and a small driver script.

    Args:
        provider: `claude`, `codex`, `deepseek` or `kimi` -- whatever the operator configured. The
            probe found `codex` clean and `claude` misconfigured on this box, which is exactly the
            kind of fact that must be an argument rather than a default buried in code.
        model: passed through untouched. fabric resolves aliases; this layer must not.
        write: whether the session may modify the tree. False is the safe default and a task that
            needs edits must ASK for them -- a runner that always granted write would make the
            no-change guard in `AgentTaskExecutor` the only thing standing between a stray session
            and the repository.
        node: which configured fabric node, by name. `None` takes the first, and CHOOSING is not
            done here: picking by free memory would be scheduling, which belongs to `admission`.

    Construction performs NO I/O and never raises for a missing transport, so a fleet config can be
    built on a box that cannot run sessions. The refusal happens at `run`.
    """

    def __init__(
        self,
        *,
        provider: str = 'codex',
        model: str | None = None,
        write: bool = False,
        project: str | None = None,
        node: str | None = None,
        plugin_dir: Path | None = None,
        node_binary: str = 'node',
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.provider = provider
        self.model = model
        self.write = write
        self.project = project
        self.node = node
        self.plugin_dir = plugin_dir
        self.node_binary = node_binary
        self.timeout_seconds = timeout_seconds

    def run(self, brief: str, *, job: Job) -> SessionOutcome:
        """Spawn a session, send `brief` as one turn, close, and report what happened.

        `completed` IS KEYED TO THE EXIT CODE, NEVER TO THE TEXT. Measured: a claude session that
        was misconfigured still started, answered in fluent English, and closed -- with exit code 1
        and a body that read like an explanation rather than an error. Reading the prose would have
        scored that as a finished task.

        Raises:
            SessionTransportUnavailable: no node binary, no plugin, or the driver could not be run
                at all. Distinguished from a session that ran badly, because the two need different
                responses: one is a broken box, the other is a job that needs looking at.
        """
        driver, plugin = self._resolve()
        request = json.dumps(
            {
                'provider': self.provider,
                'model': self.model,
                'write': self.write,
                'project': self.project,
                'node': self.node,
                'prompt': brief,
                'pluginDir': str(plugin),
            }
        )
        try:
            finished = subprocess.run(
                [self.node_binary, str(driver)],
                input=request,
                capture_output=True,
                text=True,
                check=False,
                cwd=plugin,
                timeout=self.timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            # NOT a transport failure: the session may well have started and be running still. It is
            # an incomplete session, which is precisely the case the executor scores INCONCLUSIVE.
            return SessionOutcome(
                completed=False,
                transcript=f'the fabric driver exceeded {self.timeout_seconds:.0f}s and was killed',
                self_report='',
            )
        except OSError as exc:
            msg = f'cannot run {self.node_binary!r}: {exc}'
            raise SessionTransportUnavailable(msg) from exc

        try:
            payload = json.loads(finished.stdout)
        except json.JSONDecodeError as exc:
            # The driver always prints JSON, so this means it did not get far enough to print any --
            # a missing module, a syntax error, a node too old. That is a broken box, not a job.
            msg = f'the fabric driver produced no JSON (rc={finished.returncode}): {finished.stderr.strip()[:400]}'
            raise SessionTransportUnavailable(msg) from exc

        if not payload.get('ok'):
            msg = f'fabric refused the session: {payload.get("error", "(no reason given)")}'
            raise SessionTransportUnavailable(msg)

        return SessionOutcome(
            completed=payload.get('exitCode') == 0,
            transcript=json.dumps(payload, indent=2),
            self_report=payload.get('text', ''),
        )

    def _resolve(self) -> tuple[Path, Path]:
        """`(driver path, plugin dir)`, or refuse.

        The driver stays inside THIS package and receives the plugin path as data, which is why it
        imports dynamically. Copying it into the plugin tree would mean writing into someone else's
        installed package, and pinning it there would make an upgrade silently run the old bridge.
        """
        if shutil.which(self.node_binary) is None:
            msg = f'{self.node_binary!r} is not on PATH; the fabric transport needs node'
            raise SessionTransportUnavailable(msg)
        plugin = self.plugin_dir if self.plugin_dir is not None else latest_plugin_dir()
        if not (plugin / 'engine' / 'node-client.mjs').is_file():
            msg = f'{plugin} does not look like a fabric plugin (no engine/node-client.mjs)'
            raise SessionTransportUnavailable(msg)
        return _DRIVER, plugin


def fleet_capacity(
    *,
    plugin_dir: Path | None = None,
    node_binary: str = 'node',
    timeout_seconds: float = 30.0,
) -> list[NodeCapacity]:
    """Ask every configured node what it has. FACTS ONLY.

    A DEAD NODE IS ABSENT FROM THE RESULT, not represented as a node with zero capacity: zero
    capacity means "full and working", and a caller that could not tell the two apart would report a
    dead fleet as a busy one. `ping.mjs` exits non-zero when any node is dead, and that is
    deliberately NOT treated as failure here -- a partially reachable fleet is still a fleet.

    This function chooses nothing. Admission is `agent_swarm.admission`'s job, and a helper here
    that picked "the emptiest node" would be the second scheduler this design exists to refuse.
    """
    if shutil.which(node_binary) is None:
        msg = f'{node_binary!r} is not on PATH; the fabric transport needs node'
        raise SessionTransportUnavailable(msg)
    plugin = plugin_dir if plugin_dir is not None else latest_plugin_dir()
    try:
        finished = subprocess.run(
            [node_binary, str(plugin / 'scripts' / 'ping.mjs')],
            capture_output=True,
            text=True,
            check=False,
            cwd=plugin,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        msg = f'could not probe the fabric fleet: {exc}'
        raise SessionTransportUnavailable(msg) from exc
    return parse_ping(finished.stdout)


def parse_ping(text: str) -> list[NodeCapacity]:
    """Parse `ping.mjs` output. Lines that are not ALIVE are skipped -- see `fleet_capacity`.

    A PARSER AND NOT A REGEX AT A CALL SITE, so that the one place this format is understood can be
    tested without a fleet. A DEAD line is not an error here: it is a node that has nothing to
    offer, and the caller learns that from its absence.
    """
    found: list[NodeCapacity] = []
    for line in text.splitlines():
        match = _PING_LINE.match(line.strip())
        if match is None:
            continue
        found.append(
            NodeCapacity(
                name=match['node'],
                version=match['version'],
                cpu=int(match['cpu']),
                free_mb=int(match['free_mb']),
                sessions=int(match['sessions']),
                max_sessions=int(match['max_sessions']),
            )
        )
    return found
