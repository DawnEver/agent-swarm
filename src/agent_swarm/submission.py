"""THE ONE UNIT THAT CROSSES INTO THE TRUNK. A base, a head, a declared intent, and who sent it.

WHY THE UNIT IS THIS AND NOT AN AGENT, A TASK OR AN ATTEMPT. A participant may be a person, a
session, or a controller running its own subagents for weeks. If the global protocol named agents it
would cost O(agents) -- thirty workstreams of eight subagents each is 240 actors that every reader of
the queue would have to enumerate. Naming what CROSSES costs O(workstreams) instead, so a
participant's internals stay private for a cost reason rather than a tidiness one. `Task` and
`Attempt` may exist INSIDE a participant; promoting them to global objects buys nothing until a
concrete cross-participant need appears.

WHY IT IS CALLED A SUBMISSION AND WILL NOT BE CALLED ANYTHING ELSE. The obvious alternative spelling
is taken TWICE in the first consuming project -- once for a schedulable item and once for a request
for a verdict -- and a third meaning of one word in one system is a reader following the wrong
definition into the wrong module. The name is a decision, recorded here so it is not re-litigated.

IMMUTABILITY IS THE TRANSPORT'S PROPERTY, NOT A PROMISE IN THIS DOCSTRING. The record is a frozen
dataclass, which stops one process editing its own copy and stops nothing else. What actually holds
is that :func:`publish` pushes the ref WITHOUT `--force`: git refuses a non-fast-forward update, and
an orphan commit over an existing ref is never a fast-forward, so the second writer is REFUSED by
the server rather than by a check the writer could forget. That is also why the ordinal is allocated
by collision instead of by reading the maximum -- two participants that both read "4" both write 5,
and with a forcing write the loser's submission is accepted, acknowledged, and then gone.

THE DECLARED INTENT IS INPUT, NEVER A LOCK. A submission whose observed effects exceed its declared
paths is ACCEPTED; the deviation is information for review and for integration ORDER. Nothing here
compares the declaration to the diff, and a guard that refused the mismatch would be a path lock
wearing an organisational hat -- the exact arrangement the queue exists to avoid, since git already
detects real collisions exactly, at merge time.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from agent_swarm import refs
from agent_swarm.refstore import GitRefStore, RefUnreachable

#: The name inside the submission's tree. HALF OF A CONTRACT between :func:`publish` and
#: :func:`read`, so it is one constant rather than two literals that can drift while both pass.
FILENAME = 'submission.json'

#: How many ordinals :func:`create` will try before giving up. A CEILING, not a tuning knob: a
#: remote that refuses every push -- revoked credentials, a full disk -- is indistinguishable from
#: heavy contention at the call site, and without a bound the two look identical forever. It is
#: comfortably above the number of participants that can realistically race one allocation, so
#: exhausting it means something is wrong rather than something is busy.
CREATE_ATTEMPTS = 32

#: Seconds any one git call here may take. A ceiling on a hang, not a measurement of a fetch.
GIT_TIMEOUT_S = 120.0


class OrdinalTaken(RuntimeError):
    """That ordinal already carries a submission, or every ordinal tried did.

    Raised rather than returned because there is no useful result: the caller's submission does not
    exist. It is the same exception for one refused push and for an exhausted :func:`create` loop,
    since both mean "this submission did not land" and both are retried by asking again.
    """


@dataclass(frozen=True, slots=True)
class Submission:
    """What a participant proposes: from `base`, take `head`, for this reason.

    BOTH ENDS ARE RECORDED, and `base` is the one that earns its keep. A head alone cannot say what
    the participant believed it was building on, so a submission that merges cleanly against a trunk
    that has moved a long way is indistinguishable from one that was written this minute -- and how
    far the base has fallen behind is exactly the input an integration order wants.
    """

    ordinal: int
    participant: str
    base: str
    head: str
    intent: str
    declared_paths: tuple[str, ...] = field(default=())

    def __post_init__(self) -> None:
        """Ordinals are 1-based, and the check is here because the ref NAME depends on it: a 0 or a
        negative gives an ordering with no first element, and `-1` is not a segment that parses back
        to the number that wrote it."""
        if self.ordinal < 1:
            msg = f'ordinal must be >= 1, got {self.ordinal}'
            raise ValueError(msg)

    def ref(self) -> str:
        """Where this submission lives. DELEGATED to the ref grammar rather than spelled here: a
        second copy of a ref root does not raise when it drifts, it writes where nothing looks."""
        return refs.submission_ref(self.ordinal)

    def payload(self) -> dict[str, Any]:
        return {
            'ordinal': self.ordinal,
            'participant': self.participant,
            'base': self.base,
            'head': self.head,
            'intent': self.intent,
            'declared_paths': list(self.declared_paths),
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> Submission:
        """Rebuild from what was stored. The paths come back as a TUPLE deliberately: JSON has one
        sequence type, so the obvious reader hands back a list and the record silently acquires a
        mutable, unhashable field that the frozen dataclass no longer protects."""
        return cls(
            ordinal=int(payload['ordinal']),
            participant=payload['participant'],
            base=payload['base'],
            head=payload['head'],
            intent=payload['intent'],
            declared_paths=tuple(payload.get('declared_paths') or ()),
        )


def publish(store: GitRefStore, submission: Submission) -> str:
    """Write `submission` at its ordinal, or refuse. Returns the commit the ref now points at.

    THE PUSH IS NOT FORCED, and that single omission is the whole immutability mechanism. An orphan
    commit is never a fast-forward of another orphan commit, so an ordinal that already carries a
    submission is rejected BY THE SERVER -- not by a read-then-write this function could lose a race
    on, and not by a flag a later editor could add `--force` to without noticing what it was for.

    Raises:
        OrdinalTaken: the ordinal already carries a submission, or the push was refused for any
            other reason the remote reported. The two are not separated here because the caller's
            response is identical -- the submission did not land, so ask for another ordinal -- and
            a distinction the caller cannot act on is a distinction that goes stale.
    """
    blob = store.stdin_text(json.dumps(submission.payload(), indent=2), 'hash-object', '-w', '--stdin')
    tree = store.stdin_text(f'100644 blob {blob}\t{FILENAME}\n', 'mktree')
    commit = store.text('commit-tree', tree, '-m', f'submission {submission.ordinal}: {submission.intent}')
    pushed = store.run('push', store.remote, f'{commit}:{submission.ref()}', timeout=GIT_TIMEOUT_S)
    if pushed.returncode != 0:
        msg = (
            f'submission {submission.ordinal} was not accepted -- the ordinal is taken or the '
            f'remote refused: {pushed.stderr.strip() or "(nothing on stderr)"}'
        )
        raise OrdinalTaken(msg)
    return commit


def read(store: GitRefStore, ordinal: int) -> Submission:
    """Read submission `ordinal` back off the remote.

    IT FETCHES FIRST, because the ref belongs to the remote and this checkout may never have seen
    it. Reading a local copy would answer confidently about a submission somebody else has since
    published -- the wrong answer being available is worse than no answer.

    Raises:
        RefUnreachable: the ref could not be fetched or its payload could not be read. One
            exception, because "there is no such submission" and "the remote is down" are both
            answered by asking again later, and neither is a fact about the submission.
    """
    ref = refs.submission_ref(ordinal)
    fetched = store.run('fetch', store.remote, ref, timeout=GIT_TIMEOUT_S)
    if fetched.returncode != 0:
        msg = f'cannot fetch {ref}: {fetched.stderr.strip() or "(nothing on stderr)"}'
        raise RefUnreachable(msg)
    shown = store.run('cat-file', '-p', f'FETCH_HEAD:{FILENAME}', timeout=GIT_TIMEOUT_S)
    if shown.returncode != 0:
        msg = f'{ref} carries no {FILENAME}: {shown.stderr.strip() or "(nothing on stderr)"}'
        raise RefUnreachable(msg)
    return Submission.from_payload(json.loads(shown.stdout))


def submitted_ordinals(store: GitRefStore) -> tuple[int, ...]:
    """Every ordinal that carries a submission, ASCENDING and NUMERIC.

    THE FILTER IS NOT DECORATION. `ls-remote`'s `*` crosses `/`, so the pattern cannot narrow a
    listing on depth and anything that ever lands under a deeper path would arrive here too. What
    keeps this honest is that a ref whose last segment is not a number yields `None` and is dropped.
    """
    found = store.list(refs.submission_glob())
    return tuple(sorted(o for ref in found if (o := refs.submission_ordinal(ref)) is not None))


def create(
    store: GitRefStore,
    *,
    participant: str,
    base: str,
    head: str,
    intent: str,
    declared_paths: tuple[str, ...] = (),
    attempts: int = CREATE_ATTEMPTS,
) -> Submission:
    """Allocate the next free ordinal and publish there. Returns what landed.

    THE ALLOCATION IS BY COLLISION, NOT BY AGREEMENT. It reads the highest ordinal to make a
    SENSIBLE FIRST GUESS and then relies entirely on :func:`publish` being refused if the guess was
    stale. That ordering matters: the read is an optimisation that may be wrong, the push is the
    arbitration that cannot be. Reversing them -- trusting the read and forcing the write -- is how
    a submission is accepted and then silently erased by a peer who read the same number.

    Raises:
        OrdinalTaken: `attempts` consecutive ordinals were all refused. See `CREATE_ATTEMPTS` for
            why the loop is bounded rather than patient.
    """
    ordinal = (submitted_ordinals(store) or (0,))[-1] + 1
    for _ in range(max(attempts, 1)):
        candidate_submission = Submission(
            ordinal=ordinal,
            participant=participant,
            base=base,
            head=head,
            intent=intent,
            declared_paths=declared_paths,
        )
        try:
            publish(store, candidate_submission)
        except OrdinalTaken:
            ordinal += 1
            continue
        return candidate_submission
    msg = f'{attempts} consecutive ordinals from {ordinal - attempts} were refused; nothing was published'
    raise OrdinalTaken(msg)
