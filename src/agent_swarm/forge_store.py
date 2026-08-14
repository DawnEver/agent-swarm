"""A :class:`~agent_swarm.store.Store` over any forge. All the logic; no vendor anywhere in it.

NO REFS. Issues and their comments are the whole storage layer (user directive 2026-08-09:
彻底废弃 ref, 后面基于 issue 和 project 迭代). The ref-push claim this file used to carry is
deleted, not deprecated.

THE CLAIM PROTOCOL IS NOT HERE. It is `agent_swarm.claim`, one implementation shared by this
store (`slots=1`) and by the fleet seat pool (`slots=N`) -- including the reasoning, the measured
numbers and the deployment precondition. **The explanation lives with the code for the same reason
the code does:** a second copy of the argument drifts from the implementation exactly as a second
copy of the arbitration drifts from the first, and a docstring that has drifted is worse than none
because it is still believed.

CREATION IS A RACE, AND IT IS DELETED RATHER THAN MITIGATED
==========================================================

The protocol above says "the job's work item" as though it exists. When it does not, sixteen runners
read an empty list, create sixteen items, and each arbitrates its claim on its own -- sixteen
winners, with the claim protocol working perfectly on each of sixteen wrong issues.

The first fix was convergence: create, re-read, take the lowest. **That is now DELETED, because it
was measured not to work.** On GitHub the plain list is stale 22/22 (0.42-6.36 s) and the re-read
did not return even the reader's OWN just-created issue, 24 of 24 times -- `created-blind`. A
mitigation that reads from the same stale view that caused the problem cannot work, and one that
works on Gitea alone is worse than none: it makes the forge we test against unrepresentative of the
forge we ship to.

What replaces it is structural. `Role.RUNNER` may not create at all, so the only creator is the
submitter, and a submitter has the 201 body and never needs to ask a list about its own work. There
is no window to tune and no constant to get wrong.

THE RESIDUAL, NAMED: two concurrent SUBMITTERS still duplicate, and nothing here detects it. The
contract is one submitter per job -- `register` is that call -- and it is enforced by role for
runners and by convention for submitters. If that convention ever needs enforcing, the place is a
lock outside this class, not another re-read inside it.

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
import re
import threading
import hashlib
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import ClassVar, Self

from agent_swarm.admission import SHARD_SUFFIX_PATTERN
from agent_swarm.claim import Arbiter, Held, LeaseLost, beat_interval, decode_claim
from agent_swarm.forge import Forge
from agent_swarm.item_index import IndexCorruptError, ItemIndex, NotIndexed
from agent_swarm.job import Job, JobKind
from agent_swarm.store import VERDICTS

#: How long a claim stays valid without being released. Long enough for the longest blocking gate
#: (30 minutes, `AGENTS.md`) plus the slack a shared box costs; short enough that a dead machine
#: does not park a job for a working day.
DEFAULT_LEASE_SECONDS = 3 * 3600.0

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

#: The first token of EVERY work-item title this package creates, seats included.
#:
#: PUBLIC because `seats` needs it and had its own copy. Two constants holding '[swarm]' agree until
#: one is edited: `purge_namespace` matches on THIS prefix, so a seat pool spelling its own would
#: quietly stop being purged -- the cleanup would report success having matched nothing, and the
#: leak would only ever be visible on a real server.
ITEM_TITLE_ROOT = '[swarm]'

#: THE HANDOVER LABEL. An item without it is NOT work, whoever created it and whatever its title.
#:
#: WHY DEFAULT-DENY. The alternative -- claimable unless a `swarm:hold` label says otherwise -- puts
#: the cost of FORGETTING on the side where forgetting is normal: a human who meant to keep an item
#: and did not say so gets an agent editing the same files. Two labels with opposite meanings are
#: also a state machine nobody maintains. One label, absent by default, and the failure of memory
#: costs an item that sits still.
#:
#: WHAT IT ADDS BEYOND THE TITLE PREFIX, which already excludes anything this store did not create:
#: **taking work back mid-flight.** Removing the label is a one-click stop that needs no CLI, no
#: credential and no race with a runner -- the next `claimable` simply stops offering it. Before
#: this there was no lever at all short of closing the item, which is also how work is ANSWERED.
#:
#: Store-created items get it at creation, so the CI loop needs no human in it. That asymmetry is
#: the design: the swarm hands work to itself, and a human hands work over by adding one label.
READY_LABEL = 'swarm:ready'


#: Prefix for the labels carrying what the SUBMITTER asked to be run.
#:
#: DECLARED, NEVER RECOMPUTED. The ref transport had nowhere to put this, so both `ci.py candidate`
#: and `ci_tick` derived it independently from the branch name (`['fast','heavy'] if name == 'main'`)
#: -- a duplicated derivation, which is the defect; the drifted copy is only its symptom. It had
#: already produced one: `--heavy` on any branch but `main` printed work that nobody would ever do.
#:
#: LABELS RATHER THAN THE BODY. `WorkItem` carries no body and adding one would change the `Forge`
#: protocol for both vendors; labels are already fetched by number, and this store already trusts
#: them for the VERDICT, which is the more consequential read. If labels were too stale to carry a
#: request, they would be too stale to carry an answer.
_KIND_LABEL_PREFIX = 'run:'

#: Prefix for the labels naming what an executor must HAVE in order to do this work.
#:
#: DECLARED BY THE SUBMITTER, MATCHED BY THE EXECUTOR, AND NEVER INTERPRETED HERE. `requires:femm`
#: is an opaque token to this package: it is compared for equality against whatever capability
#: strings the caller declares for its box, and nothing in this module knows that FEMM is a solver,
#: that it is licensed, or that it exists. The moment this file could enumerate the legal values it
#: would be the vendor registry `admission.is_known_class` refuses to be.
#:
#: SAME LABEL FETCH AS THE VERDICT AND THE HANDOVER, which is why `claimable` can answer "what can
#: this box take" without a second round trip per item -- see `Claimable.requires`.
_REQUIRES_LABEL_PREFIX = 'requires:'


@dataclass(frozen=True, slots=True)
class Claimable:
    """Work a runner could take. **Its truthiness is deliberately undefined.**

    WHY A WRAPPER RATHER THAN A LIST. The one thing a caller must never conclude from this is
    ABSENCE. Every list read on a forge can be stale -- measured on GitHub, `?labels=` lagged 4-6.6 s
    and 20/20 reads missed a just-created item -- so a short result means "none VISIBLE", never
    "none EXIST". Seeing less than exists costs a runner one idle tick and nothing else; concluding
    that nothing exists is what re-runs a 25-minute job or creates a duplicate work item.

    A docstring saying so would not have been enough, and this project has the receipts: the
    `tmp_path` scope trap was described in a comment in the very file where two people then fell
    into it. So the rule is STRUCTURAL. `if not claimable:` raises; the caller writes `.jobs` and
    thereby says which question it is asking.

    Iterating, indexing and `len()` all work -- the ban is only on the one expression whose two
    readings ("no work visible" and "no work exists") are indistinguishable at the call site.
    """

    jobs: tuple[Job, ...]

    #: claim key -> what an executor must HAVE to do it, from `requires:` labels.
    #:
    #: CARRIED HERE RATHER THAN FETCHED LATER, and the reason is the one already paid for in
    #: `claimable`: the listing response carries the labels, so reading them per job afterwards
    #: would be the N+1 that was measured at 101 calls for 100 items and then deleted. A pull
    #: surface that has to ask "can this box do it" for every offered job is precisely the caller
    #: that would have reintroduced it.
    #:
    #: A JOB MISSING FROM THIS MAP REQUIRES NOTHING. That is not the same as "unknown": every job in
    #: `jobs` was built from the same listing entry, so its absence here means the submitter
    #: declared no requirement, and `requirements_for` returns an empty set rather than raising.
    requires: dict[str, frozenset[str]] = field(default_factory=dict)

    def requirements_for(self, job: Job) -> frozenset[str]:
        """What `job` needs. Empty means nothing was declared, which is a real answer here."""
        return self.requires.get(job.claim_key(), frozenset())

    def __bool__(self) -> bool:
        msg = (
            'Claimable has no truth value: an empty result means NO WORK VISIBLE, never no work '
            'exists, because a forge list read can be stale. Write `.jobs` and say which you mean '
            '-- `if not c.jobs:` to idle this tick is fine; concluding a job is absent is not.'
        )
        raise TypeError(msg)

    def __iter__(self):
        return iter(self.jobs)

    def __len__(self) -> int:
        return len(self.jobs)

    def preferred(self, owner: str) -> tuple[Job, ...]:
        """The same jobs, ordered so that DIFFERENT owners lead with different ones.

        THE MEASURED BOTTLENECK, and the only repair that helps it. Claim arbitration is the one
        per-job cost that grows with the fleet: measured against the live forge 2026-08-10, one
        contended item elects exactly one winner per round at 297 ms with 2 racers, 667 ms with 8
        and 1373 ms with 16 -- linear, ~85 ms per extra contender. A contended item therefore caps
        its group at one job per round no matter how many agents join, while agents on DISTINCT
        items scale linearly against the per-call floor (~60 ms p50). Every racer that loses paid
        the full round to learn it lost.

        Nothing about that is fixed by a faster protocol; it is fixed by fewer racers per item. Ten
        runners taking the first visible job all contend on ONE item and nine of them do nothing.
        Rotating the start position by a hash of the owner spreads them with NO coordination --
        which is the property that matters, because a coordinator would need its own claim.

        WHAT THIS BUYS, MEASURED LIVE rather than predicted: jobs completed per round goes 1 -> 7 at
        16 runners and 1 -> 3 at 8 (0.5 -> 2.8 and 1.2 -> 4.2 jobs/s). That is ~0.44N, NOT N -- the
        balls-in-bins expectation, and the N figure was refuted the first time it met a real server.
        Round WALL CLOCK does not improve at all (2194 ms contended vs 2495 ms spread at N=16): the
        wall is set by the aggregate API-call volume both arms issue. So this is a throughput repair
        and not a latency one, and after it the binding constraint is the forge's aggregate
        throughput under concurrency rather than per-item contention.

        **A PREFERENCE ORDER, NOT A PARTITION, and that distinction is the whole design.** A
        partition (`jobs[i::n]`) starves: with three items and ten runners, seven get an empty
        shard and idle while work sits visible. This returns a PERMUTATION -- every job is still
        reachable by every owner, just later -- so a runner that loses its first race walks the
        rest and the system degrades to the old behaviour instead of to starvation.

        DETERMINISTIC PER OWNER, and by `sha256` rather than `hash()`: `hash()` is salted per
        process, so two runs of the same runner would prefer different items and a retry would race
        a fresh item rather than the one it just lost.

        ORDERING STAYS THE CALLER'S. This is offered, not applied -- `claimable` still returns the
        forge's own order, and a scheduler with real policy (retry budgets, capability gates,
        integration branch first) sorts by that instead. This is the default for a caller that has
        no policy, and its alternative is not "some other order" but "everyone takes the first one".
        """
        if not self.jobs:
            return ()
        digest = hashlib.sha256(owner.encode()).digest()
        start = int.from_bytes(digest[:8], 'big') % len(self.jobs)
        return self.jobs[start:] + self.jobs[:start]


def decode_claim_key(key: str, *, kind: JobKind) -> Job | None:
    """`test-run/abc/s2of4` -> the Job that produced it, or None if it is not this kind's.

    THE INVERSE OF `Job.claim_key`, and it lives here for the reason the title scheme does: one
    spelling. A consumer decoding item titles itself would be a second definition of the identity
    grammar, free to drift from the one that writes it.

    IDENTITY ONLY. A claim key carries kind, id and the shard/width -- it does not carry `ram_gib`,
    `exclusivity`, `solo_seconds` or `ceiling_seconds`, so those come back as Job defaults and the
    CALLER must supply them from its own policy. Said plainly because `exclusivity` defaults to the
    whole box: a caller that scheduled straight off this Job would be correct but maximally
    conservative, and one that read the default as a measurement would be wrong.
    """
    prefix = f'{kind.value}/'
    if not key.startswith(prefix):
        return None
    rest = key[len(prefix) :]
    shard = n_shards = None
    # THE PATTERN IS THE WRITER'S, not a second one written from the same description. A parser
    # authored separately from its writer is the drift, and this file was the third copy.
    if (m := re.fullmatch(rf'(?P<id>.+)/{SHARD_SUFFIX_PATTERN}', rest)) is not None:
        rest, shard, n_shards = m.group('id'), int(m.group('shard')), int(m.group('n_shards'))
    if not rest:
        return None
    return Job(id=rest, kind=kind, shard=shard, n_shards=n_shards)


@dataclass(frozen=True, slots=True)
class RetiredDuplicate:
    """One work item retired because another item claimed the same identity."""

    title: str
    kept: int
    retired: int


class DuplicateWorkItems(RuntimeError):
    """A key had more than one work item, and the extras have been retired.

    RAISED AFTER THE WORK, NOT INSTEAD OF IT -- the same shape as `Spool.drain`'s backlog alarm and
    for the same reason: a caller ignores a return value by not reading it, and it cannot ignore
    this. The duplicates ARE cleaned up; what must not happen is that the cleanup is quiet.

    Silent dedup would hide a submitter racing itself indefinitely. That is a bug to fix at its
    source -- two `ci.py candidate` invocations for one testkey -- and a sweep that tidied up after
    it every hour would make the source impossible to find, while the fleet looked healthy.
    """

    def __init__(self, message: str, findings: list[RetiredDuplicate]) -> None:
        super().__init__(message)
        self.findings = findings


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
            `forge.default_forge(role)`, and THE ROLE IS NOT OPTIONAL THERE EITHER: four
            credentials share one host, and which one a process holds is the only thing separating
            the roles, since Gitea has no scope for commit status.
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
    ) -> None:
        self.role = role
        self.index = index
        # ONE STORE CREATES AT MOST ONE ITEM PER TITLE, even with sixteen threads inside it. This is
        # not the re-read mitigation coming back: it reads no list and asks the forge nothing. It
        # closes the window between "the cache is empty" and "the cache is filled" WITHIN a process,
        # which is the window the contract's sixteen-thread race drives straight through.
        # Across processes there is no such lock and none is possible -- that is the named residual,
        # and `register` plus the runner role is what answers it.
        self._create_lock = threading.Lock()
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
        """Take `job` for `owner`. ``False`` if somebody already holds it.

        ONE SLOT, AND THE ARBITRATION IS `agent_swarm.claim`'s -- the same code the fleet seat pool
        runs at N slots. This store used to carry its own copy of it; the copy is deleted rather
        than kept in step, because two implementations of one protocol are two places every future
        fix has to land and the missed one is never the one being looked at.

        Raises:
            ArbitrationUnsound: the backend could not read a comment it had just written, so no
                claim on it means anything. This used to be a quiet ``False``, which made a broken
                deployment indistinguishable from a busy one.
        """
        return self._claim(job, owner=owner, reclaim=False, reclaim_miss_limit=None, reclaim_sample=None)

    def try_reclaim(
        self,
        job: Job,
        *,
        owner: str,
        reclaim_miss_limit: int | None = None,
        reclaim_sample: Callable[[], None] | None = None,
    ) -> bool:
        """Take `job` for `owner`, taking over a holder only after watching it stop beating.

        THE CROSS-MACHINE CLAIM PATH. `try_claim` is the single-process one: it arbitrates a FRESH
        race and reclaims a holder that has LAPSED by wall clock, and it never steals a holder that
        is merely not yet expired. Across machines that is not enough -- two boxes whose clocks
        disagree cannot agree on "lapsed", so a holder dead on its own box can read as live on a
        candidate's for up to a lease. This path answers that: it runs the same fast `take` first,
        and if a live claim ranks ahead it hands off to :meth:`agent_swarm.claim.Arbiter.reclaim` --
        the failure detector that steals only after the holder's own monotonic `refresh` counter has
        gone unchanged for `reclaim_miss_limit` consecutive reads. A holder that keeps beating is
        never stolen, whatever either machine's clock says.

        THE DIVISION OF LABOUR IS THE PROTOCOL'S, NOT A HAND-WAVE. `take` (inside `try_claim`) keeps
        its wall-clock path for the single-process case where one clock is authoritative; `reclaim`
        is the clock-free takeover for the cross-machine case where no clock is. Both coexist; the
        CAS is still the comment id, decided by the same one read -- this method only decides WHEN a
        holder may be taken over, never HOW. See `agent_swarm.claim` for why a holder judged dead is
        reclaimed by deleting its claim comment and arbitrating the slot, and why the fleet is never
        left with two live holders even through a false verdict.

        Args:
            reclaim_miss_limit: consecutive unchanged reads that prove the holder dead, passed to
                `reclaim`. ``None`` means `reclaim`'s own default, so the fleet's choice is the one
                the detector defines and never a second spelling.
            reclaim_sample: called between `reclaim`'s polls at the candidate's cadence, letting a
                live holder beat before the next read. ``None`` means `reclaim` sleeps its own
                `beat_every`, the one derivation.

        Raises:
            ArbitrationUnsound: the backend could not read a comment it had just written, so no
                claim on it means anything.
            ValueError: a non-positive `reclaim_miss_limit`.
        """
        return self._claim(
            job,
            owner=owner,
            reclaim=True,
            reclaim_miss_limit=reclaim_miss_limit,
            reclaim_sample=reclaim_sample,
        )

    def _claim(
        self,
        job: Job,
        *,
        owner: str,
        reclaim: bool,
        reclaim_miss_limit: int | None,
        reclaim_sample: Callable[[], None] | None,
    ) -> bool:
        """The ONE claim flow, fast path then optional clock-free takeover.

        `try_claim` and `try_reclaim` are the two faces of this single implementation; a second copy
        would be the drift this module exists to delete. The fast `take` decides the fresh race and
        the single-process wall-clock reclaim; the `reclaim` hand-off is engaged only when asked,
        which is what keeps `try_claim`'s contract -- a held, non-expired claim refuses a new
        claimant -- from stealing in a single process.
        """
        number = self._item_number(job, create=True)
        assert not isinstance(number, NotVisible)
        arbiter = self._arbiter(number)
        if arbiter.take(owner=owner) is not None:
            return True
        if not reclaim:
            return False
        kwargs: dict[str, object] = {'owner': owner}
        if reclaim_miss_limit is not None:
            kwargs['miss_limit'] = reclaim_miss_limit
        if reclaim_sample is not None:
            kwargs['sample'] = reclaim_sample
        return arbiter.reclaim(**kwargs) is not None

    def renew_claim(self, job: Job, *, owner: str) -> float:
        """Beat the heart of an existing claim. Returns the NEW expiry.

        **THE LEASE IS WHAT MAKES A DEAD HOLDER RECOVERABLE, AND THE HEARTBEAT IS WHAT MAKES THE
        LEASE SHORT.** Without one, the only safe lease is longer than the longest job -- three
        hours, here -- so a laptop that claims a job and closes its lid parks it for three hours. A
        holder that beats can be leased for minutes, and a holder that stops beating is reclaimed in
        minutes. Nothing else in the protocol changes.

        THE HOLD IS RECOVERED FROM THE SERVER, NOT REMEMBERED. This store keeps no handle between
        calls -- it re-reads who holds the item and rebuilds the record from the comment -- so a
        runner that restarts mid-job can go on beating a claim it took in a previous process. A
        cached handle would have made that impossible while looking like an optimisation.

        WHY IT REFUSES A CLAIM THAT IS NOT OURS rather than taking it over: the holder may be
        somebody else because our lease lapsed and the ordinary arbitration elected them. Beating
        then would produce two live holders, which is the one outcome the protocol exists to make
        impossible -- so the answer is that WE have lost, and the caller must stop.

        Raises:
            LeaseLost: our claim is gone -- expired and taken, pruned, or never held.
        """
        number = self._item_number(job)
        if isinstance(number, NotVisible):
            # A claim we cannot even find the item for is not a claim we can prove we hold. Reported
            # as LOST rather than as "probably fine": the safe direction for a heartbeat is to stop
            # a runner that is still healthy, never to reassure one that is not.
            msg = f'cannot renew {job.claim_key()!r}: its work item is not visible from here'
            raise LeaseLost(msg)
        arbiter = self._arbiter(number)
        holders = arbiter.holders()
        mine = holders.by(owner)
        if mine is None:
            who = 'nobody' if not holders.claims else repr(holders.claims[0].owner)
            msg = (
                f'{owner!r} no longer holds {job.claim_key()!r} -- it is held by {who}. '
                f'Stop the work: something else may already have taken it.'
            )
            raise LeaseLost(msg)
        return arbiter.renew(
            Held(
                arbiter=arbiter,
                owner=owner,
                comment_id=mine.comment_id,
                expires_at=mine.expires_at,
                refresh=mine.refresh,
            )
        )

    def claim_owner(self, job: Job) -> str | None:
        number = self._item_number(job)
        if isinstance(number, NotVisible):
            # An item we cannot see holds no claim WE can honour. This errs toward "unclaimed",
            # which risks a duplicate run; the lease bounds that, and the alternative -- reporting a
            # claim we cannot read -- would deadlock the job instead.
            return None
        claims = self._arbiter(number).holders().claims
        return claims[0].owner if claims else None

    def release(self, job: Job, *, owner: str) -> None:
        """Release `job` if `owner` holds it. A non-owner's release is a no-op, never a steal.

        THE COMMENT ID IS LOOKED UP RATHER THAN PASSED, because `Store.release` is specified in
        terms of the JOB and the OWNER and this store keeps no handle. At one slot that lookup is
        unambiguous -- there is only ever one holder to be.
        """
        number = self._item_number(job)
        if isinstance(number, NotVisible):
            return
        arbiter = self._arbiter(number)
        mine = arbiter.holders().by(owner)
        if mine is not None:
            arbiter.release(owner=owner, comment_id=mine.comment_id)

    @property
    def beat_every(self) -> float:
        """How often a holder of one of THIS store's claims must beat.

        OFFERED SO A CALLER NEVER COMPUTES IT. A process hosting a heartbeat needs the cadence and
        holds a store, not an arbiter; without this it would divide `lease_seconds` itself, which is
        the second spelling `claim.beat_interval` exists to prevent.
        """
        return beat_interval(self.lease_seconds)

    def _arbiter(self, number: int) -> Arbiter:
        """This store's claim, which is the package's ONE claim, at one slot.

        Built per call rather than cached: construction is pure, and a cached arbiter would be a
        second place the item number is remembered -- `_item_numbers` is already that place, and
        two caches of one fact is the shape this refactor exists to remove.
        """
        return Arbiter(self.forge, item_number=number, slots=1, lease_seconds=self.lease_seconds)

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

        # THE VERDICT LABEL LANDS BEFORE THE CLOSE, and the order is load-bearing twice over.
        #
        # It used to ride ALONG WITH the close, as a `labels` replacement on one PATCH -- 3 round
        # trips instead of 4. REFUTED against the live server 2026-08-10: the PATCH returned 200,
        # the item closed, and the label was never attached. So the verdict was unreadable while
        # the job was already unclaimable -- the silent-loss state this method is ordered to avoid.
        #
        # Now: comment, read labels, drop a stale verdict, add this one, close. Every write except
        # the last leaves the item OPEN, so a crash anywhere leaves work that is still claimable and
        # gets re-run, at the cost of one duplicate comment. Closing LAST is what makes that true.
        #
        # Exactly one verdict label at a time: a retry after INCONCLUSIVE that merely ADDED `pass`
        # would leave the job both inconclusive and green, and nothing downstream can act on that.
        # The removal costs a round trip only when there IS a stale one, which is the retry path.
        #
        # Everything that is not a verdict label is PRESERVED -- the handover label and anything a
        # human attached. The old replacement set had to rebuild them, and getting that list wrong
        # would have surfaced only as work that quietly stopped being offered.
        for name in self.forge.labels(number):
            if name in _LABEL_TO_VERDICT:
                self.forge.remove_label(number, name)
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
        return f'{ITEM_TITLE_ROOT} {self.namespace}/{job.claim_key()}'

    def claimable(self, kind: JobKind) -> Claimable:
        """Work of `kind` a runner could take: OPEN, in this namespace, and not yet answered.

        THE QUESTION A RUNNER HAS AND NOBODY ELSE COULD ANSWER. Every other method here takes a Job
        the caller already holds; a runner's whole problem is that it has none yet. Before this, the
        only way to ask was `Forge.list_work_items()` from the consumer -- which would put the
        identity grammar and the answered-ness rule in a second place, free to drift from the ones
        that write them. "The open, unclaimed work in this namespace" says nothing about any
        particular consumer, so it is store vocabulary.

        **ORDERING IS THE CALLER'S.** Returned in the forge's own order and nothing more: a
        scheduler sorts by its own policy (integration branch first, retry budgets, capability
        gates) and the store cannot see any of it. Sorting here would be a policy the caller cannot
        override and cannot inspect.

        **AN EMPTY RESULT MEANS NO WORK VISIBLE, NEVER NO WORK EXISTS** -- which is why this returns
        :class:`Claimable` rather than a list. See its docstring; the ban on truthiness is the whole
        point of the wrapper.

        **AN UNREACHABLE STORE RAISES**, matching `live_runners` in the CI scheduler rather than the
        `fleet_capabilities` asymmetry. Returning an empty result on a network failure would make an
        offline runner report "no work" forever -- indistinguishable from a genuinely idle queue,
        and a regression that layer has already been through once. `ForgeError` propagates.

        ANSWERED-NESS IS READ FROM LABELS, *AND* OPENNESS FROM STATE -- both, because neither alone
        is the question. **A claim this docstring first made and which is FALSE:** that
        `verdict:inconclusive` leaves an item open, so the label check was what carried the
        three-valued distinction. It does not -- `record_verdict` closes on every verdict word
        including INCONCLUSIVE. Deleting the label check left the whole suite green, which is how
        the claim was caught.

        What the label check ACTUALLY buys is the REOPENED item: a human or a retry policy reopens
        an answered issue without clearing its verdict label. State says claimable, the label says
        answered, and the safe reading is answered -- handing that to a runner re-runs a job whose
        conclusion is already published. `test_a_REOPENED_item_carrying_a_verdict_label_is_not_work`
        is the only test that discriminates it.
        """
        prefix = f'{ITEM_TITLE_ROOT} {self.namespace}/'
        jobs: list[Job] = []
        requires: dict[str, frozenset[str]] = {}
        for item in self.forge.list_work_items(state='open'):
            if item.state != 'open' or not item.title.startswith(prefix):
                continue
            job = decode_claim_key(item.title[len(prefix) :], kind=kind)
            if job is None:
                continue
            self._item_numbers[item.title] = item.number
            # FROM THE LISTING, not a per-item fetch. That fetch was N+1 -- 101 calls for 100
            # open items, per runner, per sweep -- to re-ask something the list response already
            # carried.
            labels = item.labels
            if any(label in _LABEL_TO_VERDICT for label in labels):
                continue
            # DEFAULT DENY. Read from the SAME label fetch as the verdict check -- a second call
            # would be a second observation of a mutable thing, and an item whose label was removed
            # between them would read as claimable on one line and withdrawn on the next.
            if READY_LABEL not in labels:
                continue
            # FROM THE SAME `labels`, for the same reason the two checks above share it: a second
            # observation of a mutable thing can disagree with the first, and here that would mean
            # offering a job under one set of requirements and admitting it under another.
            declared = frozenset(
                label.removeprefix(_REQUIRES_LABEL_PREFIX)
                for label in labels
                if label.startswith(_REQUIRES_LABEL_PREFIX)
            )
            if declared:
                requires[job.claim_key()] = declared
            jobs.append(job)
        return Claimable(jobs=tuple(jobs), requires=requires)

    def newest_open(self, kind: JobKind, *, group: str) -> Job | None:
        """The newest OPEN item whose id starts with ``group/``, or ``None`` for none visible.

        **CORRECTNESS LIVES HERE, NOT IN THE SUPERSEDE.** A submitter that replaces one work item
        with another does TWO writes -- create the new, close the old -- and it will eventually die
        between them. That leaves two open items for one group, and a reader that assumed the close
        succeeded picks the STALE one. Which is precisely the failure the ref transport made
        impossible: a force-push to `refs/candidates/<branch>` retired the previous SHA atomically,
        so latest-wins was a property of the TRANSPORT and never of the design. Migrating without
        replacing it would reintroduce the bug through the back door.

        So the close is CLEANUP -- it bounds retention and keeps the tracker readable -- and a
        failed close costs one extra item and nothing else.

        **NEWEST = HIGHEST NUMBER**, server-assigned and monotonic, the same primitive the claim
        protocol already rests on. Not a timestamp in the body: a body is written by the client, so
        two submitters with skewed clocks would disagree about which item is newer, and the whole
        point is that every observer converges without coordinating.

        **`None` MEANS NONE VISIBLE**, never "the group has no work" -- a list read can be stale.
        Unlike `claimable` this returns a bare optional rather than a wrapper, because there is no
        ambiguous expression to ban: a caller must handle `None` explicitly either way, and
        `Claimable`'s ban exists for `if not jobs:` specifically.

        Args:
            kind: which loop's work.
            group: the id prefix that identifies the series -- a branch name, for candidates. The
                store does not know what a branch is; it knows that ids are `group/rest`.
        """
        prefix = f'{ITEM_TITLE_ROOT} {self.namespace}/'
        best: tuple[int, Job] | None = None
        for item in self.forge.list_work_items(state='open'):
            if item.state != 'open' or not item.title.startswith(prefix):
                continue
            job = decode_claim_key(item.title[len(prefix) :], kind=kind)
            if job is None or not job.id.startswith(f'{group}/'):
                continue
            if best is None or item.number > best[0]:
                best = (item.number, job)
        return None if best is None else best[1]

    def requirements(self, job: Job) -> frozenset[str]:
        """What an executor must HAVE to do `job`. Empty means the submitter declared nothing.

        BY NUMBER, like `requested_runs` and for the identical reason: this is the read measured
        fresh on both forges, and a caller holding a Job has already survived the staleness
        question. `Claimable.requires` is the BULK answer for a caller that is still choosing;
        this is the authoritative one for a caller about to commit.

        THE TWO MUST NOT DRIFT, so the prefix is a module constant consulted by both rather than a
        string spelled twice -- the duplicated-derivation defect that `requested_runs` records.
        """
        number = self._item_number(job)
        if isinstance(number, NotVisible):
            # NOT an empty set meaning "requires nothing". An invisible item cannot tell us what it
            # needs, and answering "nothing" would let a box take work it cannot do -- so this is
            # the one place the distinction is worth an exception rather than a value.
            msg = f'cannot read the requirements of {job.claim_key()!r}: its work item is not visible from here'
            raise LookupError(msg)
        return frozenset(
            label.removeprefix(_REQUIRES_LABEL_PREFIX)
            for label in self.forge.labels(number)
            if label.startswith(_REQUIRES_LABEL_PREFIX)
        )

    def register(self, job: Job, *, requests: Iterable[str] = (), requires: Iterable[str] = ()) -> int:
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
        # HANDED OVER AT BIRTH, IN ONE CALL. A separate `add_label` cost a second round trip per
        # registered job -- measured at 2.0 calls per job against a create-only 1.0, i.e. half the
        # registration throughput spent on a label -- and it left a window in which the item existed
        # WITHOUT the label that makes it claimable.
        # THE REQUIREMENTS RIDE ON THE CREATE, not on a follow-up `add_label`, and that is the same
        # window argument the handover label already makes: between the create and a second call the
        # item is claimable and declares NO requirement, so a box without the capability can take it
        # and the declaration arrives too late to matter. `requests` stays a follow-up because it is
        # read only by a runner that already holds the item, where a late arrival costs nothing.
        labels = [READY_LABEL]
        for required in sorted(set(requires)):
            if not required:
                msg = 'a requirement must be named; an empty requirement is not a requirement'
                raise ValueError(msg)
            labels.append(f'{_REQUIRES_LABEL_PREFIX}{required}')
        number = self.forge.create_work_item(title=title, body=f'`{job.claim_key()}`', labels=labels)
        self._item_numbers[title] = number
        for requested in sorted(set(requests)):
            if not requested:
                # An empty request would produce the bare prefix, which `requested_runs` would then
                # read back as an unnamed run -- a request nothing can schedule and nobody can see.
                msg = 'a requested run must be named; an empty request is not a request'
                raise ValueError(msg)
            self.forge.add_label(number, f'{_KIND_LABEL_PREFIX}{requested}')
        # THE INDEX IS WRITTEN HERE OR IT IS NEVER WRITTEN USEFULLY. The submitter is the only
        # creator, so this is the one moment the number is known for free -- and it was missing:
        # the index could previously only be warmed by a lookup that had ALREADY paid for the list
        # read, so it never saved the read it exists to save. Measured before the fix, a "warm"
        # index cost 5060 ms against 4627 ms with no index at all, because every lookup still fell
        # through to the list. That is the assumption-not-measurement trap in one number.
        self._remember(job, number)
        return number

    def requested_runs(self, job: Job) -> frozenset[str]:
        """What the submitter ASKED to be run on this item. Empty means none declared.

        **THE SCHEDULER READS THIS; IT DOES NOT RECOMPUTE IT.** That is the entire point. Under the
        ref transport there was nowhere to put a request, so the submitter printed one payload and
        the scheduler derived its own from the branch name -- two spellings of one rule, and the
        rule was wrong in one of them for every branch that is not `main`.

        EMPTY IS A REAL ANSWER HERE, unlike everywhere else in this class, and only because the
        caller already holds the item: this reads labels BY NUMBER, which is the read measured fresh
        on Gitea, not a list filter. A caller that got here from `claimable` or `newest_open` has
        already survived the staleness question. What empty means is "the submitter declared
        nothing" -- deciding what to do about that is policy and belongs to the scheduler, which is
        also the only layer that knows whether a default is safe.
        """
        number = self._item_number(job)
        if isinstance(number, NotVisible):
            return frozenset()
        return frozenset(
            label.removeprefix(_KIND_LABEL_PREFIX)
            for label in self.forge.labels(number)
            if label.startswith(_KIND_LABEL_PREFIX)
        )

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
        with self._create_lock:
            # Re-check inside the lock: another thread of THIS store may have created it while we
            # waited, and creating a second would be the same duplicate by a shorter route.
            already = self._item_numbers.get(title)
            if already is not None:
                return already
            # NO RE-READ. The creation response is authoritative and fresh on every forge, and
            # asking the list to confirm what we just made is the `created-blind` failure: measured
            # on GitHub, the re-read did not return the reader's OWN issue 24 of 24 times.
            mine = self.forge.create_work_item(title=title, body=f'`{job.claim_key()}`', labels=[READY_LABEL])
            self._item_numbers[title] = mine
            self._remember(job, mine)
            return mine

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

    def _lowest_numbered(self, title: str) -> int | None:
        """The lowest-numbered item with this exact title, or None if none is visible.

        OPEN FIRST, EVERYTHING ONLY IF THAT MISSES. The scan used to ask for `state='all'` always,
        and MEASURED 2026-08-10 that meant a fresh runner claiming ONE job transferred the WHOLE
        history: 201 items behind 200 finished jobs. The request COUNT was 1, which is why counting
        calls never showed it -- the cost is bandwidth, pagination and server work, and closed items
        are never deleted, so after a year of 7x24 operation every claim by every restarted runner
        paid for every job ever run.

        Why open-first is CORRECT and not merely cheaper: an item worth finding is almost always an
        open one -- you cannot claim a closed item, and registration only dedupes against live work.
        The closed case is real (reading a finished job's verdict from a fresh process) and it still
        works; it simply pays the full scan, which is the rare path rather than the hot one.

        Why not open-ONLY: a retired duplicate must stay findable, or `reconcile_duplicates` reports
        a clean tracker it cannot actually see. Narrowing without the fallback would trade a
        bandwidth bill for a correctness hole.

        Private, and the ``None`` stays inside this class: at this depth "not visible" and "not
        there" are genuinely the same observation, and it is the PUBLIC boundary that must refuse to
        let a caller confuse them.
        """
        for state in ('open', 'all'):
            matches = [item.number for item in self.forge.list_work_items(state=state) if item.title == title]
            if matches:
                return min(matches)
        return None

    def reconcile_duplicates(self) -> list[RetiredDuplicate]:
        """Find every key with more than one work item, keep the LOWEST, retire the rest.

        WHY THIS EXISTS. Deleting `_converge` made creation correct within a process -- the
        in-process lock -- and unguarded across them. Two submitter PROCESSES for one key produce
        two work items and nothing detects it. Convergence is not coming back: it re-read the stale
        view whose removal was the whole point, and on GitHub it did not return even the reader's
        own just-created issue, 24 of 24 times. So the duplicate is caught AFTER the fact instead of
        prevented by a read that does not work.

        **A BACKGROUND SWEEP, NEVER THE HOT PATH.** It may be arbitrarily late; the create path may
        not be slow, and a create that waited for a list would be the deleted mitigation wearing a
        new name. Nothing in `try_claim`, `register` or `_item_number` calls this, and a test
        tokenises those to keep it so. The caller owns the schedule and must leave the backend's
        list-staleness window behind it -- a sweep that ran a millisecond after a create could
        retire an item whose sibling had not yet appeared, and then retire the other one next time.

        IT IS IDEMPOTENT: only OPEN items are considered, and retiring closes them, so running it
        twice alarms once. An alarm that fires every hour about work already done is one that gets
        filtered out of somebody's inbox, and then the real one is filtered out too.

        LOWEST WINS, because issue numbers are server-assigned and monotonic on both forges, so
        every observer converges on the same survivor without coordinating. Retiring the LATER one
        also loses less: the earlier item is the one runners are more likely to have found.

        DETECTION AND RETIREMENT ONLY. Nothing is merged and nothing is resurrected -- the survivor
        is not touched, and comments on a retired duplicate are not copied across. Merging would
        invent a history that never happened, and the same ruling was made for verdict tampering:
        the system reports, a human decides.

        FORGE-NEUTRAL: the duplicate-submitter race follows from "no compare-and-swap", not from
        Gitea. HOW an item is retired is the forge's business, which is why this calls
        `retire_work_item` and does not spell out close-versus-delete.

        Returns:
            What was retired, oldest key first. Empty means the fleet is behaving.

        Raises:
            DuplicateWorkItems: at least one duplicate was found. The findings ride on the
                exception, so nothing is lost by raising.
            PermissionError: a runner may not retire work items; the lifecycle is the submitter's.
        """
        if self.role is Role.RUNNER:
            msg = f'a {Role.RUNNER.value} store may not reconcile work items; that is the submitter lifecycle'
            raise PermissionError(msg)

        prefix = f'{ITEM_TITLE_ROOT} {self.namespace}/'
        by_title: dict[str, list[int]] = {}
        for item in self.forge.list_work_items(state='open'):
            # OPEN ITEMS ONLY, and that is what makes this sweep IDEMPOTENT rather than an alarm
            # that fires forever. Retirement closes the item, so a second pass has nothing to find.
            # Filtering on the vendor's retirement DECORATION instead would mean knowing what each
            # forge appends -- and a closed item is in any case either answered (its verdict is on
            # record; leave it) or already retired.
            if item.state == 'open' and item.title.startswith(prefix):
                by_title.setdefault(item.title, []).append(item.number)

        findings: list[RetiredDuplicate] = []
        for title, numbers in sorted(by_title.items()):
            if len(numbers) < 2:
                continue
            kept = min(numbers)
            for duplicate in sorted(numbers):
                if duplicate == kept:
                    continue
                self.forge.retire_work_item(duplicate)
                findings.append(RetiredDuplicate(title=title, kept=kept, retired=duplicate))
            # The cache and the index may be pointing at a loser; drop both so the next lookup
            # resolves cleanly rather than confirming a retired item forever.
            self._item_numbers.pop(title, None)

        if findings:
            msg = (
                f'{len(findings)} duplicate work item(s) retired across '
                f'{len({f.title for f in findings})} key(s): '
                + ', '.join(f'{f.title!r} kept #{f.kept} retired #{f.retired}' for f in findings)
                + '. A key with two items means a submitter raced itself -- fix that, do not rely on this sweep.'
            )
            raise DuplicateWorkItems(msg, findings)
        return findings

    def purge_namespace(self) -> None:
        """Retire every work item THIS namespace created.

        SCOPED BY CONSTRUCTION, not by care: the title prefix begins with the namespace, so another
        swarm's items are not reachable from here. HOW an item is retired is the forge's business --
        this deployment closes and retitles, another may delete.
        """
        title_prefix = f'{ITEM_TITLE_ROOT} {self.namespace}/'
        for item in self.forge.list_work_items():
            if item.title.startswith(title_prefix):
                self.forge.retire_work_item(item.number)
