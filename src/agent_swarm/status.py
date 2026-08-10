"""The verdict becomes a commit status -- the producer a protected branch waits for.

WHY THIS FILE EXISTS. `set_status` shipped, was measured against a real Gitea, and was called by
NOTHING. Verdicts went into work items and stopped there, so the `motronics/gate` context a
protected branch would require had no producer at all. Enabling protection in that state freezes
`main`: every merge waits on a check nobody publishes. That is the shape this project already names
elsewhere -- a flag existing is not a runner running -- landing on the merge path.

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
"""

from __future__ import annotations

from agent_swarm.forge import ROLE_ACCOUNTS, Forge, ForgeError
from agent_swarm.store import VERDICTS

#: The status context a merge waits on. ONE SPELLING, shared with `swarmctl`'s branch-protection
#: writer: a name agreed in two places is two definitions of one fact, and the failure -- a branch
#: protected against a check nobody publishes -- looks exactly like a broken gate.
STATUS_CONTEXT = 'motronics/gate'

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


class StatusPublisher:
    """Publishes one verdict as one commit status, as the verifier.

    IDEMPOTENT BY THE FORGE'S SEMANTICS, not by bookkeeping here: a commit status is keyed by
    (sha, context) and a second POST for the same pair REPLACES the first. So a retry after a lost
    response is safe, and a re-run that changes its mind overwrites rather than accumulating -- which
    is what makes this survivable on the crash path the store's verdict write is already ordered for.
    """

    def __init__(self, forge: Forge, *, context: str = STATUS_CONTEXT) -> None:
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
        self.context = context

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
