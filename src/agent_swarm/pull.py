"""The PULL surface: how an executor that cannot be commanded takes work anyway.

THE EXECUTOR IS A KIND, NOT A LAYER
===================================

fabric can be COMMANDED -- open a session, send a turn, read the result. A human at a keyboard and a
TUI agent someone is already talking to cannot be: nothing may reach into a person's terminal and
start a job there. They can only be INFORMED. So the work boundary for those executors is PULL, and
that is a property of the executor's KIND rather than a second architectural layer with its own
queue.

**NO NEW PROTOCOL IS NEEDED, AND THAT IS THE FINDING RATHER THAN THE SHORTCUT.** The reason a pull
surface did not exist is not that the primitives were missing; it is that only ONE kind of executor
had ever been built -- the gate -- so the store's list/claim/verdict calls had exactly one caller and
looked like part of it. Three verbs over the primitives the CI runner already uses:

    available   what THIS box may take: capabilities x `requires`, minus what is claimed
    take        the SAME compare-and-swap and the SAME lease, so a human and a runner cannot
                both take one item
    report      the SAME verdict vocabulary -- gate.py's three words, no fourth

If `take` were a different claim, the whole property would be gone: two mechanisms claiming one
item is two mechanisms that do not see each other, which is duplicate execution with extra steps.
So this module implements NO claiming of its own. It calls `ForgeStore.try_claim`, and a test
tokenises it to keep it that way.

IDENTITY IS ALREADY SOLVED, so nothing here invents one: `swarmctl consume` puts a credential on a
machine, and a TUI agent is another `Role.RUNNER` separated by the runner-id salt. What a caller
supplies is an `owner` string, exactly as the CI runner does.

A HEARTBEAT IS REQUIRED, NOT OPTIONAL, AND THE REASON IS THE HUMAN
==================================================================

The failure this surface would otherwise introduce is the one `ci_tick.claim` already produced once:
a holder that stops existing parks the work for the whole lease. With a human executor that is not
an edge case, it is Tuesday -- somebody takes an item, gets pulled into a meeting and closes the
terminal, and the item is unavailable for three hours with nothing anywhere saying why.
:meth:`Ticket.beat` is `ForgeStore.renew_claim`, so a taken item is reclaimable minutes after its
holder stops beating, and a holder that has lost its claim finds out by exception rather than by
discovering someone else's verdict on its work.

**A `Claimable` GOES IN AND A `Claimable` COMES OUT.** Filtering must not launder the distinction
its ban on truthiness protects: an empty result here means no work VISIBLE to a possibly-stale list
read, never that this box has nothing to do. Returning a plain list would have thrown that away at
precisely the surface where a human is the reader -- and a human reading "nothing to do" acts on it.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from agent_swarm.claim import LeaseLost
from agent_swarm.forge_store import Claimable, ForgeStore, Role
from agent_swarm.job import Job, JobKind
from agent_swarm.store import VERDICTS


class MissingCapability(RuntimeError):
    """This box was asked to take work whose `requires:` it does not declare.

    **RAISED RATHER THAN RETURNED AS "NO", because the two mean opposite things to the caller.**
    `take` returning ``None`` says "somebody beat you, try the next item" -- a retryable, ordinary
    outcome. A box taking work it cannot do is not retryable at all: retrying is the worst possible
    response, and the caller would do it forever while the item looked contended. Collapsing both
    into a ``None`` would make a misconfigured box indistinguishable from a busy one, which is the
    shape that gets diagnosed as "the queue is slow".

    It is also the honest signal for a HUMAN, who is the executor this surface exists for: "you do
    not have FEMM" is actionable and "no work for you" is not.
    """


@dataclass(frozen=True, slots=True)
class Ticket:
    """Proof that this owner holds this job, and the handle for keeping it.

    NOT A RECEIPT. It is only true while the lease is beaten, and every method on it can raise
    `LeaseLost` -- which is the honest model, because a claim held by a machine that stopped
    beating IS lost, whatever an object in its memory says.
    """

    workbench: Workbench
    job: Job
    owner: str

    def beat(self) -> float:
        """Extend the claim. Returns the new expiry.

        Raises:
            LeaseLost: the claim is gone. STOP: another executor may already have taken the work.
        """
        return self.workbench.store.renew_claim(self.job, owner=self.owner)

    def report(self, *, verdict: str, detail: str) -> None:
        """Record the outcome and give the item back. See :meth:`Workbench.report`."""
        self.workbench.report(self, verdict=verdict, detail=detail)

    def abandon(self) -> None:
        """Put the work back without answering it. Owner-checked by the store; never a steal.

        A FIRST-CLASS OUTCOME, not a failure path. A human who takes an item and finds it is not
        what they thought must be able to return it in one call -- otherwise the shortest correct
        path is to walk away, and walking away costs the fleet the whole lease.
        """
        self.workbench.store.release(self.job, owner=self.owner)


@dataclass(frozen=True, slots=True)
class Survey:
    """What one look at the queue actually saw. **Its truthiness is deliberately undefined.**

    WHY THE COUNTS TRAVEL WITH THE JOBS. A surface that returned only the jobs forces its caller to
    render "nothing to do" for four different situations: the queue is empty, this box lacks every
    capability, everything is already claimed, or the bound stopped us looking. Those need different
    words in front of a human, and a caller cannot invent the difference after the fact -- so the
    numbers come back from the layer that knows them.

    THE BAN IS `Claimable`'s, for the reason `Claimable` gives, and it is inherited rather than
    softened: an empty survey means nothing was VISIBLE, never that nothing exists.

    Attributes:
        offered: what this box may take, and the only thing safe to hand a `take`.
        visible: open items of this kind the listing returned. A LOWER BOUND -- a list read can be
            stale, so this is what was observed and never what exists.
        capable: how many of those passed the capability filter.
        examined: how many had their claim state read. Below `capable` exactly when `limit` bit,
            and that difference is what a caller must say out loud rather than round off.
        limit: the bound that was applied, or ``None`` for none.
    """

    offered: Claimable
    visible: int
    capable: int
    examined: int
    limit: int | None = None

    def __bool__(self) -> bool:
        msg = (
            'Survey has no truth value: an empty result means NO WORK VISIBLE, never no work '
            'exists, because a forge list read can be stale and a bound may have stopped the look '
            'early. Write `.offered.jobs` and say which question you are asking.'
        )
        raise TypeError(msg)

    @property
    def bound_bit(self) -> bool:
        """Whether the bound stopped us short of looking at everything this box could do.

        THE ONE FACT A CALLER MUST NOT ROUND OFF. When this is true, "no work available" means "none
        in the first K", and there may be work just past the edge of the screen.
        """
        return self.examined < self.capable


class Workbench:
    """One pull-mode executor's view of the queue: what it may take, taking it, and answering.

    Args:
        store: a RUNNER-role store. Required to be a runner, and checked -- a submitter-role store
            would let a pull executor CREATE work items, which is the concurrent-creation race the
            role split exists to make unreachable. A human at a keyboard is exactly the caller who
            would reach for it by hand.
        owner: who this is, in the same namespace the CI runner claims under. That shared namespace
            is what makes a human and a runner contend rather than coexist.
        capabilities: what this box HAS, as opaque tokens compared for equality against `requires:`.
            The caller declares them; nothing here knows what any of them mean.
    """

    def __init__(self, store: ForgeStore, *, owner: str, capabilities: Iterable[str] = ()) -> None:
        if store.role is not Role.RUNNER:
            msg = (
                f'a Workbench needs a {Role.RUNNER.value} store, got {store.role.value}. A pull '
                f'executor discovers and claims; creating work items is the submitter lifecycle, '
                f'and two creators is the race the roles exist to remove.'
            )
            raise ValueError(msg)
        if not owner:
            msg = 'a Workbench must name its owner; an unnamed holder cannot be released or reported on'
            raise ValueError(msg)
        self.store = store
        self.owner = owner
        self.capabilities = frozenset(capabilities)

    def available(self, kind: JobKind, *, limit: int | None = None) -> Survey:
        """Work of `kind` this box could actually do, in the store's own order.

        TWO FILTERS, AND THEY ARE NOT THE SAME KIND OF STATEMENT:

        * **capabilities.** A job whose `requires:` is not a subset of ours is not offered. Decidable
          from the listing labels `Claimable` already carries, so it costs nothing, and it is exact
          -- a missing capability does not become available later. IT RUNS FIRST, which is what
          makes work this box cannot do cost zero reads; the order is load-bearing and tested.
        * **already claimed.** ADVISORY, and it costs one comment read per candidate examined. A
          list read can be stale and a claim can be taken between this call and the next, so this
          removes work a person would waste a minute discovering; it decides nothing. `take` is the
          arbiter, and it refuses.

        **THE COST, MEASURED, AND THE MEASUREMENT IS WHY `limit` EXISTS.** Against `RecordingForge`,
        every item claimable and none claimed:

            open items      10      100      500     1000
            forge calls     11      101      501     1001     (1 list + 1 comment read per item)
            in-memory ms   0.1      0.4      1.8      3.8

        Exactly N+1, linear, no hidden constant. **The milliseconds are not the cost** -- they are a
        dict answering. The cost is N HTTP round trips, and this package's own live measurement puts
        the per-call floor at ~60 ms p50 (`Claimable.preferred`, measured 2026-08-10). PROJECTED
        from that floor, and labelled a projection because this has never been run against a real
        server: ~0.6 s at 10 items, ~6 s at 100, ~30 s at 500.

        **THE UNBOUNDED FORM'S JUSTIFICATION -- "the resource conserved is a person's attention" --
        HOLDS AT TEN ITEMS AND FAILS AT FIVE HUNDRED**, and the refutation is left standing rather
        than reworded: at 500 the filter spends thirty seconds of a person's attention to save them
        from occasionally being told "somebody got there first". It costs more of the thing it was
        defending than it saves, while the person watches a terminal do nothing.

        **`limit` IS THE REPAIR, AND IT IS A BOUND ON LOOKING, NEVER ON OFFERING.** The attention
        argument is about the jobs a person actually SEES, so the read is paid for those and not for
        the tail. It was left unimplemented until a CLI existed, because K cannot be invented -- it
        is a property of a screen, and `workbench_cli` derives it from the real terminal height.
        `None` keeps the unbounded behaviour, which is right for a caller with no screen.

        **WHAT `limit` DOES NOT DO, said because the obvious reading is wrong:** it does not hide
        work. Jobs past the bound are simply not claim-checked, so they may still be taken by key --
        and `Survey.bound_bit` says the look stopped early, so a caller can never render "no work
        available" when the truthful sentence is "none in the first K".

        **THE ORDER IS ROTATED BY OWNER FIRST (`Claimable.preferred`), AND THAT IS PART OF THE
        BOUND, NOT A GARNISH.** Without it every owner examines the same first K, races the same
        head, and K-1 of them do nothing while work sits visible past the edge of the screen -- the
        bound converting waste into starvation. This docstring used to NAME `preferred` as the
        pairing that prevents it while nothing in the package called it, which is the same defect as
        the heartbeat that named a cadence it recomputed. The code now does what the sentence says.

        **RETURNS A `Survey`.** Not a bare list, and not a bare `Claimable` either: four different
        situations produce zero offers and a human needs different words for each. See `Survey`.
        """
        if limit is not None and limit <= 0:
            # A zero bound examines nothing and offers nothing, which is indistinguishable at the
            # surface from an empty queue -- the exact confusion `Survey` exists to prevent, arriving
            # through a parameter rather than through a stale read.
            msg = f'limit must be positive or None, got {limit!r}; a zero bound offers nothing and says nothing'
            raise ValueError(msg)
        listed = self.store.claimable(kind)
        # ROTATED BY OWNER BEFORE ANYTHING IS BOUNDED, and this line is the repair for a defect the
        # bound INTRODUCED. `preferred` was measured live on 2026-08-10 to take completed jobs per
        # round from 1 to 7 at sixteen runners, purely by making different owners lead with
        # different items -- and NOTHING in this package called it. Unbounded, that was waste;
        # bounded, it is starvation: every owner would examine the same first K, race the same head,
        # and K-1 of them would do nothing while work sat visible past the edge of the screen.
        #
        # It is applied HERE rather than left to the caller because the caller cannot: the bound is
        # applied below, so by the time a caller sees the result the tail it would have rotated into
        # view is already gone.
        capable = [job for job in listed.preferred(self.owner) if listed.requirements_for(job) <= self.capabilities]
        considered = capable if limit is None else capable[:limit]

        jobs: list[Job] = []
        requires: dict[str, frozenset[str]] = {}
        for job in considered:
            if self.store.claim_owner(job) is not None:
                continue
            needed = listed.requirements_for(job)
            if needed:
                requires[job.claim_key()] = needed
            jobs.append(job)
        return Survey(
            offered=Claimable(jobs=tuple(jobs), requires=requires),
            visible=len(listed.jobs),
            capable=len(capable),
            examined=len(considered),
            limit=limit,
        )

    def take(self, job: Job) -> Ticket | None:
        """Claim `job` for this owner. ``None`` if somebody else got there first.

        **THE SAME COMPARE-AND-SWAP THE CI RUNNER USES**, and nothing else -- this method does not
        post a comment, invent a marker or keep a local record. A second claiming mechanism would be
        a second thing to be lowest on, and a human and a runner would each hold "the" claim.

        THE CAPABILITY CHECK IS RE-READ HERE, BY NUMBER, rather than trusted from `available`. That
        read is the one measured fresh on both forges, and the listing that fed `available` is the
        one measured stale -- so a job may have gained a requirement since it was offered, and this
        is the last moment anything can notice. It also covers the caller who never called
        `available` at all, which on a human-facing surface is not a hypothetical.

        Raises:
            MissingCapability: this box does not declare what the job requires. Not a ``None``; see
                that class for why the distinction is the whole point.
        """
        needed = self.store.requirements(job)
        if not needed <= self.capabilities:
            msg = (
                f'{self.owner!r} cannot take {job.claim_key()!r}: it requires '
                f'{sorted(needed - self.capabilities)}, which this box does not declare. '
                f'Declared here: {sorted(self.capabilities)}.'
            )
            raise MissingCapability(msg)
        if not self.store.try_claim(job, owner=self.owner):
            return None
        return Ticket(workbench=self, job=job, owner=self.owner)

    def report(self, ticket: Ticket, *, verdict: str, detail: str) -> None:
        """Answer the work in the ONE verdict vocabulary, then give the claim back.

        **THE CLAIM IS PROVEN BEFORE THE VERDICT IS WRITTEN.** A holder that stopped beating has
        lost the job, and by then another executor may have taken it, run it and answered it --
        writing our verdict over theirs would replace a live answer with a stale one, and neither
        would be marked. So a lost lease RAISES and nothing is written.

        THE ORDER IS `record_verdict` THEN `release`, and it is load-bearing. `record_verdict`
        closes the item, so after it the work is no longer claimable and the leftover claim comment
        can harm nobody; a release first would leave a window in which the item is open, unclaimed
        and unanswered, and a second executor would start a job whose verdict is one call away.

        Raises:
            LeaseLost: this owner no longer holds the job. Nothing has been written.
            ValueError: `verdict` is not one of gate.py's three words -- refused BEFORE the lease is
                checked, since a caller that invented a fourth state has a bug either way and the
                clearer error is the one about the word.
        """
        if verdict not in VERDICTS:
            msg = f'verdict must be one of {sorted(VERDICTS)}, got {verdict!r}'
            raise ValueError(msg)
        holder = self.store.claim_owner(ticket.job)
        if holder != ticket.owner:
            who = 'nobody' if holder is None else repr(holder)
            msg = (
                f'{ticket.owner!r} cannot report on {ticket.job.claim_key()!r}: it is held by '
                f'{who}. Nothing was written -- another executor may already have answered it.'
            )
            raise LeaseLost(msg)
        self.store.record_verdict(ticket.job, verdict=verdict, detail=detail)
        self.store.release(ticket.job, owner=ticket.owner)
