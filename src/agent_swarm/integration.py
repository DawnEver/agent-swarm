"""THE SERIALISATION POINT: submissions in, a REAL merged tree judged, the trunk advanced by CAS.

THE MEASUREMENT THIS MODULE EXISTS FOR. 2026-08-12, in the first consuming project: two branches
each fixed HALF of one check. Each branch ALONE was red; their merge was green. The converse -- two
green branches whose merge was red -- was measured the same day. A verdict on a branch is therefore
not a statement about the merge, and no amount of gating branches individually can become one. So
this module does not judge submissions. It BUILDS the tree that would exist if they landed, hands
that tree to a verdict function, and advances only on an answer about exactly those bytes.

ALMOST NOTHING HERE IS INVENTED, AND THAT IS THE DESIGN. `git merge` in a throwaway worktree builds
the real tree; `git update-ref refs/heads/<trunk> <new> <old>` IS compare-and-swap, atomic, already
correct under concurrency, and already deployed on every box. A lock, a read-then-write, or a
"queue owner" election would each be a reimplementation of one of those with fewer eyes on it.

THE THREE-VALUED VERDICT IS THE WHOLE POINT, and collapsing it is the defect this package refuses:

    PASS          about THIS tree            -> record INTEGRATED, then CAS the trunk to it
    FAIL          about THIS tree            -> record REJECTED. Terminal; the participant resubmits
    INCONCLUSIVE  about the RUN, not the tree -> record NOTHING AT ALL, so it stays open

"Records nothing" is not laziness, it is the mechanism. A submission is OPEN exactly when no
disposition ref exists for it, so an inconclusive run cannot half-close one by forgetting to unset a
flag: the requeue is the absence of a write. :func:`disposition_of` returns `None` for INCONCLUSIVE
and every writer must consult it, which is a declaration the code cannot drift away from -- unlike a
comment saying the two are different.

A CONFLICT IS AN ORDINARY OUTCOME, NOT AN ERROR. Git detecting that two participants really did
touch the same lines is the system working; it is the reason no path lock is needed anywhere else in
this design. The conflicting submission is recorded, dropped from the batch, and the rest of the
batch proceeds -- because one participant's rebase is not a reason to spend another verdict on
nobody's work. Verdict capacity is the scarce resource: measured 2026-08-12, one full run of the
consuming project's fast tier is 704 s and occupies the whole machine, so roughly 40 per day exist
for every participant combined.

THIS MODULE DOES NOT KNOW WHAT A GATE IS. The verdict function is injected -- it takes a tree sha
and returns one of the three words. A package that reached for a test command would have picked one
consumer's, which is the coupling the rest of this package has already had removed once.

AND IT HAS A RUNNER, WHICH IS THE HALF A MECHANISM IS USELESS WITHOUT. :class:`Integrator` satisfies
`loop.Executor`, so one pass is an ordinary :class:`~agent_swarm.job.Job` of kind `INTEGRATION`,
ranked by `allocator.rank` and claimed by `store.try_claim` like every other job. It runs on ANY
IDLE CAPABLE BOX -- there is no integration host, because a dedicated host is a second scheduler
with one node in it that idles precisely when the fleet is busiest. Nothing new schedules it: the
kind cost this package one enum member and this section, which is `JobKind`'s own stated bar.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
from collections.abc import Callable, Collection, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from agent_swarm import refs
from agent_swarm.job import INTEGRATION, Job
from agent_swarm.refstore import GitRefStore, RefUnreachable
from agent_swarm.shards import FAIL, INCONCLUSIVE, PASS
from agent_swarm.submission import Submission, read

#: A submission's TERMINAL dispositions. Deliberately NOT the verdict words: a verdict is about a
#: tree and a disposition is about a submission, and one batch's PASS disposes of several
#: submissions at once. Reusing the three words would make "which one is this" a guess.
INTEGRATED = 'INTEGRATED'
REJECTED = 'REJECTED'
CONFLICTED = 'CONFLICTED'

#: The name inside a disposition's tree; half of a contract, so it is one constant.
OUTCOME_FILENAME = 'outcome.json'

#: Seconds any one git call may take. A merge and a worktree checkout are the slow ones, and the
#: number is a ceiling on a HANG rather than a measurement of a repository -- a consumer whose trunk
#: is large enough for this to bite passes its own.
GIT_TIMEOUT_S = 600.0


class TrunkMoved(RuntimeError):
    """The trunk was not where the caller expected when the CAS ran.

    NOT AN ERROR IN THE ORDINARY SENSE -- it is the compare-and-swap doing its job, and the caller's
    response is to rebuild the merge on the new trunk. It is an exception rather than a `False`
    because a caller that ignored a boolean would carry on believing its work had landed.
    """


class HeadNotPresent(RuntimeError):
    """A submission names a commit this checkout does not have.

    Raised BEFORE any merging, so a batch cannot half-build a tree that is missing one participant's
    work while reading as complete. The remedy is in the message: fetch the head.
    """


# --------------------------------------------------------------------------- order


def order_key(submission: Submission) -> tuple[int]:
    """The sort key, EXPOSED, because "why is this one ahead of mine" must have an answer.

    ARRIVAL ORDER AND NOTHING ELSE, today. Every richer rule that was considered -- smallest diff
    first, oldest base first, fewest declared paths first -- is a policy that advantages one shape of
    work over another, and none of them can be justified before there is data on what actually
    queues. Arrival order is the one rule that needs no justification and cannot be gamed except by
    submitting earlier, which is the behaviour the system wants anyway.

    A TUPLE, so a second component can be added without every caller's comparison changing shape.
    """
    return (submission.ordinal,)


def queue(submissions: Iterable[Submission]) -> tuple[Submission, ...]:
    """The batch, in the order it will be merged. Deterministic for a given set, by construction."""
    return tuple(sorted(submissions, key=order_key))


def order_explanation(submissions: Iterable[Submission]) -> tuple[str, ...]:
    """One line per position, naming the KEY that put it there. Data, not a heuristic in a loop.

    It exists so the answer to "why is this one first" is generated by the same function that does
    the ordering. An explanation written separately is a second implementation of the rule, and it
    would keep explaining the old rule after the sort changed.
    """
    return tuple(
        f'{position}. submission {sub.ordinal} ({sub.participant}) -- key {order_key(sub)}, arrival order'
        for position, sub in enumerate(queue(submissions), start=1)
    )


# --------------------------------------------------------------------------- what is still open


def disposition_of(verdict: str) -> str | None:
    """The TERMINAL disposition a verdict implies, or `None` when it implies none.

    THE `None` IS THE FEATURE, and every writer consults this rather than testing the words itself.
    INCONCLUSIVE says the RUN could not answer -- a dead worker, a box that ran out of memory, a
    vendor tool that was not licensed today -- which is not a fact about the submission and must
    never be recorded as one. Recording it would reject work for the sin of having been scheduled on
    a broken machine, and the rejection would be indistinguishable, afterwards, from a real failure.

    Raises:
        ValueError: `verdict` is not one of the three words. A fourth value is a caller and this
            package disagreeing about the vocabulary, which cannot be resolved by guessing.
    """
    if verdict == PASS:
        return INTEGRATED
    if verdict == FAIL:
        return REJECTED
    if verdict == INCONCLUSIVE:
        return None
    msg = f'not a verdict: {verdict!r} (expected {PASS}, {FAIL} or {INCONCLUSIVE})'
    raise ValueError(msg)


def disposed_ordinals(store: GitRefStore) -> frozenset[int]:
    """Every ordinal that already has a terminal disposition."""
    found = store.list(refs.outcome_glob())
    return frozenset(o for ref in found if (o := refs.outcome_ordinal(ref)) is not None)


def open_ordinals(store: GitRefStore) -> tuple[int, ...]:
    """Submitted, and not yet disposed of. ASCENDING.

    OPEN IS THE ABSENCE OF A DISPOSITION, computed from two listings rather than held as a state
    somebody sets. There is no "in progress" value to be left behind by a participant that died
    mid-batch -- which is the state a queue with a status field always ends up leaking.
    """
    disposed = disposed_ordinals(store)
    submitted = store.list(refs.submission_glob())
    ordinals = {o for ref in submitted if (o := refs.submission_ordinal(ref)) is not None}
    return tuple(sorted(ordinals - disposed))


def open_submissions(store: GitRefStore) -> tuple[Submission, ...]:
    """Every open submission, read back and QUEUED. One round trip per submission, deliberately:
    the payload is what the merge needs and there is no listing that carries it."""
    return queue(read(store, ordinal) for ordinal in open_ordinals(store))


def record(store: GitRefStore, submission: Submission, disposition: str, detail: str) -> bool:
    """Write a submission's TERMINAL disposition. Returns whether it landed.

    NOT FORCED, like a submission is not forced: a disposition is written once, and a second
    integrator reaching the same submission is REFUSED by the remote rather than overwriting a
    decision that may already have advanced the trunk.

    Raises:
        ValueError: `disposition` is not terminal. In particular `None` -- what
            :func:`disposition_of` returns for INCONCLUSIVE -- cannot be passed here by accident,
            so the requeue path cannot close a submission through a typo.
    """
    if disposition not in (INTEGRATED, REJECTED, CONFLICTED):
        msg = f'not a terminal disposition: {disposition!r} (an INCONCLUSIVE run records NOTHING)'
        raise ValueError(msg)
    payload = {'ordinal': submission.ordinal, 'disposition': disposition, 'detail': detail}
    blob = store.stdin_text(json.dumps(payload, indent=2), 'hash-object', '-w', '--stdin')
    tree = store.stdin_text(f'100644 blob {blob}\t{OUTCOME_FILENAME}\n', 'mktree')
    commit = store.text('commit-tree', tree, '-m', f'{disposition} {submission.ordinal}')
    ref = refs.outcome_ref(submission.ordinal)
    return store.run('push', store.remote, f'{commit}:{ref}', timeout=GIT_TIMEOUT_S).returncode == 0


# --------------------------------------------------------------------------- the real merged tree


@dataclass(frozen=True, slots=True)
class Conflict:
    """One submission git could not merge, and the paths it named. Ordinary, not exceptional."""

    submission: Submission
    paths: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Merge:
    """A tree that really exists, and the exact list of submissions that are in it.

    BOTH SHAS ARE CARRIED because they answer different questions. `tree` identifies the CONTENT and
    is what a verdict is about -- two different merge commits of the same content deserve one
    verdict, not two, and verdict capacity is the scarce resource. `commit` is what the trunk is
    advanced TO, since a branch points at a commit and the history is what a human reads later.
    """

    tree: str
    commit: str
    base: str
    merged: tuple[Submission, ...]
    conflicts: tuple[Conflict, ...]

    def is_empty(self) -> bool:
        """Did anything actually merge? A batch in which every submission conflicted produces a tree
        identical to the trunk, and spending a verdict on it would buy nothing."""
        return not self.merged


@contextlib.contextmanager
def isolated_worktree(store: GitRefStore, commit: str, path: Path) -> Iterator[Path]:
    """A detached worktree at `commit`, removed on the way out however that happens.

    A WORKTREE RATHER THAN THE CHECKOUT ITSELF, and this is not tidiness. The checkout belongs to
    whoever is working in it -- very possibly a human, mid-edit, whose typing this system is built
    to tolerate rather than to see. Merging in it would destroy uncommitted work, and a `--abort`
    on the conflict path would destroy it while reading as a clean recovery.

    THE REMOVAL IS `--force` AND IN A `finally`. A conflicted merge leaves the worktree dirty, so a
    polite removal refuses exactly on the path this exists to survive, and a leaked worktree is a
    permanent entry in the checkout's administrative files that later prunes trip over.
    """
    added = store.run('worktree', 'add', '--detach', str(path), commit, timeout=GIT_TIMEOUT_S)
    if added.returncode != 0:
        msg = f'cannot create a worktree at {path}: {added.stderr.strip() or "(nothing on stderr)"}'
        raise RefUnreachable(msg)
    try:
        yield path
    finally:
        store.run('worktree', 'remove', '--force', str(path), timeout=GIT_TIMEOUT_S)


def build_merge(
    store: GitRefStore,
    *,
    trunk_commit: str,
    submissions: Sequence[Submission],
    workdir: Path,
    timeout_s: float = GIT_TIMEOUT_S,
) -> Merge:
    """Merge `submissions` onto `trunk_commit` FOR REAL, and return the tree that resulted.

    THE ORDER IS :func:`queue`'s, applied here rather than assumed of the caller -- a caller that
    passed an arbitrary sequence would get a tree whose identity depended on dict iteration order,
    and two integrators would then spend two verdicts on the same content.

    EVERY HEAD IS CHECKED BEFORE ANYTHING IS MERGED. A missing object discovered halfway through
    leaves a tree containing some participants' work and not others', which is the one outcome a
    merged tree must never be: it would be judged, and it would pass or fail on behalf of work that
    was not in it.

    Raises:
        HeadNotPresent: a submission's head is not in this object database.
        RefUnreachable: the worktree could not be created.
    """
    ordered = queue(submissions)
    missing = [
        s for s in ordered if store.run('cat-file', '-e', f'{s.head}^{{commit}}', timeout=timeout_s).returncode != 0
    ]
    if missing:
        named = ', '.join(f'{s.ordinal}:{s.head[:12]}' for s in missing)
        msg = f'these submission heads are not in {store.root}; fetch them before merging: {named}'
        raise HeadNotPresent(msg)

    merged: list[Submission] = []
    conflicts: list[Conflict] = []
    with isolated_worktree(store, trunk_commit, workdir) as tree_root:
        for sub in ordered:
            attempt = store.run(
                'merge',
                '--no-ff',
                '--no-edit',
                '-m',
                f'integrate submission {sub.ordinal} ({sub.participant})',
                sub.head,
                cwd=tree_root,
                timeout=timeout_s,
            )
            if attempt.returncode == 0:
                merged.append(sub)
                continue
            # ORDINARY, NOT EXCEPTIONAL. The paths are read BEFORE the abort, because aborting is
            # what removes the index entries that name them -- afterwards there is nothing to report
            # and the participant is told only that "something" clashed.
            unmerged = store.run(
                'diff', '--name-only', '--diff-filter=U', cwd=tree_root, timeout=timeout_s
            ).stdout.split()
            store.run('merge', '--abort', cwd=tree_root, timeout=timeout_s)
            conflicts.append(Conflict(submission=sub, paths=tuple(unmerged)))

        head = store.text('rev-parse', 'HEAD', cwd=tree_root)
        tree = store.text('rev-parse', 'HEAD^{tree}', cwd=tree_root)

    return Merge(tree=tree, commit=head, base=trunk_commit, merged=tuple(merged), conflicts=tuple(conflicts))


# --------------------------------------------------------------------------- the advance


def trunk_commit(store: GitRefStore, trunk: str, *, timeout_s: float = GIT_TIMEOUT_S) -> str:
    """Where `trunk` points right now, in this checkout."""
    out = store.run('rev-parse', f'refs/heads/{trunk}', timeout=timeout_s)
    if out.returncode != 0:
        msg = f'no such branch {trunk!r} in {store.root}: {out.stderr.strip() or "(nothing on stderr)"}'
        raise RefUnreachable(msg)
    return out.stdout.strip()


def advance(store: GitRefStore, *, trunk: str, expected: str, new: str, timeout_s: float = GIT_TIMEOUT_S) -> None:
    """Move `trunk` from `expected` to `new`, ATOMICALLY, or refuse.

    `git update-ref <ref> <new> <old>` IS the compare-and-swap: git verifies the old value and
    swaps under its own ref lock, in one operation. There is no lock to build here and no
    read-then-write to get right -- both of which would be strictly worse versions of a thing the
    tool already does correctly, and both of which have a window this does not.

    IT IS ALSO WHY THE MERGE MAY BE THROWN AWAY. If the trunk moved while the batch was being
    judged, the tree that was judged is not the tree that would result now, and the only honest
    response is to rebuild and judge again. Advancing anyway would put an unjudged tree on the
    trunk, which is the single thing this whole module exists to prevent.

    Raises:
        TrunkMoved: the trunk was not at `expected`.
    """
    out = store.run('update-ref', f'refs/heads/{trunk}', new, expected, timeout=timeout_s)
    if out.returncode != 0:
        msg = (
            f'{trunk} was not at {expected[:12]} when the swap ran, so the judged tree is not the '
            f'tree that would result now: {out.stderr.strip() or "(nothing on stderr)"}'
        )
        raise TrunkMoved(msg)


# --------------------------------------------------------------------------- one pass


@dataclass(frozen=True, slots=True)
class Integration:
    """What one pass did. Every submission in the batch appears in exactly one of the three lists.

    `requeued` IS SEPARATE FROM `rejected` AND CARRIES NO DISPOSITION. Nothing was written for
    those submissions, so they are open on the next pass by construction rather than by anybody
    putting them back.
    """

    merge: Merge
    verdict: str
    advanced: bool
    integrated: tuple[Submission, ...]
    rejected: tuple[Submission, ...]
    requeued: tuple[Submission, ...]
    conflicts: tuple[Conflict, ...]


def integrate(
    store: GitRefStore,
    *,
    trunk: str,
    submissions: Sequence[Submission],
    verdict_of: Callable[[str], str],
    workdir: Path,
    timeout_s: float = GIT_TIMEOUT_S,
) -> Integration:
    """One pass: merge the batch, judge THAT tree, and advance the trunk if it passed.

    `verdict_of` RECEIVES THE TREE SHA, not a branch, not a directory, not a submission. That
    argument is the whole contract with the consumer: whatever it runs, it must run against those
    exact bytes, and what it returns is a claim about them and nothing else.

    THE TRUNK IS RE-READ AT THE SWAP rather than trusted from before the verdict. A verdict takes
    minutes and the trunk is shared; the CAS is the only thing that can say whether it moved, so
    the value it compares against is the one measured at merge time -- the tree that was actually
    judged -- and not a hopeful re-read that would compare the trunk against itself.

    A CONFLICTED SUBMISSION IS DISPOSED OF, TERMINALLY, and the participant rebases and submits
    again. The alternative -- keeping it open -- reads better and behaves worse: it would conflict
    identically on every subsequent pass, consuming an attempt each time, until a human noticed
    that a queue was silently stuck on one participant's stale base.

    Raises:
        HeadNotPresent: a submission's head is missing; nothing is merged and nothing is recorded.
        TrunkMoved: the trunk moved under a PASS. Nothing is recorded in that case either, so the
            whole batch is still open and the next pass judges the tree that now exists.
        ValueError: `verdict_of` returned something that is not one of the three verdict words.
    """
    at_merge = trunk_commit(store, trunk, timeout_s=timeout_s)
    merge = build_merge(store, trunk_commit=at_merge, submissions=submissions, workdir=workdir, timeout_s=timeout_s)
    for conflict in merge.conflicts:
        record(store, conflict.submission, CONFLICTED, f'conflicted on: {", ".join(conflict.paths) or "(no paths)"}')

    if merge.is_empty():
        # NOTHING MERGED, SO THERE IS NOTHING TO JUDGE. Calling the verdict function here would
        # spend one of the day's ~40 runs re-confirming the trunk, and would report a PASS that a
        # reader could mistake for a statement about the submissions in the batch.
        return Integration(
            merge=merge,
            verdict=INCONCLUSIVE,
            advanced=False,
            integrated=(),
            rejected=(),
            requeued=(),
            conflicts=merge.conflicts,
        )

    verdict = verdict_of(merge.tree)
    disposition = disposition_of(verdict)

    if disposition is None:
        # THE REQUEUE, AND IT IS AN ABSENCE. No ref is written, so these submissions are open on the
        # next pass without anybody putting them back -- see this module's docstring.
        return Integration(
            merge=merge,
            verdict=verdict,
            advanced=False,
            integrated=(),
            rejected=(),
            requeued=merge.merged,
            conflicts=merge.conflicts,
        )

    if disposition == REJECTED:
        for sub in merge.merged:
            record(store, sub, REJECTED, f'the merged tree {merge.tree[:12]} was judged {verdict}')
        return Integration(
            merge=merge,
            verdict=verdict,
            advanced=False,
            integrated=(),
            rejected=merge.merged,
            requeued=(),
            conflicts=merge.conflicts,
        )

    # PASS. THE SWAP FIRST, THE DISPOSITIONS AFTER: if the trunk moved, `TrunkMoved` leaves every
    # submission open, which is the recoverable order. Recording first would close submissions whose
    # work never landed anywhere.
    advance(store, trunk=trunk, expected=at_merge, new=merge.commit, timeout_s=timeout_s)
    for sub in merge.merged:
        record(store, sub, INTEGRATED, f'landed on {trunk} as {merge.commit[:12]}')
    return Integration(
        merge=merge,
        verdict=verdict,
        advanced=True,
        integrated=merge.merged,
        rejected=(),
        requeued=(),
        conflicts=merge.conflicts,
    )


# --------------------------------------------------------------------------- the runner


#: What a box must DECLARE to be offered integration work. An opaque token, compared for equality
#: exactly as `pull.Workbench`'s capabilities are -- this package does not know what it means. What
#: the consumer must ensure it means is: a checkout of the trunk, and the right to push it.
INTEGRATOR_CAPABILITY = 'integrator'


class NotAnIntegrator(RuntimeError):
    """This box was asked to be an integrator and does not declare that it can be one.

    RAISED AT CONSTRUCTION, BEFORE ANY CLAIM, and that placement is the whole point. Refusing later
    -- inside `execute` -- would be caught by `loop._run`'s executor handler and recorded as
    INCONCLUSIVE, so a permanently incapable box would take the job, say nothing a reader could act
    on, and take it again next tick. A box that cannot serve this kind must SAY SO rather than
    silently take nothing, which is `pull.MissingCapability`'s reason with the same shape.
    """


def integrator_blockers(capabilities: Collection[str]) -> list[str]:
    """Every reason this box may not integrate. Empty means it may.

    A LIST FOR `Box.blockers`' REASON -- a refusal naming one of several sends the reader to fix the
    wrong thing -- and it is a plain function so a scheduler can ask BEFORE it constructs anything.
    """
    if INTEGRATOR_CAPABILITY in set(capabilities):
        return []
    return [
        f'this box does not declare {INTEGRATOR_CAPABILITY!r}, so it has no checkout of the trunk '
        f'to merge in and no right to push it; declared: {sorted(capabilities) or "(nothing)"}'
    ]


def batch_key(trunk: str, ordinals: Iterable[int]) -> str:
    """The job id for one pass: the trunk, plus a digest of EXACTLY the submissions it would merge.

    WHY NOT SIMPLY THE TRUNK NAME, which is the obvious spelling and is wrong in a way that stalls
    silently. `loop.run_one` will not re-run a job that already has a verdict -- that is what makes
    a job DONE rather than merely unclaimed -- so an id of `main` would be answered by the first
    pass and never scheduled again. Integration is not one job; it is one job PER BATCH.

    THE THREE OUTCOMES THEN LAND WHERE THEY BELONG, without a retry rule written anywhere:

    * PASS      -- the batch is disposed of, the open set shrinks, the next pass has a NEW id.
    * FAIL      -- the batch is disposed of REJECTED, the open set shrinks, likewise a new id.
    * INCONCLUSIVE -- nothing was recorded, so the open set is unchanged and the id is the SAME.
      The pass is retried exactly as `admission.should_retry` prices it, which is the one case where
      retrying the identical work is the correct thing to do.

    THE ORDINALS ARE SORTED AND DEDUPLICATED, so two boxes that listed the queue in different orders
    compute one id and therefore contend for one claim rather than doing the work twice.

    Raises:
        ValueError: the batch is empty. There is no such pass -- a job that merges nothing would
            spend a claim and a scheduling slot to re-confirm the trunk, and `integrate` already
            refuses to spend a VERDICT on it for the same reason.
    """
    unique = sorted(set(ordinals))
    if not unique:
        msg = f'there is no integration pass for {trunk!r} with no open submission'
        raise ValueError(msg)
    digest = hashlib.sha256(','.join(str(o) for o in unique).encode()).hexdigest()[:12]
    return f'{trunk}@{digest}'


def integration_job(
    trunk: str,
    ordinals: Iterable[int],
    *,
    ram_gib: float | None = None,
    exclusivity: str | None = None,
    solo_seconds: float | None = None,
    ceiling_seconds: float | None = None,
) -> Job:
    """One schedulable integration pass. An ORDINARY job; nothing about it is special-cased.

    THE COST IS THE CALLER'S TO STATE, and it is unpriced by default rather than free: a pass runs
    the consumer's whole gate on the merged tree, so its RAM and duration are the gate's numbers and
    this package has never measured them. `Job` treats `None` as "nobody measured", which keeps it
    schedulable; a zero would mean "free" and is the safe-looking answer that is exactly wrong.

    `exclusivity` DEFAULTS TO `Job`'s OWN DEFAULT (the whole box), which is right for a pass that
    runs a full gate, and is left overridable because how wide the consumer's gate is, is not a fact
    this module has.
    """
    kwargs = {} if exclusivity is None else {'exclusivity': exclusivity}
    return Job(
        id=batch_key(trunk, ordinals),
        kind=INTEGRATION,
        ram_gib=ram_gib,
        solo_seconds=solo_seconds,
        ceiling_seconds=ceiling_seconds,
        **kwargs,  # type: ignore[arg-type]
    )


@dataclass(frozen=True)
class Integrator:
    """A `loop.Executor` that advances ONE trunk. Scheduled, claimed and released like anything else.

    EXCLUSIVITY HERE BUYS VERDICT CAPACITY, NOT CORRECTNESS -- and stating which is the point, because
    the two call for opposite code. Correctness is already unconditional: `advance` is
    `git update-ref <ref> <new> <old>`, a compare-and-swap under git's own ref lock, so a second
    integrator that merged and judged the same batch either finds the trunk where it left it (and
    advances a tree that really was judged) or raises `TrunkMoved` and lands nothing. **A lost race
    is harmless.** What a duplicate pass DOES cost is a run of the consumer's gate -- measured
    2026-08-12 in the first consumer, 704 s occupying a whole machine, so roughly 40 exist per day
    for the entire fleet -- which is the scarcest thing here.

    SO THERE IS NO LOCK IN THIS FILE. The `claim_key` of an `INTEGRATION` job is
    `integration/<trunk>@<batch digest>`, and `store.try_claim`'s existing compare-and-swap is what
    keeps two boxes with the same view of the queue off the same batch. That is best-effort by
    construction: two boxes whose listings disagree compute different digests and both run. Adding a
    real lock to close that gap would be the safe-looking mistake -- it would serialise a plane whose
    concurrency is the reason the fan-out exists, to prevent a duplicate that costs one run and
    corrupts nothing, and it would be a second, weaker arbitration beside a correct one.

    Attributes:
        store: the checkout this box will merge in and push from.
        trunk: the branch this integrator advances. ONE branch per integrator, checked in `execute`.
        verdict_of: takes the merged TREE sha, returns one of the three verdict words.
        workdir: where the throwaway merge worktree is built.
        capabilities: what this box declares. Refused at construction; see :class:`NotAnIntegrator`.
        timeout_s: ceiling on any one git call.
    """

    store: GitRefStore
    trunk: str
    verdict_of: Callable[[str], str]
    workdir: Path
    capabilities: frozenset[str] = field(default_factory=frozenset)
    timeout_s: float = GIT_TIMEOUT_S

    def __post_init__(self) -> None:
        blockers = integrator_blockers(self.capabilities)
        if blockers:
            raise NotAnIntegrator('; '.join(blockers))
        object.__setattr__(self, 'capabilities', frozenset(self.capabilities))

    def execute(self, job: Job) -> tuple[str, str]:
        """Run the pass `job` names, and answer in the verdict vocabulary. Never raises for a race.

        THE BATCH IS RE-READ AND THE ID RE-DERIVED. A job carries no payload, so what it names is a
        batch by digest; if the queue moved between scheduling and here, this box was asked a
        question it can no longer answer and the honest word is INCONCLUSIVE -- integrating the NEW
        batch instead would record a verdict under an id describing different work, which is a
        declaration that lies with a content address on it.

        THE MAPPING FROM A PASS TO A VERDICT, and each leg is about the TREE and not about the pass:

        * advanced             -> PASS. The judged tree is on the trunk.
        * anything REJECTED    -> FAIL. The merged tree was judged bad; the batch is disposed of.
        * requeued, or nothing merged, or the trunk moved, or a head is missing -> INCONCLUSIVE.
          None of those is a fact about the code, which is exactly what the third word is for.

        `TrunkMoved` AND `HeadNotPresent` ARE CAUGHT BY NAME, not by a broad handler: both are
        ordinary races with a specific answer, and letting them reach `loop._run`'s catch would
        record the same INCONCLUSIVE while reading, to anyone looking, as a box that fell over.
        Everything else propagates.

        Raises:
            ValueError: `job` is not an INTEGRATION job for this integrator's trunk. A caller and
                this object disagreeing about which branch is being advanced cannot be resolved by
                guessing, and guessing would push a judged tree onto the wrong trunk.
        """
        if job.kind is not INTEGRATION:
            msg = f'{job.claim_key()} is not an {INTEGRATION.value} job'
            raise ValueError(msg)
        if not job.id.startswith(f'{self.trunk}@'):
            msg = f'{job.id!r} does not name a pass over {self.trunk!r}; this integrator advances one trunk'
            raise ValueError(msg)

        submissions = open_submissions(self.store)
        if not submissions:
            return INCONCLUSIVE, f'nothing is open on {self.trunk} any more; the batch was taken by another pass'
        now = batch_key(self.trunk, (s.ordinal for s in submissions))
        if now != job.id:
            return INCONCLUSIVE, f'the queue moved: this box was scheduled for {job.id}, the open batch is now {now}'

        try:
            done = integrate(
                self.store,
                trunk=self.trunk,
                submissions=submissions,
                verdict_of=self.verdict_of,
                workdir=self.workdir,
                timeout_s=self.timeout_s,
            )
        except TrunkMoved as exc:
            return INCONCLUSIVE, f'another integrator advanced {self.trunk} first, so nothing was landed: {exc}'
        except HeadNotPresent as exc:
            return INCONCLUSIVE, f'this checkout is missing a submitted head, so no tree was built: {exc}'

        landed = ', '.join(str(s.ordinal) for s in done.integrated) or '(none)'
        if done.advanced:
            return PASS, f'{self.trunk} advanced to {done.merge.commit[:12]} carrying submission(s) {landed}'
        if done.rejected:
            spurned = ', '.join(str(s.ordinal) for s in done.rejected)
            return FAIL, f'the merged tree {done.merge.tree[:12]} was judged {done.verdict}; rejected {spurned}'
        return INCONCLUSIVE, f'nothing landed: verdict {done.verdict} on {done.merge.tree[:12]}'
