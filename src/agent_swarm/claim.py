"""THE arbitration. One implementation, N slots, every contended resource in this package.

There is exactly one way to take something in this system, and it is here. `ForgeStore` takes a job
with `slots=1`; `seats.SeatPool` takes a licence seat with `slots=N`. Neither owns a copy.

**THE DUPLICATION IS THE DEFECT; A DRIFTED COPY IS ONLY ITS SYMPTOM.** This module exists because
the seat pool was first written as a second implementation of the store's claim -- same post, same
one read, same withdrawal, same lease, retyped. Nothing was wrong with it on the day it was written,
which is exactly the problem: two copies of an arbitration are two places a fix has to land, and the
one that gets missed is the one nobody is looking at. The store's own history says so twice over --
`ci.py candidate` and `ci_tick` each derived the run kinds from a branch name, and the drift was
found only when `--heavy` printed work nobody would ever do.

THE PROTOCOL: A SERVER-ASSIGNED MONOTONIC ORDERING KEY, AND THE LOWEST N LIVE IDS WIN
=====================================================================================

    1. POST a comment `CLAIM <expiry> <owner>` on the contended item.
    2. GET the comment list, ONCE. Sort the unexpired claims by comment id.
    3. Inside the first N -> held. Outside -> DELETE your own comment and return None.

**THIS IS NOT A COMPARE-AND-SWAP, and calling it one would be the lie.** It is post-then-arbitrate,
the same SHAPE as motronics' `ci_tick.claim()` that `store.py` was written to condemn. What makes it
sound is not the shape but **who chooses the ordering key**:

* `ci_tick`'s key is `<epoch>-<runner>`, chosen by the CLIENT. A racer arriving LATER can carry a
  LOWER key and dethrone a runner that has already started. Correctness then requires every runner
  to observe the full set at resolve time -- a window that is narrowed by hope, never closed.
* This key is the comment id, assigned by the SERVER at insert, monotonically. Anything created
  after your comment necessarily sorts HIGHER, and a lower id was inserted earlier and is already
  committed.

**WHY THE GENERALISATION FROM 1 TO N IS FREE, which is the one step worth checking rather than
assuming: YOUR RANK AMONG THE LIVE CLAIMS CAN ONLY FALL.**

* it cannot rise by an ARRIVAL, because every later comment has a higher id;
* it cannot rise by an EXPIRY, because an expired claim leaves the live set and never returns;
* it falls when a lower-id holder releases or lapses, which only makes a held slot safer.

So a racer that reads itself inside the first N cannot be displaced by anything the future does, and
a racer that reads itself outside them was beaten by claims already committed when it posted. At
most N holders, decided by one read each, with no coordinator anywhere. At `slots=1` this is exactly
the rule the store has always used, so the unification changed no behaviour there -- it deleted a
copy.

MEASURED, not reasoned: Gitea 1.26.4, 16 threads released from a `threading.Barrier` onto one fresh
issue, FOUR independent rounds, exactly one winner each (`runner-03`, `r14`, `r02`, `r15`). Ids came
back monotonic, unique and gapless (161->176 for 16 posts). Median claim latency ~280 ms under
16-way contention, against 2510 ms for the create-only ref push this replaces. Four rounds and not
one, because a single round electing a single winner is what a BROKEN protocol also does most of the
time. **THE N>1 CASE HAS NOT BEEN MEASURED AGAINST A LIVE SERVER** -- the argument above says it
needs nothing the N=1 case did not, and that is an argument, not a measurement. Said here rather
than left for a reader to assume the numbers cover both.

NO REFS. Issues and their comments are the whole storage layer (user directive 2026-08-09:
彻底废弃 ref, 后面基于 issue 和 project 迭代). A create-only ref push is also a genuine CAS -- measured,
8 racers, exactly 1 winner -- and it is 2510 ms against ~280 ms and built on a primitive being
retired outright.

THE PRECONDITION, WHICH IS A PROPERTY OF THE DEPLOYMENT AND NOT OF THE PROTOCOL
==============================================================================

Soundness rests on **read-after-write consistency**: a comment with a lower id must be visible to
any reader that posts after it. That holds on a single-node Gitea backed by one database, which is
what was measured. Put the forge behind a read replica and a racer can read a stale list, count too
few live claims, and admit an (N+1)th holder. **The lease is the mitigation, not the fix** -- it
bounds how long a duplicated hold persists, it does not prevent one. A reader that cannot see its
OWN write is the extreme of that, and it is not tolerated quietly: :class:`ArbitrationUnsound`.

CAS *AND* LEASE *AND* OWNER-CHECKED RELEASE *AND* A HEARTBEAT -- ALL FOUR
=========================================================================

`ci_tick.claim` HAD a compare-and-swap and gave it up to gain an expiry, so a laptop that claimed a
job and shut its lid parked that job for the whole lease. That trade was never necessary: the expiry
is a field in the comment, the CAS is the comment id, the owner check is a string comparison, and
the heartbeat is an in-place edit that keeps the id and therefore the rank. An implementation
offering three of the four is offering a choice that does not exist.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from agent_swarm.forge import CommentGone, Forge

#: The first word of a claim comment. Human-readable on purpose: the forge is also the UI, and an
#: operator scrolling an issue should be able to see who holds it and until when without a tool.
CLAIM_MARKER = 'CLAIM'


class LeaseLost(RuntimeError):
    """The lease this process believed it held is gone, and the work must STOP.

    ONE TYPE FOR ONE CONCEPT, raised by every heartbeat in this package -- a job claim and a fleet
    seat are different resources and the same failure: something else may already be doing what this
    process is doing.

    **RAISED, NEVER RETURNED, AND NEVER LOGGED INSTEAD.** A heartbeat that failed and returned a
    success value would be the forbidden shape this project names outright -- a warning on an
    unchanged success return, with the CALLER unable to tell. The whole reason a heartbeat exists is
    to be the one signal that arrives while the work is still running, so it must be impossible to
    miss at the call site.
    """


class ArbitrationUnsound(RuntimeError):
    """A just-posted claim was not in the list this process then read.

    THE DEPLOYMENT'S PRECONDITION HAS FAILED, not our arbitration. Read-after-write consistency is
    what makes one read conclusive, and a reader that cannot see its OWN write has none of it -- the
    `created-blind` shape measured on GitHub, where a re-read missed the reader's own issue 24 of 24
    times.

    LOUD, BECAUSE THE QUIET ALTERNATIVE IS INDISTINGUISHABLE FROM CONTENTION. Returning "you did not
    get it" here would make a broken backend look like a busy queue: every attempt refused, every
    holder absent, and an operator chasing a resource that is perfectly healthy. The store used to
    do exactly that -- its arbitration returned False when it could not find its own comment -- and
    the whole failure class was therefore invisible on the one deployment that could produce it.
    """


@dataclass(frozen=True, slots=True)
class Claim:
    """A parsed claim comment. `comment_id` is the ordering key and is the server's, never ours."""

    owner: str
    expires_at: float
    comment_id: int = -1

    def is_expired(self, *, now: float) -> bool:
        """Has the lease run out? The boundary instant is still HELD, not free."""
        return now > self.expires_at


def encode_claim(*, owner: str, expires_at: float) -> str:
    """`CLAIM <expiry> <owner>`.

    The expiry comes FIRST so that the owner can be the whole remainder of the line: an owner with
    a space in it would otherwise be silently truncated to its first word, and two machines would
    then share one identity -- a release by either freeing the other's claim.
    """
    return f'{CLAIM_MARKER} {expires_at:.3f} {owner}'


def decode_claim(body: str, *, comment_id: int = -1) -> Claim | None:
    """Parse a claim comment. ``None`` if `body` is not a claim comment at all.

    Raises:
        ValueError: `body` announces itself as a claim and then cannot be read as one. THE
            DISTINCTION MATTERS MORE THAN IT LOOKS: "not a claim" and "an unreadable claim" would
            both be skipped if this returned ``None`` for each, and skipping a live claim hands a
            running job to a second runner. A verdict comment is the first case; a truncated or
            future-format claim is the second, and it must stop the caller.
    """
    if not body.startswith(f'{CLAIM_MARKER} '):
        return None
    parts = body.split(maxsplit=2)
    if len(parts) != 3:
        msg = f'unreadable claim comment: {body[:80]!r}'
        raise ValueError(msg)
    try:
        expires_at = float(parts[1])
    except ValueError as exc:
        msg = f'unreadable claim comment expiry: {body[:80]!r}'
        raise ValueError(msg) from exc
    return Claim(owner=parts[2], expires_at=expires_at, comment_id=comment_id)


@dataclass(frozen=True, slots=True)
class Holders:
    """Who is holding, as far as ONE list read can tell. **Truthiness is deliberately undefined.**

    THE BAN IS `Claimable`'s AND THE REASON IS SHARPER HERE. A stale list read UNDER-counts holders,
    so any number derived from it OVER-counts free capacity -- and that is the dangerous direction:
    an operator who reads "2 free" starts two runs against a licence with none, and the failure
    lands in the middle of somebody's job.

    **SO THERE IS NO `free` AND THERE WILL NOT BE.** Not an omission: a free-slot count is a number
    that cannot be right, and the only sound way to learn a slot is free is to :meth:`Arbiter.take`
    one -- which posts, reads once, and is decided by the server's ordering key rather than by
    anybody's arithmetic.

    `claims` IS TRUNCATED TO `slots`, which is the definition of holding rather than a tidy-up: a
    live comment ranked beyond the slot count was never admitted, so reporting it as a holder would
    over-count occupancy from the debris of a crashed loser.
    """

    claims: tuple[Claim, ...]
    slots: int

    def __bool__(self) -> bool:
        msg = (
            'Holders has no truth value: a list read UNDER-counts holders, so anything derived from '
            'it OVER-counts free capacity -- the direction that over-admits. Write `.claims` and '
            'say which question you are asking; to learn whether a slot is free, take one.'
        )
        raise TypeError(msg)

    def __iter__(self):
        return iter(self.claims)

    def __len__(self) -> int:
        return len(self.claims)

    @property
    def occupied(self) -> int:
        """A LOWER BOUND on slots in use. Never the exact figure; see the class docstring."""
        return len(self.claims)

    def by(self, owner: str) -> Claim | None:
        """This owner's admitted claim, or ``None``. The LOWEST if it somehow holds two."""
        return next((claim for claim in self.claims if claim.owner == owner), None)


@dataclass(slots=True)
class Held:
    """One admitted hold: the receipt, and the handle for keeping it.

    NOT A GUARANTEE. It is true only while the lease is beaten, and every method on it can raise
    `LeaseLost` -- which is the honest model, because a hold owned by a machine that stopped beating
    IS lost, whatever an object in its memory says.

    MUTABLE IN EXACTLY ONE FIELD (`expires_at`), because a heartbeat that returned a NEW object
    would let a caller keep beating the old one forever: two objects, one comment, and the stale one
    still answering `is_live()` with a lie.

    RECONSTRUCTIBLE FROM THE SERVER, and that is why it is a plain value rather than a handle a
    caller must hold onto. `ForgeStore` never keeps one: it re-reads the holder and rebuilds this
    from what the comment says, which is the same record recovered rather than remembered.
    """

    arbiter: Arbiter
    owner: str
    comment_id: int
    expires_at: float

    def needs_beat(self, *, now: float | None = None) -> bool:
        """Whether a caller's loop should beat NOW. The cadence, as code rather than as advice.

        THIS EXISTS BECAUSE THE CONSTANT DID NOT WORK. The cadence used to be a module constant in
        the seat layer, documented as "the number a caller's loop should use" -- advice nothing
        consulted, in a package whose own rules name that as the dominant defect. Worse, it was a
        SECOND spelling: the job claim had no cadence at all, so two callers of one heartbeat had
        one number between them and no reason to agree.

        A QUARTER OF THE LEASE, so three consecutive beats may be lost -- to a 5xx, a retry storm, a
        GC pause -- before the fleet concludes the holder is dead. Derived from the lease rather than
        set beside it, because the two are one decision: a cadence that does not shrink when the
        lease does is how a "short lease" quietly stops being beaten in time.
        """
        return (time.time() if now is None else now) >= self.expires_at - self.arbiter.beat_every

    def is_live(self, *, now: float | None = None) -> bool:
        """Whether this hold has not yet lapsed, BY OUR OWN CLOCK.

        A LOCAL ANSWER AND IT SAYS SO. It is the expiry this process wrote, so it is honest about
        time and says nothing about whether the comment still exists -- only `renew` can tell you
        that, because only a write finds out. A caller that treats this as "I still hold it" has
        substituted the cheap question for the real one.
        """
        return (time.time() if now is None else now) <= self.expires_at

    def renew(self) -> float:
        """Extend this hold in place. Returns the new expiry. Raises :class:`LeaseLost`."""
        return self.arbiter.renew(self)

    def release(self) -> None:
        """Give it back. Owner-checked, idempotent, and a no-op if it was already lost."""
        self.arbiter.release(owner=self.owner, comment_id=self.comment_id)


class Arbiter:
    """N concurrent holders of one contended item, decided by the server's comment ordering.

    Args:
        forge: storage. Dull CRUD only -- no vendor name appears in this module.
        item_number: the work item the claims live on. REQUIRED, and never resolved lazily: a lazy
            resolve is the branch where a racer that cannot see the item creates one, and sixteen
            racers on sixteen items each arbitrate a full N slots on their own.
        slots: how many may hold at once. MUST BE POSITIVE -- zero refuses every attempt, which
            reads as a fully-booked resource rather than as a bug, so it is refused at construction.
        lease_seconds: how long a hold survives without a beat. MUST BE POSITIVE, for the same
            reason: a zero lease expires the claim being made, so every attempt refuses.

    Construction performs NO I/O.
    """

    def __init__(self, forge: Forge, *, item_number: int, slots: int = 1, lease_seconds: float) -> None:
        if slots <= 0:
            msg = (
                f'slots must be positive, got {slots!r}. A non-positive count refuses every attempt, '
                f'which is indistinguishable from a fully-booked resource.'
            )
            raise ValueError(msg)
        if lease_seconds <= 0:
            msg = f'lease_seconds must be positive, got {lease_seconds!r}'
            raise ValueError(msg)
        self.forge = forge
        self.item_number = item_number
        self.slots = slots
        self.lease_seconds = lease_seconds
        #: How often a holder should beat. Derived, never configured separately -- see
        #: `Held.needs_beat` for why the two are one decision rather than two numbers.
        self.beat_every = lease_seconds / 4

    def take(self, *, owner: str) -> Held | None:
        """Post, read once, and hold if we are inside the first N. ``None`` if we are not.

        **``None`` MEANS "NOT NOW", NEVER "THERE IS NOTHING TO TAKE".** The two are distinguishable
        at the call site because the second one RAISES -- a backend that cannot read its own write
        is `ArbitrationUnsound` -- so a ``None`` is always the ordinary, retryable answer and no
        caller has to guess which situation it is in.

        ARBITRATED BY COMMENT ID, NOT BY OWNER, which is what refuses a re-take by a current holder:
        the store contract requires that, because a runner that lost track of its own claim must not
        reset the lease and keep a hung job locked forever. Beating is `renew`, and it is a
        different call for that reason.

        Raises:
            ArbitrationUnsound: our own just-posted comment was not in the list we read.
        """
        expires_at = time.time() + self.lease_seconds
        mine = self.forge.add_comment(self.item_number, encode_claim(owner=owner, expires_at=expires_at))

        live = self._live(now=time.time())
        rank = next((i for i, claim in enumerate(live) if claim.comment_id == mine), None)
        if rank is None:
            # WE WROTE IT AND CANNOT READ IT. Withdraw first -- a comment left behind is a hold that
            # ACTIVATES LATER, see below -- and only then raise.
            self.forge.delete_comment(self.item_number, mine)
            msg = (
                f'claim comment {mine} on item {self.item_number} was posted and is not in the list '
                f'this process then read. The deployment does not offer read-after-write '
                f'consistency, so no arbitration on it can be trusted.'
            )
            raise ArbitrationUnsound(msg)
        if rank < self.slots:
            return Held(arbiter=self, owner=owner, comment_id=mine, expires_at=expires_at)
        # WITHDRAWING IS NOT TIDINESS. A refused claim comment left behind ACTIVATES LATER: once the
        # holders above it release, it becomes one of the lowest N live comments, and a racer that
        # was told "no" reads itself as the owner of something it never started. On a licence that
        # is worse still than on a job -- a leaked seat is capacity nothing downstream ever notices.
        self.forge.delete_comment(self.item_number, mine)
        return None

    def renew(self, held: Held) -> float:
        """Beat the heart of an existing hold, IN PLACE. Returns the new expiry.

        **IN PLACE, KEEPING THE COMMENT ID, WHICH IS THE WHOLE REASON THIS IS NOT "RELEASE AND
        RE-TAKE".** The id is the ordering key, so keeping it is keeping our rank: a re-take posts a
        NEW comment with a HIGHER id and, in the interval between the delete and the post, the thing
        is genuinely free -- a heartbeat that periodically releases what it is protecting. On a
        contended pool it would also walk its own holder to the back of the queue and eventually
        beat itself out.

        Raises:
            LeaseLost: the hold is gone -- lapsed and taken, pruned, or another arbiter's.
        """
        if held.arbiter is not self:
            msg = f'that hold belongs to item {held.arbiter.item_number}, not to item {self.item_number}'
            raise ValueError(msg)
        now = time.time()
        if not held.is_live(now=now):
            # REFUSED BEFORE THE WRITE, and this is the case that would otherwise over-admit. Once
            # our expiry passes, our comment stops counting as live and somebody below us may have
            # been admitted in our place. Editing it back to a future expiry would resurrect a
            # holder the fleet has already replaced -- N+1 holders, produced by the very mechanism
            # meant to keep the count right.
            msg = (
                f'{held.owner!r} let its hold on item {self.item_number} lapse at '
                f'{held.expires_at:.3f} (now {now:.3f}); another holder may have taken it. Stop.'
            )
            raise LeaseLost(msg)
        expires_at = now + self.lease_seconds
        try:
            self.forge.update_comment(
                self.item_number,
                held.comment_id,
                encode_claim(owner=held.owner, expires_at=expires_at),
            )
        except CommentGone as exc:
            msg = (
                f"{held.owner!r}'s claim comment {held.comment_id} on item {self.item_number} has "
                f'been pruned; it no longer holds.'
            )
            raise LeaseLost(msg) from exc
        held.expires_at = expires_at
        return expires_at

    def release(self, *, owner: str, comment_id: int) -> None:
        """Give back the hold recorded by `comment_id`, IF `owner` still holds it.

        OWNER-CHECKED, which reads as belt-and-braces because the caller supplies an id it could
        only have got by holding -- and is not, for the reason `InMemoryStore.release` is checked: a
        stranger freeing a live hold is the duplicate-execution failure walking back in through the
        door marked cleanup. A retry loop keeping a stale `Held` across a lapse-and-retake is
        exactly how that call gets made by accident.

        BOTH THE OWNER AND THE ID, never one of them. By id alone, the stale-`Held` retry above
        frees the new holder's comment. By owner alone, a caller holding two of N slots cannot say
        which one it is giving back, and would give back both.

        A NO-OP when the hold is not ours, never a steal and never an exception: releasing something
        already lost is the normal shape of a `finally` block, and raising there would replace the
        real error with this one.
        """
        for comment in self.forge.comments(self.item_number):
            if comment.id != comment_id:
                continue
            claim = decode_claim(comment.body, comment_id=comment.id)
            if claim is not None and claim.owner == owner:
                self.forge.delete_comment(self.item_number, comment_id)
            return

    def holders(self, *, now: float | None = None) -> Holders:
        """Who holds, as far as one read can tell. Read :class:`Holders` before deriving anything.

        FOR A STATUS DISPLAY AND FOR AN OWNER LOOKUP, NOT FOR A DECISION ABOUT FREE CAPACITY. The
        decision is `take`, which is the only operation whose answer the server arbitrates rather
        than this process computing it.
        """
        live = self._live(now=time.time() if now is None else now)
        return Holders(claims=tuple(live[: self.slots]), slots=self.slots)

    def _live(self, *, now: float) -> list[Claim]:
        """Unexpired claims, LOWEST COMMENT ID FIRST -- the rank the protocol is decided on.

        EXPIRED CLAIMS ARE SKIPPED, NOT DELETED, and that is what makes reclaiming a dead holder
        need no second code path: the corpse stops counting, the next racer's rank falls inside N,
        and the ordinary arbitration admits it. A dedicated takeover path would be a second way to
        win, and a second way to win is where a count of N quietly becomes N+1.
        """
        live = [
            claim
            for claim in (decode_claim(c.body, comment_id=c.id) for c in self.forge.comments(self.item_number))
            if claim is not None and not claim.is_expired(now=now)
        ]
        live.sort(key=lambda claim: claim.comment_id)
        return live
