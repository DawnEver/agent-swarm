"""The verdict becomes a commit status -- the producer a protected branch waits for.

WHY THIS FILE EXISTS. `set_status` shipped, was measured against a real Gitea, and was called by
NOTHING. Verdicts went into work items and stopped there, so the gate context a protected branch
would require had no producer at all. Enabling protection in that state freezes `main`: every merge
waits on a check nobody publishes. That is the shape this project already names elsewhere -- a flag
existing is not a runner running -- landing on the merge path.

WHY IT IS SEPARATE FROM `ForgeStore`. An architecture test asserts the store cannot reach
`set_status`, and this file does not weaken it: the store carries the JOB's answer, this carries the
COMMIT's, and they are different questions with different audiences. A job's verdict is read by the
scheduler; a commit status is read by the forge's merge gate and by a human looking at a branch.

WHY THE VERIFIER ROLE. Gitea has no scope for commit status -- writing one needs repository write,
which `swarm-agent` also has, and that was MEASURED on 2026-08-10 by publishing a status as the
agent and watching it succeed. So "only the verifier marks a commit green" is carried by which
process holds which credential and by nothing else. This module cannot enforce it either; what it
can do is refuse to be constructed with a forge that is not the verifier's, so the boundary is
checkable at the seam instead of assumed at every call site.

WHY THE CONTEXT CARRIES THE WRITER'S NAME -- THE LAST-WRITE-WINS HOLE
=====================================================================

A commit status is keyed by (sha, context) and a second POST REPLACES the first. For ONE writer that
is a feature: a retry after a lost response is safe. For SEVERAL it is a correctness defect, and
there are several -- three boxes hold the `swarm-verifier` credential on this deployment, so all
three pass the construction check above and all three used to write one slot. A box finishing a
stale tree a second later overwrote a real FAIL with a PASS, on the exact key the merge gate reads.
Gitea cannot delete a commit status, so that wrong answer is permanent.

THE AVAILABILITY HALF AND THE CORRECTNESS HALF ARE IN TENSION, NOT THE SAME BUG. "Only the verifier
publishes" makes one box a single point of failure; handing the credential to three boxes answers
that and is what CREATED the overwrite. Every step towards availability adds a writer to one slot.
So the resolution is to stop sharing the slot: this publisher writes `<context>/<runner>`, one key
per writer, and the writer set may then grow without bound with no write able to land on another's
key. Single-writer-per-fact -- the architecture's core invariant -- applied to the one fact that had
escaped it.

KEYED BY THE WRITER AND NOT BY THE JOB. A job-keyed context would put two runners answering the same
job back into one slot, and that collision is the LIKELIER one: a runner whose lease expired and the
runner that took over are answering the same job by construction.

WHAT THIS MOVES ONTO THE SERVER, and it must be wired there or the fix is only half real: the branch
rule can no longer wait on one literal context. It must require the PATTERN `<context>/*`, and the
deployment must be checked to block a merge when NOTHING matches that pattern -- `merge_decision`
states that rule on this side, and a server that admitted a merge on zero matching contexts would
disagree with it in the unsafe direction.

WHY THIS MODULE NO LONGER KNOWS THE CHECK'S NAME
================================================

It held `STATUS_CONTEXT = '<project>/gate'` with a comment warning that a second copy lived in the
consumer's `swarmctl` and that the two must not drift. That comment described a real failure -- a
branch protected against a check nobody publishes reads exactly like a broken gate -- and then
proposed vigilance as the remedy for it. Two copies kept in step by a comment is two definitions.

ONE DEFINITION MEANS ONE OWNER, not two spellings that agree. The fact "this project's gate check is
called X" belongs to whatever configures that project's branch protection, which is the consumer.
So this package holds no copy at all and takes the name as an argument, and `swarmctl`'s value stops
being the duplicate and becomes the definition.
`tests/test_this_package_names_no_specific_project.py` fails if a project name comes back.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from agent_swarm.forge import ROLE_ACCOUNTS, Forge, ForgeError
from agent_swarm.store import VERDICTS

#: verdict word -> forge status state.
#:
#: INCONCLUSIVE IS `error`, NOT `failure` AND NOT `pending`, and the choice is the whole reason this
#: mapping is data rather than an `if`:
#:
#: * `failure` would claim the change is bad. It is not; nobody found out. A human reading a red
#:   branch would go looking for a defect that does not exist.
#: * `pending` would leave the merge waiting forever on a run that has already finished, which is
#:   indistinguishable from a runner that died -- and a fleet cannot tell those apart either.
#: * `error` is terminal AND blocks the merge AND says "this did not conclude", which is exactly the
#:   three things true about an INCONCLUSIVE gate.
#:
#: All three block a merge. That is deliberate: a merge must never proceed on no information, and
#: the safe direction here costs a re-run while the unsafe one costs a bad main.
VERDICT_STATES = {'PASS': 'success', 'FAIL': 'failure', 'INCONCLUSIVE': 'error'}

#: The one state that admits a merge. Derived from `VERDICT_STATES` rather than spelled again, so a
#: new verdict word cannot acquire a merge-admitting state without this line changing with it.
_ADMITS_MERGE = frozenset({VERDICT_STATES['PASS']})

#: Characters a runner name may not contain, and each one is a different failure:
#:
#: * `/` -- would forge a context under another namespace, or a deeper one the `<context>/*` glob
#:   does not match, so the status exists and the gate cannot see it;
#: * glob metacharacters -- the branch rule reads these as a pattern, so a runner named `*` would
#:   name contexts it does not own.
#:
#: Whitespace is refused separately, by category rather than by listing: a context is compared
#: verbatim by the branch rule, and a name with a space in it is one nobody can type into that rule
#: correctly.
_RUNNER_FORBIDDEN = ('/', '*', '?', '[', ']')

#: The same list minus `/`, which a context legitimately contains: `<owner>/gate` is the usual shape
#: and the branch rule matches it verbatim. Derived rather than spelled twice, so a metacharacter
#: added to one list cannot be forgotten in the other.
_CONTEXT_FORBIDDEN = tuple(c for c in _RUNNER_FORBIDDEN if c != '/')


@dataclass(frozen=True, slots=True)
class MergeDecision:
    """Whether the statuses on one commit admit a merge, and WHICH contexts stopped it.

    `blocking` is named rather than counted because the operator's next question is always "which
    box said no", and a bare boolean sends them to the web UI to find out.
    """

    allowed: bool
    blocking: tuple[str, ...]
    reason: str


def merge_decision(states: Mapping[str, str], *, context: str) -> MergeDecision:
    """Aggregate every `<context>/<runner>` status on one commit into one answer.

    THIS IS OUR SIDE'S STATEMENT OF THE RULE the branch protection glob enforces, written here so it
    is testable without a server and so a disagreement between the two is visible as a diff rather
    than as a merge. It decides nothing at merge time -- the server does -- and it is deliberately
    the stricter of the two readings wherever they could differ.

    TWO PROPERTIES, both of which a naive `all(...)` gets wrong:

    * ONE non-success blocks, whatever the others say and in whatever order they arrived. That is
      the whole point of per-writer keys: a disagreement SURVIVES instead of being overwritten.
    * NO matching context at all BLOCKS. An empty mapping is what a fleet that is entirely down
      looks like, and `all([])` is `True` -- the unsafe direction, reached by accident.

    MATCHING IS `<context>/` AND NOTHING ELSE. Not a bare `startswith`, which would swallow
    `<context>way/x`, and not the unsuffixed `<context>` itself: the branch rule's glob matches
    neither, so counting either here would claim an authority the server does not grant. Nothing in
    this package can write the unsuffixed context -- `runner` is required and validated -- so its
    only source is a legacy or foreign writer, and ignoring it is a statement about scope rather
    than an oversight.

    Args:
        states: context -> state, for ONE commit. The caller does the per-sha filtering, because
            reading statuses is I/O and this function is a decision.
        context: the BASE context, unsuffixed. The same value the publishers were built with.
    """
    prefix = f'{context}/'
    mine = {ctx: state for ctx, state in states.items() if ctx.startswith(prefix)}
    if not mine:
        return MergeDecision(
            allowed=False,
            blocking=(),
            reason=f'no status matches {prefix}*: a merge must never proceed on no information',
        )
    blocking = tuple(sorted(ctx for ctx, state in mine.items() if state not in _ADMITS_MERGE))
    if blocking:
        return MergeDecision(
            allowed=False,
            blocking=blocking,
            reason=f'{len(blocking)} of {len(mine)} matching contexts do not admit a merge',
        )
    return MergeDecision(allowed=True, blocking=(), reason=f'all {len(mine)} matching contexts are green')


class StatusPublisher:
    """Publishes one verdict as one commit status, as the verifier, UNDER THIS RUNNER'S OWN CONTEXT.

    IDEMPOTENT WITHIN A WRITER, by the forge's semantics rather than by bookkeeping here: a commit
    status is keyed by (sha, context) and a second POST for the same pair REPLACES the first. So a
    retry after a lost response is safe and a re-run that changes its mind overwrites rather than
    accumulating -- and because the context carries `runner`, that replacement can only ever hit this
    box's own answer. See the module docstring for what the shared key cost.

    Args:
        forge: must hold the verifier credential, checked at construction.
        context: the BASE context; the published one is `<context>/<runner>`.
        runner: WHICH BOX is answering. Required, and it must be this box's stable identity rather
            than a per-run token: the key is what makes a re-run replace its own previous answer, and
            a fresh name per run would leave every superseded answer standing forever.
    """

    def __init__(self, forge: Forge, *, context: str, runner: str) -> None:
        # THE RUNNER NAME IS VALIDATED AT CONSTRUCTION, alongside the role and for the same reason:
        # a commit status cannot be deleted, so a name producing a colliding or unmatchable context
        # would be permanent. Neither check does any I/O.
        if not runner or any(c.isspace() for c in runner):
            msg = f'runner must be a non-empty name with no whitespace, got {runner!r}'
            raise ForgeError(msg)
        if any(c in runner for c in _RUNNER_FORBIDDEN):
            msg = f'runner must not contain any of {"".join(_RUNNER_FORBIDDEN)!r}, got {runner!r}'
            raise ForgeError(msg)
        # THE CONTEXT IS OPERATOR INPUT NOW that this package no longer holds a copy, so it gets the
        # same treatment as the runner name. `/` is legal INSIDE it -- `<owner>/gate` is the usual
        # shape -- but not at either end, where it would produce a doubled or empty path segment
        # that the branch rule's glob does not match.
        if not context or any(c.isspace() for c in context) or context.strip('/') != context:
            msg = f'context must be a non-empty name with no whitespace and no leading/trailing "/", got {context!r}'
            raise ForgeError(msg)
        if any(c in context for c in _CONTEXT_FORBIDDEN):
            msg = f'context must not contain any of {"".join(_CONTEXT_FORBIDDEN)!r}, got {context!r}'
            raise ForgeError(msg)
        username = getattr(forge, 'username', None)
        if username != ROLE_ACCOUNTS['verifier']:
            # REFUSED AT CONSTRUCTION, so the mistake surfaces where the forge was built rather than
            # at the one call per job that publishes. The server will not refuse it -- measured --
            # so this seam is the only place the role can be checked at all.
            msg = (
                f'a status publisher must hold the verifier credential, got {username!r}. '
                f"Build it with default_forge('verifier')."
            )
            raise ForgeError(msg)
        self.forge = forge
        self.runner = runner
        #: The context ACTUALLY PUBLISHED, composed once and exposed: the operator wiring the branch
        #: rule needs the exact spelling, and re-deriving it by hand is how the two copies of the
        #: base context drifted in the first place.
        self.context = f'{context}/{runner}'

    def publish(self, sha: str, *, verdict: str, detail: str) -> None:
        """Publish `verdict` for `sha`. Raises on an unknown verdict BEFORE any I/O.

        Validating first matters here for the same reason it does in `record_verdict`: a publisher
        that checked afterwards would already have written a status for a word it then rejected, and
        a commit status cannot be deleted on Gitea.
        """
        if verdict not in VERDICTS:
            msg = f'verdict must be one of {sorted(VERDICTS)}, got {verdict!r}'
            raise ValueError(msg)
        if not sha:
            # A status needs a COMMIT. Publishing against an empty sha would 404 far from here, and
            # the caller that forgot to thread one through is the one who needs to hear about it.
            msg = 'publishing a status needs a commit sha'
            raise ValueError(msg)
        self.forge.set_status(sha, state=VERDICT_STATES[verdict], context=self.context, description=detail)
