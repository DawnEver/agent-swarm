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

import time
from dataclasses import dataclass

from agent_swarm.forge import Forge
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

    def __init__(self, namespace: str, forge: Forge, *, lease_seconds: float = DEFAULT_LEASE_SECONDS) -> None:
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
        if number is None:
            return None
        holder = self._holder(number, now=time.time())
        return holder.owner if holder else None

    def release(self, job: Job, *, owner: str) -> None:
        """Release `job` if `owner` holds it. A non-owner's release is a no-op, never a steal."""
        number = self._item_number(job)
        if number is None:
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
        if number is None:
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
        if number is None:
            return ''
        bodies = [c.body for c in self.forge.comments(number) if decode_claim(c.body, comment_id=c.id) is None]
        return bodies[-1] if bodies else ''

    def item_state(self, job: Job) -> str | None:
        number = self._item_number(job)
        return None if number is None else self.forge.state(number)

    def work_item_number(self, job: Job, *, create: bool = False) -> int | None:
        """Which work item carries `job`, creating it if asked. ``None`` if absent and not creating.

        PUBLIC SO THAT NOBODY ELSE HAS TO REDERIVE THE TITLE SCHEME. `spool.ForgePublisher` needs
        the item in order to scan for its replay marker, and the alternative -- spelling
        `[swarm] <namespace>/<claim_key>` a second time in a second module -- is a duplicated
        scheme rather than one drifted copy, which is the defect class this project names first. It
        stays a READ plus the same create-and-converge the store already does; it decides nothing
        new.
        """
        return self._item_number(job, create=create)

    def _item_title(self, job: Job) -> str:
        return f'{_ITEM_TITLE_ROOT} {self.namespace}/{job.claim_key()}'

    def _item_number(self, job: Job, *, create: bool = False) -> int | None:
        """The work item for `job`, created if absent. LOWEST NUMBER WINS, for the same reason.

        CREATION IS A RACE TOO, AND IT WAS THE ONE THAT BIT. Sixteen runners claiming a job whose
        item does not exist yet all read an empty list, all create an item, and each then arbitrates
        the claim on its OWN issue -- sixteen comment streams, sixteen lowest comments, sixteen
        winners. Measured exactly that way against the real server: 16/16 believed they won, with
        the claim protocol itself working perfectly on each of sixteen wrong issues.

        The fix is the protocol applied one level up, not a lock. An issue number is server-assigned
        and monotonic, exactly like a comment id, so: create, re-read, and take the LOWEST-numbered
        item carrying this title. Every racer converges on the same one. A runner that finds its own
        creation was not the winner retires it, so the duplicate stops matching the title and the
        list heals instead of accumulating.
        """
        title = self._item_title(job)
        cached = self._item_numbers.get(title)
        if cached is not None:
            return cached

        found = self._lowest_numbered(title)
        if found is None:
            if not create:
                return None
            mine = self.forge.create_work_item(title=title, body=f'`{job.claim_key()}`')
            # Re-read AFTER creating: anyone who created concurrently is now visible, and the
            # lowest number is the one every racer will independently agree on.
            found = self._lowest_numbered(title)
            if found is None or found > mine:  # pragma: no cover -- our own create must be visible
                found = mine
            if found != mine:
                self.forge.retire_work_item(mine)

        self._item_numbers[title] = found
        return found

    def _lowest_numbered(self, title: str) -> int | None:
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
