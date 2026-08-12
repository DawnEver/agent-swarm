"""WHO IS ALIVE, WHAT THEY CAN DO, and the ordering rules that keep both answers honest.

WHAT THIS IS. The liveness half of motronics' `scripts/ci/ci_tick.py` -- `heartbeat`,
`last_heartbeat`, `publish_capabilities`, `live_runners`, `fleet_capabilities` -- written against
:class:`agent_swarm.refstore.RefStore` instead of against a module-level git helper. That seam is
what made the move possible at all; before it, every one of these was a decision welded to its
transport, and the decision could not be tested without a remote.

Every property here was written in answer to a measured incident, and each of the three below is
SILENT in production:

**THE PRUNE HAPPENS AFTER THE WRITE, AND ONLY IF THE WRITE SUCCEEDED.** :func:`beat` deletes every
stamp that is not the one it just wrote, so running that after a FAILED write deletes the last good
stamp and empties the namespace. The runner then reads as dead to the whole fleet, and every
scheduled tier is reported as unservable by a box that can serve all of them. Observed live.

**NEWEST-WINS PER RUNNER.** The layout keeps several epochs under one runner, so a rule of "every
beat is recent" would call a healthy runner dead the moment it had any history. Any fresh beat is
proof of life; the older ones are just its log.

**LIVENESS GATES THE CAPABILITY UNION.** A machine decommissioned last year would otherwise go on
"covering" a tier forever -- a coverage claim with no machine behind it. The heartbeat is reused
rather than a second liveness signal added, because two signals are free to disagree about who is
alive, and then the fleet has two answers to its most load-bearing question.

AND THE ASYMMETRY BETWEEN THE TWO READERS, which is not an inconsistency though it looks like one:
:func:`live_runners` PROPAGATES `RefUnreachable` -- "I could not ask" is not "nobody is alive", and
a caller reporting the fleet must be able to tell them apart. :func:`fleet_capabilities` CATCHES it
and answers with an empty union, because for a scheduler an unconfirmable fleet and an incapable
one lead to the same correct action: serve nothing this tick and retry. Each reader gets the answer
its decision needs, and neither guesses on behalf of the other.
"""

from __future__ import annotations

from collections.abc import Iterable

from agent_swarm import refs
from agent_swarm.refstore import RefStore, RefUnreachable

#: A runner whose newest beat is older than this is not counted as part of the fleet. Ticks are
#: seconds to a minute apart, so an hour is many missed ticks -- far past "briefly busy", well short
#: of punishing a machine that was rebooted. Every function here takes it as an argument; this is
#: the value a caller gets by not choosing, and it is a TIME rather than a project fact.
DEFAULT_STALE_AFTER_S = 3600.0

#: How many times :func:`beat` retries its write before giving up and keeping the previous stamp.
#: A SMALL CEILING RATHER THAN PERSISTENCE: a heartbeat that blocks a tick is worse than one that
#: misses a beat, and the caller is going to run again in under a minute anyway.
BEAT_TRIES = 3


class BeatFailed(RuntimeError):
    """The stamp could not be published, and the previous one was therefore KEPT.

    CARRIES THE TRANSPORT'S OWN WORDS. The version of this that named only the consequence produced
    two occurrences and no way to act on either: a runner that cannot beat is invisible to the
    fleet, and "it failed" does not distinguish an expired token from a network partition from a
    branch protection rule.
    """


def beat(store: RefStore, runner: str, now: int, *, tries: int = BEAT_TRIES) -> str:
    """Publish `runner`'s liveness at `now`, then collect its older stamps. Returns the ref written.

    WRITTEN BEFORE ANY WORK IS CHOSEN, by the caller's contract: a heartbeat emitted only when a job
    runs cannot tell an idle queue from a dead runner, and "quiet" then reads as "healthy".

    THE PRUNE LISTS THE REMOTE rather than assuming what the previous stamp was. A tick that crashed
    after writing would otherwise leave its stamp forever -- and the stalest one is exactly the one
    a reader must not see.

    Raises:
        BeatFailed: every attempt to write failed. The previous stamp is left in place, which is the
            whole point: an empty namespace is worse than a stale one, because stale still says
            "this box existed" while empty says "no such runner".
    """
    ref = refs.heartbeat_ref(runner, now)
    commit = store.head()
    ok, why = False, ''
    for _attempt in range(tries):
        ok, why = store.write(ref, commit)
        if ok:
            break
    if not ok:
        msg = (
            f'heartbeat write failed for {runner} after {tries} tr(ies) -- keeping the previous '
            f'stamp. Until this succeeds the fleet cannot tell this runner from a dead one. '
            f'The transport said: {why.strip() or "(nothing on stderr)"}'
        )
        raise BeatFailed(msg)

    for stale in store.list(refs.heartbeat_glob(runner)):
        if stale != ref:
            store.delete(stale)
    return ref


def last_beat(store: RefStore, runner: str) -> int | None:
    """Epoch seconds of `runner`'s most recent stamp, or `None` if it has never beaten.

    One listing, no fetch. This is what makes "that executor is dead" a QUESTION A READER CAN
    ANSWER rather than an inference from silence.
    """
    stamps = [s for ref in store.list(refs.heartbeat_glob(runner)) if (s := refs.heartbeat_stamp(ref)) is not None]
    return max(stamps) if stamps else None


def live_runners(store: RefStore, now: float, stale_after_s: float = DEFAULT_STALE_AFTER_S) -> set[str]:
    """Every runner whose NEWEST beat is still fresh -- "who will pick up work".

    ONE PREDICATE, EVERY CALLER. This was once inline inside the capability union, which meant no
    other caller could ask the question and a status report could only describe the local loop. A
    second copy of the parse is the duplicated-scheme defect in its most dangerous form: the status
    command would be free to name a runner the capability union had already written off.

    JUNK UNDER THE NAMESPACE IS SKIPPED, NOT FATAL. This feeds operator-facing reports, and a
    traceback here would hide the very summary a reader came for.

    Raises:
        RefUnreachable: propagated deliberately. See this module's docstring for why this reader
            and the capability union answer an unreachable remote differently.
    """
    newest: dict[str, int] = {}
    for ref in store.list(refs.heartbeat_glob()):
        runner, epoch = refs.runner_of(ref), refs.heartbeat_stamp(ref)
        if runner is None or epoch is None:
            continue
        newest[runner] = max(newest.get(runner, epoch), epoch)
    return {runner for runner, epoch in newest.items() if now - epoch < stale_after_s}


def publish_capabilities(store: RefStore, runner: str, capabilities: Iterable[str]) -> set[str]:
    """Advertise what this machine can serve, ONE REF PER CAPABILITY. Returns what is now published.

    NOT A PAYLOAD, for the same reason the heartbeat puts its epoch in a ref name: a single prefix
    listing then answers "what can the fleet do?" with no fetch and no object this machine has never
    seen. The namespace is bounded by runners x capabilities, which is a handful.

    REPUBLISHED AND PRUNED EVERY TICK, so uninstalling a tool stops advertising it rather than
    leaving a claim the machine can no longer honour. That is the direction that matters: a stale
    capability does not fail loudly, it wins a job the box cannot run.
    """
    wanted = {refs.capability_ref(runner, cap) for cap in capabilities}
    commit = store.head()
    for ref in sorted(wanted):
        store.write(ref, commit)
    for ref in store.list(refs.capability_glob(runner)):
        if ref not in wanted:
            store.delete(ref)
    return wanted


def fleet_capabilities(store: RefStore, now: float, stale_after_s: float = DEFAULT_STALE_AFTER_S) -> set[str]:
    """The union of capabilities across runners that are still BEATING.

    AN UNREACHABLE CONTROL PLANE IS AN EMPTY UNION HERE, and that is a decision rather than a
    swallowed error: for the scheduler that consumes this, "nobody can be confirmed alive" and
    "nobody can do it" lead to the same correct action -- serve nothing, retry next tick. Callers
    that need to tell the two apart ask :func:`live_runners`, which raises.
    """
    try:
        live = live_runners(store, now, stale_after_s)
        published = store.list(refs.capability_glob())
    except RefUnreachable:
        return set()
    found: set[str] = set()
    for ref in published:
        if refs.runner_of(ref) in live and (cap := refs.capability_of(ref)) is not None:
            found.add(cap)
    return found
