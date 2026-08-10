"""A :class:`~agent_swarm.store.Store` over any forge. All the logic; no vendor anywhere in it.

NO REFS. Issues and their comments are the whole storage layer (user directive 2026-08-09:
彻底废弃 ref, 后面基于 issue 和 project 迭代). The ref-push claim this file used to carry is
deleted, not deprecated.

THE CLAIM PROTOCOL: A SERVER-ASSIGNED MONOTONIC ORDERING KEY
============================================================

    1. POST a comment `CLAIM <expiry> <owner>` on the job's work item.
    2. GET the comment list. LOWEST LIVE COMMENT ID WINS.
    3. If that id is not yours, DELETE your comment and return False.

**THIS IS NOT A COMPARE-AND-SWAP, and calling it one would be the lie.** It is post-then-arbitrate,
the same SHAPE as motronics' `ci_tick.claim()` that `store.py` was written to condemn. What makes it
sound is not the shape but **who chooses the ordering key**:

* `ci_tick`'s key is `<epoch>-<runner>`, chosen by the CLIENT. A racer arriving LATER can carry a
  LOWER key and dethrone a runner that has already started. Correctness then requires every runner
  to observe the full set at resolve time -- a window that is narrowed by hope, never closed.
* This key is the comment id, assigned by the SERVER at insert, monotonically. Anything created
  after your comment necessarily sorts HIGHER, so a runner that reads the list and finds itself
  lowest **cannot be dethroned by any future arrival**; and a lower id, by definition, was inserted
  earlier and is already committed.

That converts "correct if everyone sees everything simultaneously" into "correct after one read".
`Store.try_claim`'s contract -- return False, never resolve a tie on the caller's behalf -- is
satisfied: a loser refuses at the call and does not compute a winner for anyone else.

MEASURED, not reasoned: Gitea 1.26.4, 16 threads released from a `threading.Barrier` onto one fresh
issue, FOUR independent rounds, exactly one winner each (`runner-03`, `r14`, `r02`, `r15`). Ids came
back monotonic, unique and gapless (161->176 for 16 posts). Median claim latency ~280 ms under
16-way contention, against 2510 ms for the create-only ref push this replaces. Four rounds and not
one, because a single round electing a single winner is what a BROKEN protocol also does most of
the time.

THE PRECONDITION, WHICH IS A PROPERTY OF THE DEPLOYMENT AND NOT OF THE PROTOCOL
==============================================================================

Soundness rests on **read-after-write consistency**: a comment with a lower id must be visible to
any reader that posts after it. That holds on a single-node Gitea backed by one database, which is
what was measured. Put the forge behind a read replica and a runner can read a stale list, find
itself lowest, and start work a second runner has already begun. **The lease is the mitigation, not
the fix** -- it bounds how long a duplicated claim persists, it does not prevent one. If that day
comes, `.claude/memory/2026/08/09/measurement-a-pure-issue-claim-protocol-that-holds-comment-id-
arbitration.md` is where to look, and `GITHUB_UNMEASURED` names the probe.

CREATION IS A RACE TOO
======================

The protocol above says "the job's work item" as though it exists. When it does not, sixteen runners
read an empty list, create sixteen items, and each arbitrates its claim on its own -- sixteen
winners, with the claim protocol working perfectly on each of sixteen wrong issues. Measured that
way against the real server before it was fixed. `_item_number` therefore applies the SAME rule one
level up: an issue number is server-assigned and monotonic too, so create, re-read, and take the
lowest-numbered item with this title.

THAT FIX HAS A MEASURED LIMIT AND IT IS NOT PORTABLE YET. It assumes the list read is fresh -- that
an item created moments ago is visible to the next reader. True on our Gitea. On GitHub the
`?labels=` filter was measured STALE for 4.0-6.6 s (20/20), the exact inverse of Gitea, and the
resulting rule is that NO "does not exist" conclusion may be drawn from a list query on either
forge. `_item_number` draws exactly that conclusion before it creates. Whether the plain issues list
is fresh on GitHub is the first entry in `forge.GITHUB_UNMEASURED`, and it is why no GitHub client
is written: if that list lags, two runners create two items and the sixteen-winner bug returns on
the second forge.

WHY A LOSER DELETES ITS OWN COMMENT
===================================

Not tidiness. A refused claim comment left behind is a claim that ACTIVATES LATER: once the holder
releases, the abandoned comment becomes the lowest live one, and the runner that was told False now
reads itself as the owner of a job it never started. `release` is owner-checked for the same reason
`InMemoryStore`'s is -- a stranger freeing a live claim is the duplicate-execution failure walking
back in through the door marked cleanup.

THE VERDICT HALF NEEDS NO ARBITRATION, so it is a comment carrying gate.py's output, one `verdict:*`
label, and the item closed. A verdict is written by the runner that already holds the claim; nobody
is racing it. Everything about it -- title format, label vocabulary, the rule that exactly one
verdict label may be attached -- is decided here, once, for every vendor.
"""

from __future__ import annotations

import enum
import time
from dataclasses import dataclass
from typing import ClassVar, Self

from agent_swarm.forge import Forge
from agent_swarm.item_index import IndexCorruptError, ItemIndex, NotIndexed
from agent_swarm.job import Job
from agent_swarm.store import VERDICTS

#: How long a claim stays valid without being released. Long enough for the longest blocking gate
#: (30 minutes, `AGENTS.md`) plus the slack a shared box costs; short enough that a dead machine
#: does not park a job for a working day.
DEFAULT_LEASE_SECONDS = 3 * 3600.0

#: The first word of a claim comment. Human-readable on purpose: the forge is also the UI, and an
#: operator scrolling an issue should be able to see who holds it and until when without a tool.
CLAIM_MARKER = 'CLAIM'

#: The label a verdict wears. Lower-case because labels are read by humans on a board; the VALUE is
#: always `store.VERDICTS`' upper-case word, and this mapping is the only translation between them.
#: It lives HERE, not in a forge: both vendors have labels, so the vocabulary is not a vendor's to
#: choose.
VERDICT_LABELS = {
    'PASS': 'verdict:pass',
    'FAIL': 'verdict:fail',
    'INCONCLUSIVE': 'verdict:inconclusive',
}
_LABEL_TO_VERDICT = {label: word for word, label in VERDICT_LABELS.items()}

_ITEM_TITLE_ROOT = '[swarm]'


class Role(enum.Enum):
    """Who this store is, and therefore what it is ALLOWED to do.

    A RUNNER MAY NOT CREATE A WORK ITEM. Concurrent creation is a race that no re-read closes on a
    forge whose list lags, so the only reliable fix is that exactly one writer creates -- and a rule
    that lives in a docstring is a rule the code never consults. This makes it structural: a
    runner-mode store RAISES rather than creating, so the eight-item race is not merely unlikely,
    it is unreachable from that role.

    THERE IS NO DEFAULT, DELIBERATELY. A defaulted role is a policy that is quietly opt-out, and a
    caller who never thought about creation would get the permissive half for free -- which is the
    exact shape that produced the bug this enum exists to prevent.
    """

    SUBMITTER = 'submitter'
    RUNNER = 'runner'


class NotVisible:
    """The answer a LIST query is actually able to give: "I cannot see it", never "it is absent".

    THIS TYPE EXISTS TO MAKE A BUG UNWRITABLE. `_item_number` used to return ``None`` for "no such
    work item", and every caller then did the only natural thing with a ``None`` -- created one. On
    Gitea that is safe by accident, because its plain list is read-after-write fresh. On GitHub the
    plain list was measured **22/22 stale**, recovering in 0.42-6.36 s, so "None" meant "created 200
    ms ago and not replicated yet" and the natural branch created a duplicate.

    A comment saying "do not treat this as absent" would have been prose the code never consults.
    A distinct type is a declaration the caller MUST handle, and `if number is None: create()`
    cannot be written against it.
    """

    _instance: ClassVar[NotVisible | None] = None

    def __new__(cls) -> Self:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return 'NOT_VISIBLE'

    def __bool__(self) -> bool:
        # FALSY ON PURPOSE, so `if not number:` is at least not silently wrong -- but note that a
        # visible item number can never be 0 (forges number from 1), so the falsy case is exactly
        # the unknown one.
        return False


#: "The list cannot see it." Never "it does not exist".
NOT_VISIBLE = NotVisible()

#: How long a freshly created work item may stay invisible to a LIST query on this backend.
#:
#: THE VALUE IS A VENDOR FACT AND LIVES WITH THE VENDOR -- see `forge.LIST_STALENESS_SECONDS`, which
#: carries the measurements. This module keeps only the neutral default, because a per-backend
#: number spelled here would put a vendor name in the one file that must not have one, and the test
#: that tokenises this module for vendor names is what caught the first attempt.
#:
#: It is a **probability improvement, never a guarantee**: the window belongs to the forge's
#: replication and the forge may change it tomorrow. NOT A KNOB TO TUNE UNTIL A TEST PASSES -- if a
#: value has to grow to make something go green, the thing to change is the design. See `register`.
DEFAULT_LIST_STALENESS_SECONDS = 0.0


# --------------------------------------------------------------------------------------------
# The claim comment -- pure, so it is testable without a network or a forge.
# --------------------------------------------------------------------------------------------


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


class ForgeStore:
    """A `Store` whose claims and verdicts are both work-item comments.

    VENDOR-AGNOSTIC BY CONSTRUCTION: the only vendor-shaped thing it holds is a `Forge`, and it asks
    that forge for nothing but dull CRUD. No vendor name appears in this module's CODE at all --
    only in the docstring above, which cites the measurements -- and `test_forge_store.py` proves
    that by tokenising the source rather than by asserting the prose.

    Args:
        namespace: isolates one swarm (or one test run) from another. It prefixes work-item titles,
            and `purge_namespace` cannot reach outside it.
        forge: the storage/UI backend. REQUIRED, and deliberately not defaulted -- a default is a
            choice of vendor, and this module is the one that must not make one. Callers take
            `forge.default_forge()`.
        lease_seconds: how long a claim survives without a release. MUST BE POSITIVE: a zero lease
            expires the claim being made, so every runner would refuse and the job would never run.

    Construction performs NO I/O -- not a connection, not a credential read.
    """

    def __init__(
        self,
        namespace: str,
        forge: Forge,
        *,
        role: Role,
        index: ItemIndex | None = None,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
        list_staleness_seconds: float = DEFAULT_LIST_STALENESS_SECONDS,
    ) -> None:
        self.role = role
        self.index = index
        self.list_staleness_seconds = list_staleness_seconds
        if lease_seconds <= 0:
            # Refused at construction rather than at the claim: a zero lease makes `try_claim`
            # return False forever, which reads as healthy contention rather than as a bug.
            msg = f'lease_seconds must be positive, got {lease_seconds!r}'
            raise ValueError(msg)
        self.namespace = namespace
        self.forge = forge
        self.lease_seconds = lease_seconds
        self._item_numbers: dict[str, int] = {}

    # -- claims ------------------------------------------------------------------------------

    def try_claim(self, job: Job, *, owner: str) -> bool:
        """Post a claim, then read the list once. Lowest live comment id wins."""
        number = self._item_number(job, create=True)
        assert not isinstance(number, NotVisible)
        mine = self.forge.add_comment(number, encode_claim(owner=owner, expires_at=time.time() + self.lease_seconds))

        holder = self._holder(number, now=time.time())
        if holder is not None and holder.comment_id == mine:
            return True
        # ARBITRATED BY COMMENT ID, NOT BY OWNER, which is what refuses a re-claim by the holder
        # itself: the contract requires that, because a runner that lost track of its own claim must
        # not reset the lease and keep a hung job locked forever.
        #
        # Withdrawing is not tidiness -- see the module docstring. An abandoned claim comment becomes
        # the lowest live one the moment the holder releases, and this runner would then read itself
        # as owning a job it was refused and never started.
        self.forge.delete_comment(number, mine)
        return False

    def claim_owner(self, job: Job) -> str | None:
        number = self._item_number(job)
        if isinstance(number, NotVisible):
            # An item we cannot see holds no claim WE can honour. This errs toward "unclaimed",
            # which risks a duplicate run; the lease bounds that, and the alternative -- reporting a
            # claim we cannot read -- would deadlock the job instead.
            return None
        holder = self._holder(number, now=time.time())
        return holder.owner if holder else None

    def release(self, job: Job, *, owner: str) -> None:
        """Release `job` if `owner` holds it. A non-owner's release is a no-op, never a steal."""
        number = self._item_number(job)
        if isinstance(number, NotVisible):
            return
        holder = self._holder(number, now=time.time())
        if holder is not None and holder.owner == owner:
            self.forge.delete_comment(number, holder.comment_id)

    def _holder(self, number: int, *, now: float) -> Claim | None:
        """The live claim with the lowest comment id, or ``None``.

        EXPIRED CLAIMS ARE SKIPPED RATHER THAN REMOVED, and that is what makes a takeover need no
        second code path: a dead machine's comment simply stops counting, the next racer's comment
        is the lowest LIVE one, and the ordinary arbitration elects it. A separate takeover path
        would be a second way to win, and a second way to win is where a protocol quietly acquires
        two winners.
        """
        live = [
            claim
            for claim in (decode_claim(c.body, comment_id=c.id) for c in self.forge.comments(number))
            if claim is not None and not claim.is_expired(now=now)
        ]
        return min(live, key=lambda claim: claim.comment_id) if live else None

    # -- verdicts ----------------------------------------------------------------------------

    def record_verdict(self, job: Job, *, verdict: str, detail: str) -> None:
        if verdict not in VERDICTS:
            # BEFORE any I/O: a store that validated after the comment was posted would leave one
            # behind for a verdict it then rejected.
            msg = f'verdict must be one of {sorted(VERDICTS)}, got {verdict!r}'
            raise ValueError(msg)

        number = self._item_number(job, create=True)
        assert not isinstance(number, NotVisible)
        self.forge.add_comment(number, f'**{verdict}**\n\n```\n{detail}\n```')

        # Exactly one verdict label at a time. A retry after INCONCLUSIVE that merely ADDED `pass`
        # would leave the job both inconclusive and green, and nothing downstream can act on that.
        for existing in self.forge.labels(number):
            if existing in _LABEL_TO_VERDICT:
                self.forge.remove_label(number, existing)
        self.forge.add_label(number, VERDICT_LABELS[verdict])
        self.forge.close_work_item(number)

    def verdict(self, job: Job) -> str | None:
        number = self._item_number(job)
        if isinstance(number, NotVisible):
            # `None` here means "not answered", and an invisible item is reported as unanswered --
            # so a stale read costs a RE-RUN, never an unearned green. That is the safe direction
            # and it is not free: on GitHub it can cost a 25-minute gate. The fix is a local
            # testkey -> number index, which is a layer above this one.
            return None
        for label in self.forge.labels(number):
            word = _LABEL_TO_VERDICT.get(label)
            if word is not None:
                return word
        return None

    def verdict_detail(self, job: Job) -> str:
        """The evidence behind the verdict -- gate.py's output, as the last non-claim comment.

        CLAIM COMMENTS ARE EXCLUDED rather than assumed absent: claims and verdicts share one
        comment stream, and a job released after its verdict would otherwise hand back `CLAIM ...`
        as the gate's output -- plausible, wrong, and nothing would flag it.
        """
        number = self._item_number(job)
        if isinstance(number, NotVisible):
            return ''
        bodies = [c.body for c in self.forge.comments(number) if decode_claim(c.body, comment_id=c.id) is None]
        return bodies[-1] if bodies else ''

    def item_state(self, job: Job) -> str | None:
        number = self._item_number(job)
        return None if isinstance(number, NotVisible) else self.forge.state(number)

    def work_item_number(self, job: Job, *, create: bool = False) -> int | NotVisible:
        """Which work item carries `job`. Returns a number or :data:`NOT_VISIBLE` -- NEVER "absent".

        PUBLIC SO THAT NOBODY ELSE REDERIVES THE TITLE SCHEME. `spool.ForgePublisher` needs the item
        to scan for its replay marker, and spelling `[swarm] <namespace>/<claim_key>` a second time
        in a second module would be a duplicated scheme rather than one drifted copy.
        """
        return self._item_number(job, create=create)

    def _item_title(self, job: Job) -> str:
        """The ONE spelling of a work item's identity. Everything that needs to find a job's item
        goes through here, so there is one scheme rather than two copies drifting apart."""
        return f'{_ITEM_TITLE_ROOT} {self.namespace}/{job.claim_key()}'

    def register(self, job: Job) -> int:
        """Create this job's work item ONCE, from the single writer that owns submitting it.

        **THIS IS THE FIX, AND EVERYTHING BELOW IT IS MITIGATION.** Concurrent creation is a race
        that no amount of re-reading closes on a forge whose list lags -- measured on GitHub, 8
        threads x 3 rounds left 8 duplicate items EVERY round, and the failure was not "each runner
        arbitrated on its own item" but `created-blind`: the convergence re-read did not return even
        the reader's OWN just-created issue, 24 of 24 times. A mitigation that reads from the same
        stale view that caused the problem cannot work.

        So: the submitter calls `register` exactly once and records the number; runners DISCOVER and
        claim, never create. That mirrors the retired ref design, where only `ci.py` wrote
        `refs/candidates/*` and only `ci_tick` read them -- elimination, not mitigation.

        Returns the number from the creation response, which is authoritative and fresh on every
        forge. It does not re-read to find what it just made; see `_item_number`.
        """
        if self.role is Role.RUNNER:
            msg = f'a {Role.RUNNER.value} store may not register work items; submitting is not its job'
            raise PermissionError(msg)
        title = self._item_title(job)
        number = self.forge.create_work_item(title=title, body=f'`{job.claim_key()}`')
        self._item_numbers[title] = number
        return number

    def _item_number(self, job: Job, *, create: bool = False) -> int | NotVisible:
        title = self._item_title(job)
        cached = self._item_numbers.get(title)
        if cached is not None:
            return cached

        remembered = self._from_index(job, title)
        if remembered is not None:
            self._item_numbers[title] = remembered
            return remembered

        found = self._lowest_numbered(title)
        if found is not None:
            self._item_numbers[title] = found
            self._remember(job, found)
            return found
        if not create:
            # NOT `None`. The list said "I cannot see it", and the caller must not be able to read
            # that as "it is not there" -- which is the exact substitution that produced duplicates.
            return NOT_VISIBLE

        if self.role is Role.RUNNER:
            # STRUCTURAL, not conventional. A runner reaching here has read a list that cannot see
            # the item -- which on a lagging forge means "not replicated yet" far more often than
            # "never submitted" -- and creating one is how eight runners produce eight items.
            msg = (
                f'a {Role.RUNNER.value} store may not create a work item for {job.claim_key()!r}: '
                f'either it has not been submitted yet, or the list is stale. The submitter creates '
                f'it exactly once via ForgeStore.register().'
            )
            raise PermissionError(msg)

        # THE CREATION RESPONSE IS THE ONLY FRESH ANSWER. `POST /issues` returns the number in its
        # 201 body: authoritative, immediate, free. Any code that creates and then lists to find
        # what it just created is strictly worse, and is a bug on BOTH forges even though only one
        # exposes it.
        mine = self.forge.create_work_item(title=title, body=f'`{job.claim_key()}`')

        winner = self._converge(title, mine)
        self._item_numbers[title] = winner
        self._remember(job, winner)
        return winner

    def _from_index(self, job: Job, title: str) -> int | None:
        """Turn a remembered number into an authoritative answer, or `None` to fall through.

        A HIT IS A HYPOTHESIS. It is confirmed by a read BY NUMBER -- the only read measured fresh on
        both forges -- and the confirmation is what makes this a shortcut rather than a second
        source of truth.

        Three outcomes, and the third is the one that must not be quiet:

        * the item exists and its title matches  -> authoritative, and it cost one fresh read
        * the number no longer exists            -> an ordinary stale entry: FORGET it and fall
          through to the list, so a cold cache costs re-reads and never correctness
        * the number exists with a DIFFERENT title -> two keys have been crossed. That is
          corruption, not a miss. The entry is forgotten (self-correcting, so the retry is clean)
          and then it RAISES (loud), because a store that carried on would write this job's verdict
          onto somebody else's work item.
        """
        if self.index is None:
            return None
        remembered = self.index.get(job.claim_key())
        if isinstance(remembered, NotIndexed):
            return None
        item = self.forge.work_item(remembered.number)
        if item is None:
            self.index.forget(job.claim_key())
            return None
        if item.title != title:
            self.index.forget(job.claim_key())
            msg = (
                f'work-item index is corrupt: {job.claim_key()!r} pointed at #{remembered.number}, '
                f'which is titled {item.title!r}, not {title!r}. The entry has been dropped; a '
                f'retry will resolve cleanly, but find out what crossed the two keys.'
            )
            raise IndexCorruptError(msg)
        return item.number

    def _remember(self, job: Job, number: int) -> None:
        if self.index is not None:
            self.index.put(job.claim_key(), number)

    def _converge(self, title: str, mine: int) -> int:
        """Agree with concurrent creators on ONE item, as far as the backend permits.

        A PROBABILITY IMPROVEMENT, NOT A GUARANTEE, and the docstring says so because the code
        cannot. Issue numbers are server-assigned and monotonic, so "lowest wins" converges IF every
        racer can see the others. When the list lags, they cannot, and each keeps its own -- which is
        precisely the measured GitHub failure. `list_staleness_seconds` buys a window; it does not
        close one, and a caller that needs a guarantee must use `register` instead.
        """
        if self.list_staleness_seconds > 0:
            # Sleeping is ugly and it is the honest shape: the delay belongs to the forge's
            # replication, and pretending otherwise would put the lie in the code instead of here.
            time.sleep(self.list_staleness_seconds)
        found = self._lowest_numbered(title)
        if found is None or found >= mine:
            return mine
        # Someone earlier exists and is visible. Retire our duplicate so the list heals rather than
        # accumulating; retirement is the FORGE's business, which is why it is not spelled out here.
        self.forge.retire_work_item(mine)
        return found

    def _lowest_numbered(self, title: str) -> int | None:
        """The lowest-numbered VISIBLE item with this title, or ``None`` for "none visible".

        Private, and the ``None`` stays inside this class: at this depth "not visible" and "not
        there" are genuinely the same observation, and it is the PUBLIC boundary that must refuse to
        let a caller confuse them.
        """
        matches = [item.number for item in self.forge.list_work_items() if item.title == title]
        return min(matches) if matches else None

    # -- housekeeping ------------------------------------------------------------------------

    def purge_namespace(self) -> None:
        """Retire every work item THIS namespace created.

        SCOPED BY CONSTRUCTION, not by care: the title prefix begins with the namespace, so another
        swarm's items are not reachable from here. HOW an item is retired is the forge's business --
        this deployment closes and retitles, another may delete.
        """
        title_prefix = f'{_ITEM_TITLE_ROOT} {self.namespace}/'
        for item in self.forge.list_work_items():
            if item.title.startswith(title_prefix):
                self.forge.retire_work_item(item.number)
