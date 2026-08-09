"""The SECOND executor: an LLM coding session, answering in the same three words.

WHY THIS FILE IS THE ONE THAT TESTS THE DESIGN
==============================================

`job.py` claims "one loop, two kinds" and `loop.py` states that conversational-versus-deterministic
is an attribute of the EXECUTOR rather than a system boundary. Until now there was exactly one
executor -- the deterministic gate runner -- so the claim was **unfalsified, not verified**. A
protocol with one implementation is a description of that implementation.

This is the second one. Note what it did NOT need: no new field on `Job`, no branch in `run_one`, no
second queue. It reads `job.claim_key()` and nothing else, exactly as the gate runner does. If it
had needed a field, the single scheduler would already have become two.

THE VERDICT IS EARNED, NEVER ASSERTED
=====================================

**An agent session that says "done" is not PASS.** That sentence is the whole design. A deterministic
runner cannot lie to you about its exit code; a language model can produce a confident, fluent,
entirely wrong completion report, and it does so in the same register as a correct one. So the
session's self-report is recorded as CONTEXT and is never, on any path, promoted to a verdict.

The definition of done is the VERIFIER's verdict -- in practice `gate.py`, reached through the
`Verifier` seam so this package keeps its zero dependencies and so the test suite can drive a real
one. The executor's own contribution is two refusals that a deterministic runner never needs:

1. **A session that changed nothing yields INCONCLUSIVE, never PASS.** This is the subtle one. Run a
   coding agent that does nothing at all against an already-green tree and the gate says PASS --
   truthfully, about a tree nobody touched. Attributing that to the task is how a backlog empties
   itself without work being done. A green tree you did not touch is not evidence about your task.
2. **A session that did not finish yields INCONCLUSIVE, whatever the tree looks like.** A crash or a
   timeout leaves work half-applied, and a half-applied change that happens to pass is the most
   dangerous green there is. INCONCLUSIVE is also the word that means "re-runnable", which is what a
   killed session actually needs; `admission.should_retry` already prices how often.

WHAT THIS STILL CANNOT CATCH, named because it is the failure a deterministic runner does not have:
a session that satisfies the verifier by attacking the verifier -- deleting the failing test,
loosening the tolerance, marking an xfail. The gate goes green and it is EARNED by the letter of the
rule. Nothing here detects that, and nothing in this package can: it needs a diff review, which is a
judgement, not a check. `SessionOutcome.transcript` and the workspace fingerprint are kept precisely
so that a reviewer has something to look at.

THE SESSION LAYER IS NOT REIMPLEMENTED HERE
===========================================

Spawning and driving claude/codex sessions is `fabric`'s job and it already does it. This file
defines the seam (`SessionRunner`) and stops; `fabric.FabricSessionRunner` is the implementation.

THE SEAM IS NOT A CEREMONY. It was written before the transport existed and then MEASURED against
two genuinely different backends -- `claude` and `codex` -- which took the same fields and answered
in the same shape. Nothing here names fabric, node, MCP, a session id or a pid, and that is the
layering rule: L1 may say issue/branch/gate, L0 says session/turn/node/pid, and neither vocabulary
leaks. A `provider=` argument reaching this file would be the first sign it had.
"""

from __future__ import annotations

import traceback
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from agent_swarm.job import Job
from agent_swarm.store import VERDICTS

#: Returned when the executor refuses to attribute a tree's state to this session. Not a soft FAIL:
#: INCONCLUSIVE is the word that means "nobody has answered this yet, and it is worth re-running".
INCONCLUSIVE = 'INCONCLUSIVE'


@dataclass(frozen=True, slots=True)
class SessionOutcome:
    """What an LLM session produced. NOTE WHAT IS NOT HERE: a verdict.

    A session reports on itself and cannot be the judge of itself, so this type deliberately offers
    no field an executor could mistake for an answer. `self_report` is the model's own summary and is
    evidence about the SESSION, never about the code.

    Attributes:
        completed: did the session finish on its own terms (not killed, not timed out)? A crashed
            session's tree state cannot be attributed to a finished attempt.
        transcript: the full session log, kept for the human who will review the diff.
        self_report: the model's closing claim. Recorded, never believed.
    """

    completed: bool
    transcript: str = ''
    self_report: str = ''


@runtime_checkable
class SessionRunner(Protocol):
    """Something that can drive one LLM coding session to completion. L0 transport, no decisions."""

    def run(self, brief: str, *, job: Job) -> SessionOutcome:
        """Drive a session for `job` against `brief`. Raising is a legitimate outcome (a timeout,
        a dead provider); the executor turns it into INCONCLUSIVE rather than letting it escape."""
        ...


@runtime_checkable
class Verifier(Protocol):
    """The definition of done. In practice `gate.py`.

    THE SEAM EXISTS SO THAT "DONE" IS SOMEBODY ELSE'S ANSWER. An executor that decided for itself
    whether the work was finished would be grading its own homework twice over -- once as the model
    and once as the judge.
    """

    def verify(self, job: Job) -> tuple[str, str]:
        """Return `(verdict, detail)`. `verdict` must be one of :data:`~agent_swarm.store.VERDICTS`."""
        ...


@runtime_checkable
class Workspace(Protocol):
    """The tree the session works in. Only ever asked whether it CHANGED."""

    def fingerprint(self) -> str:
        """A value that differs if the working tree differs. A git diff hash, a tree hash, an mtime
        roll-up -- the executor does not care which, only that equal means untouched."""
        ...


class AgentTaskExecutor:
    """Drives an LLM session, then asks the verifier what actually happened.

    Satisfies `loop.Executor`, so `run_one` schedules it exactly as it schedules the gate runner --
    same claim, same lease, same verdict vocabulary, same store.

    Args:
        session: the L0 transport. Not implemented in this package; see `FabricSessionRunner`.
        verifier: the definition of done. Its verdict IS the verdict.
        workspace: used only to answer "did this session change anything at all".
        brief: turns a job into the instruction the session is given.
    """

    def __init__(
        self,
        *,
        session: SessionRunner,
        verifier: Verifier,
        workspace: Workspace,
        brief: Brief,
    ) -> None:
        self.session = session
        self.verifier = verifier
        self.workspace = workspace
        self.brief = brief

    def execute(self, job: Job) -> tuple[str, str]:
        """Run the session, then earn a verdict. Never returns the session's own opinion."""
        before = self.workspace.fingerprint()

        try:
            outcome = self.session.run(self.brief.for_job(job), job=job)
        except Exception:  # noqa: BLE001 -- a dead provider is an outcome, not a crash of the loop
            return INCONCLUSIVE, f'the session did not run:\n{traceback.format_exc()}'

        if not outcome.completed:
            # BEFORE the verifier, deliberately. A half-applied change that happens to be green is
            # the most dangerous PASS available, and asking the gate first would produce exactly it.
            return INCONCLUSIVE, _context('the session did not complete', outcome)

        if self.workspace.fingerprint() == before:
            return INCONCLUSIVE, _context(
                'the session changed nothing; a tree it did not touch being green is not evidence about this task',
                outcome,
            )

        verdict, detail = self.verifier.verify(job)
        if verdict not in VERDICTS:
            # NOT coerced into one of the three. A fourth state laundered into INCONCLUSIVE is the
            # invented verdict the vocabulary exists to refuse, and a broken verifier must be loud.
            msg = f'verifier returned {verdict!r}, not one of {sorted(VERDICTS)}'
            raise ValueError(msg)
        return verdict, _context(detail, outcome)


@runtime_checkable
class Brief(Protocol):
    """Turns a job into the instruction a session is given."""

    def for_job(self, job: Job) -> str: ...


@dataclass(frozen=True, slots=True)
class StaticBrief:
    """One instruction for every job, with the claim key interpolated. For tests and simple fleets."""

    template: str = 'Complete the task tracked as {key}. Do not modify tests to make them pass.'

    def for_job(self, job: Job) -> str:
        return self.template.format(key=job.claim_key())


def _context(detail: str, outcome: SessionOutcome) -> str:
    """Attach the session's own account BELOW the verdict's evidence, clearly labelled.

    The order and the label are the point. A reader scanning the top of a report must meet the
    measured result first; the model's account of itself is context, and mixing the two is how a
    confident summary comes to be read as a result.
    """
    claim = outcome.self_report.strip() or '(the session said nothing)'
    return f'{detail}\n\n--- the session said (NOT the verdict) ---\n{claim}'
