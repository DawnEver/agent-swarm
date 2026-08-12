"""The two PRODUCTION implementations of the executor's protocols, kept out of the seam.

WHY THEY ARE NOT IN `agent_executor`, WHICH IS WHERE THEY WERE FIRST WRITTEN. That file guards its
own vocabulary with a test: `test_no_vendor_or_transport_name_appears_in_the_executor` tokenises the
source and refuses `subprocess`, `socket`, `node`, `provider`, `pid` and the vendor names. Putting a
process-spawning verifier beside the `Verifier` protocol tripped it immediately, and the guard was
RIGHT -- the seam exists so that "done" is somebody else's answer, and a file that both declares the
seam and reaches the operating system through it is no longer a seam. That is the same split
`fabric` already has against `SessionRunner`, arrived at from the other direction.

THIS IS THE `JOB` LAYER, NOT `DRIVER`, and that is a hard constraint rather than taste: both adapters
take a `Job`, `job` is a JOB-layer module, and a DRIVER module importing it would point UP the
dependency arrow, which `test_the_dependency_arrow_is_enforced` refuses. `layers.py` already
anticipates the case -- "the executor adapters live here too: a thing that turns a Job into a session
must speak both vocabularies" -- and a thing that turns a Job into a subprocess is the same shape.

MEASURED 2026-08-12: before this file existed, `Verifier` and `Workspace` were protocols with **no
implementation anywhere in `src/`**, so `AgentTaskExecutor` could not be constructed in production by
anyone willing to write the wiring. The suite was green because every test supplied its own fake --
which is what a protocol with no implementation always looks like from inside a test suite.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from agent_swarm.agent_executor import INCONCLUSIVE
from agent_swarm.job import Job


def _interpolate(part: str, job: Job) -> str:
    """`{key}` -> the claim key, and a literal brace survives untouched.

    A bare `part.format(...)` raises on any other placeholder and on an unmatched brace, which an
    operator will eventually type in a command line -- and a verifier that dies while composing its
    own argv reports nothing at all.
    """
    return part.replace('{key}', job.claim_key())


@dataclass(frozen=True, slots=True)
class CommandVerifier:
    """The definition of done as an operator states it: a command line. Satisfies `Verifier`.

    **IT NAMES NO PROJECT AND MUST NOT.** `argv` is the caller's -- in practice a gate runner, but
    this class knows nothing about one. A default here would be `DEFAULT_REPO` under a new spelling:
    a vendor-neutral layer holding one project's fact, invisible exactly because the default works.

    THREE OUTCOMES, AND THE THIRD IS THE WHOLE REASON THIS IS NOT FOUR LINES INLINE. Exit zero is
    PASS and non-zero is FAIL, but a command that could not START, or that never finished, has said
    NOTHING about the work:

    * a missing binary, a bad interpreter path, a permission error -- the operator's misconfiguration
    * a timeout -- the gate hung, which is not the same as the gate failing

    Reporting FAIL for either converts "I do not know" into a verdict AGAINST somebody's change, and
    the item is then closed as answered. INCONCLUSIVE is the honest word and it is also the word that
    means re-runnable; `admission.should_retry` already prices how often.

    Attributes:
        argv: the command. `{key}` in any element is replaced with the job's claim key, the same
            idiom `StaticBrief` uses, so one configured command can answer many jobs and say which.
        timeout_s: a CEILING, not a suggestion. Without one a hung gate holds the claim until the
            lease expires and the job is retried into the same hang forever.
        cwd: where to run it. `None` means this process's directory.
        detail_tail: how many characters of output to keep. The detail lands in a FORGE COMMENT, so
            an untruncated gate log is a request the server rejects -- the verdict lost to the size
            of its own evidence.

    """

    argv: Sequence[str]
    timeout_s: float
    cwd: Path | None = None
    detail_tail: int = 2000

    def verify(self, job: Job) -> tuple[str, str]:
        argv = [_interpolate(part, job) for part in self.argv]
        try:
            proc = subprocess.run(  # noqa: S603 -- argv is the operator's own command, by design
                argv,
                capture_output=True,
                text=True,
                timeout=self.timeout_s,
                cwd=self.cwd,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return INCONCLUSIVE, f'the verifier timed out after {self.timeout_s}s: {" ".join(argv)}'
        except (OSError, ValueError) as exc:
            # NOT A FAIL. This is the command not existing, not the code being wrong -- and the two
            # are indistinguishable to anyone reading a closed item weeks later.
            return INCONCLUSIVE, f'the verifier could not run ({exc.__class__.__name__}: {exc}): {" ".join(argv)}'
        verdict = 'PASS' if proc.returncode == 0 else 'FAIL'
        return verdict, f'exit {proc.returncode}\n{self._tail(proc)}'

    def _tail(self, proc: subprocess.CompletedProcess[str]) -> str:
        """The END of the output, and it is the end rather than the start on purpose: a failure's
        cause is where the run stopped, and a head-truncated report shows the banner of a tool that
        was about to say something useful.
        """
        text = f'{proc.stdout}{proc.stderr}'
        if len(text) <= self.detail_tail:
            return text
        return f'... (truncated to the last {self.detail_tail} chars)\n{text[-self.detail_tail :]}'


@dataclass(frozen=True, slots=True)
class TreeWorkspace:
    """ "Did anything change" over a real directory: every file's relative path and size.

    **WHAT IT MISSES, STATED HERE BECAUSE THE READER IS HERE:** a same-length edit. Rewriting five
    bytes with five different bytes leaves this fingerprint identical, so such a session is reported
    as having changed nothing.

    **THAT DIRECTION IS DELIBERATE AND IT IS THE SAFE ONE.** `AgentTaskExecutor` turns "changed
    nothing" into INCONCLUSIVE -- re-runnable, nobody harmed. The opposite error, claiming a change
    that did not happen, sends an untouched tree to the verifier and lets a green the session did not
    earn be recorded as a PASS attributed to this task. That is the exact failure the executor's
    no-change guard exists to prevent, so a fingerprint must fail toward silence.

    **THAT IS ALSO WHY MTIME IS NOT IN IT**, though it would close the same-length hole almost
    always. An mtime moves for a checkout, a build, a formatter, a `touch` -- events that change no
    content -- so including it would trade a rare false "unchanged" for a common false "changed",
    buying accuracy in the harmless direction with risk in the harmful one. Content hashing WOULD be
    exact in both directions and costs a full read of the tree per tick; it has not been measured
    here, and quoting a cost nobody took is what this package refuses.

    RELATIVE PATH, NOT FILE NAME, and that is a repair rather than a copy. The version promoted from
    `test_end_to_end` keyed on `p.name`, so two same-sized files sharing a name in different
    directories were one entry and a move between directories was invisible. The path costs nothing.
    """

    root: Path

    def fingerprint(self) -> str:
        entries = sorted(
            (path.relative_to(self.root).as_posix(), path.stat().st_size)
            for path in self.root.rglob('*')
            if path.is_file()
        )
        return repr(entries)
