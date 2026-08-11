"""ADMISSION: who may run what, right now. The decision half of the swarm job layer.

WHAT THIS IS. `.claude/memory/2026/08/09/design-final-architecture-collaboration-and-test-system-unified.md`
places admission in L1 (swarm), between fabric's capacity FACTS and the project's gate:

    All dynamic admission lives in ONE swarm component, generalised from ci_tick's existing
    class-aware lock + M3 memory pricing: "spawn a claude (~200 MB)" and "run a jmag tier
    (exclusive vendor class + measured 667 s shared)" are two classes of the same admission
    question. Do not build a second scheduler.

...and M1's first block is the extraction itself: "extract ci_tick's admission + claim protocol
into the swarm job layer, then make the LLM worker and the deterministic runner two executors of
it." This module is that extraction, done in place -- the day the swarm library exists, moving it
is a `git mv`, because it imports NOTHING but the standard library. That property is guarded by
`tests/unit/scripts/test_ci_admission_is_swarm_shaped.py`, and it is guarded as a DEPENDENCY test
rather than a line count: what makes a layer real is what it is ALLOWED TO KNOW.

DECISIONS ONLY, NO I/O. Every function here takes plain values and returns a reason string or a
bool. The half that touches git, the filesystem and psutil -- `claim`, `class_lock`,
`live_classes`, `available_ram_gib`, `commits_behind_main` -- is a STORE ADAPTER concern and stays
in `ci_tick` until it has somewhere to go. This module does not pretend the layer is fully moved.

IT ALSO ENDS A DUPLICATED SCHEME. `classes_conflict` and the class vocabulary existed TWICE, in
`ci_tick.py` and in `scripts/gate/exclusive.py`, with the same behaviour and two spellings of the
class set (`KNOWN_CLASSES` / `KNOWN_CLASSES`). Two modules deciding "may these two jobs run
together" are two modules free to drift about the one thing admission decides. One home now; both
consume it.

WHY `scripts/swarm/` AND NOT `scripts/ci/`. Both `ci/` and `gate/` consume this. Filing it under
either would invert the intended layering -- the gate would be importing the scheduler's private
module. The directory names the destination layer so the eventual move is a directory move.
"""

from __future__ import annotations

#: The whole-box class. Named `expensive` because that is what `ci/policy.toml` already says and
#: what `output/.expensive.lock` is already called; renaming it would be a migration with no payoff.
WHOLE_BOX = 'expensive'

#: Parse/scan tiers. Bounded by capacity (M3), not by exclusion.
CHEAP = 'cheap'

#: The VENDOR FORM: `vendor:<name>`, a class whose jobs share one bounded external resource -- one
#: installation, one licence server, one COM object. Two jobs of the SAME vendor collide over it;
#: two of DIFFERENT vendors do not, which `fanout.md` records as a user directive ("FEMM and JMAG DO
#: run concurrently") that nothing enforced.
VENDOR_PREFIX = 'vendor:'


def is_known_class(cls: str | None) -> bool:
    """Is ``cls`` a class this layer can reason about? THE ONE PREDICATE; there is no class LIST.

    A SHAPE RULE, NOT AN ENUMERATION, and that is the whole point. This vocabulary was a frozenset
    naming `vendor:femm` and `vendor:jmag` -- two tools belonging to ONE consumer, hard-coded into
    vendor-neutral fleet infrastructure. It is the same coupling `default_forge`'s `DEFAULT_REPO`
    had, and it was invisible for the same reason: it WORKED, for that one project. Infrastructure
    supplies the MECHANISM -- "a vendor class monopolises something and is exclusive with itself" --
    and takes WHICH vendors exist as DATA from the caller, which is where it already lived
    (a consumer's own `policy.toml` says `class = "vendor:femm"`).

    So there is no `KNOWN_CLASSES` to import and nothing to keep in step: a caller that wants to
    enumerate enumerates what IT declared. Adding a vendor is a line of the consumer's config, never
    a release of this package.

    AN EMPTY NAME IS REFUSED, and this is a correctness rule rather than tidiness. `'vendor:'`
    accepted would make every typo'd class the SAME class, so unrelated jobs would serialise on one
    exclusivity slot -- a HANG, which is the failure mode you cannot read off a log. Refused, it
    falls to default-deny like any other nonsense: conservative, and visible as a class nobody
    recognises rather than as a fleet that got slower.

    THE `femm`/`jmag` MENTIONS ELSEWHERE IN THIS MODULE ARE MEASUREMENTS -- "jmag takes 356 s solo",
    "667 s shared" -- and they STAY. They record where a number came from, and a number whose
    provenance is deleted is a number nobody can re-take. Only the EXECUTABLE vocabulary must stop
    naming a consumer's tools; do not "finish the job" by scrubbing the prose.
    """
    if cls in (WHOLE_BOX, CHEAP):
        return True
    return bool(cls) and cls.startswith(VENDOR_PREFIX) and len(cls) > len(VENDOR_PREFIX)


def classes_conflict(a: str | None, b: str | None) -> bool:
    """May a job of class ``a`` and one of class ``b`` run at the same time on one box?

    THE ASYMMETRY IS THE DESIGN, which is why this is a relation and not a lock name.
    ``expensive`` means "the whole box", so it conflicts with EVERYTHING including the cheap tiers;
    two ``cheap`` jobs conflict with nothing; two DIFFERENT vendors conflict not at all. A naive
    one-lock-per-class would let an expensive gate run beside a cheap one and starve both -- the
    measurement `[runner] min_ram_gib` was priced from.

    DEFAULT-DENY. Anything unrecognised, empty or ``None`` is treated as the whole box: the
    alternative is that a typo in policy silently grants a job the right to run beside everything,
    and that failure would show up as two gates starving each other rather than as a config error.

    The relation is SYMMETRIC by construction and pinned as such -- an order-dependent answer would
    mean overlap depends on which job arrived first, a race visible only under load.
    """
    ca = a if is_known_class(a) else WHOLE_BOX
    cb = b if is_known_class(b) else WHOLE_BOX
    if WHOLE_BOX in (ca, cb):
        return True
    if ca.startswith(VENDOR_PREFIX) or cb.startswith(VENDOR_PREFIX):
        return ca == cb  # same vendor collides over its one install; different vendors do not
    return False  # cheap vs cheap


def admission_blockers(held: dict[str, bool], want: str) -> list[str]:
    """Which LIVE holders conflict with starting a ``want`` job. Empty means "may start".

    Args:
        held: ``{class: is_the_holder_alive}`` for every existing class lock.
        want: the class of the job asking to start.

    THE DIRECTION OF DANGER IS ASYMMETRIC and this function is written to that. Refusing a job that
    could have run costs throughput and self-corrects -- the next tick retries. ADMITTING one that
    should have been refused puts two 16-worker gates on a box measured to hold one, and
    `[runner] min_ram_gib` records the result: they starve each other and BOTH verdicts are lost.
    So every ambiguity resolves to blocked, via `classes_conflict`'s default-deny.

    A DEAD HOLDER DOES NOT BLOCK. Lock files outlive their processes -- a killed gate leaves one
    behind, and one was killed in this repo today. Liveness is the caller's to determine
    (`exclusive.read_owner` already does it via PID and boot epoch); trusting mere existence would
    wedge a class until someone deleted a file by hand.

    EVERY conflicting holder is returned, not the first: a refusal naming one of three reasons
    sends the reader to fix the wrong thing.

    """
    return sorted(cls for cls, alive in held.items() if alive and classes_conflict(cls, want))


#: What an UNPRICED job is assumed to need, in GiB. Not infinity -- default-deny must not become
#: "never run anything nobody measured", which would stop the fleet on a genuinely idle box. This is
#: the whole-box figure actually observed: 12.32 GB across 40 processes during a live `fast` gate on
#: 2026-08-09. A job that declares its own cost overrides it; this is the fallback, and it is a
#: MEASUREMENT rather than a round number so that replacing it requires taking another one.
_UNPRICED_JOB_GIB = 12.5

#: Headroom left for the OS, this tick's own git subprocesses, and the in-run growth measured on
#: 2026-08-09 (peak worker 541 MB -> 1062 MB within six minutes).
_CAPACITY_RESERVE_GIB = 2.0


def capacity_blocker(available_gib: float | None, want_gib: float | None, reserve_gib: float) -> str | None:
    """Why this job does not fit right now, or ``None`` if it does.

    WHAT `[runner] min_ram_gib` CANNOT ANSWER. That key is an admission FLOOR -- "may this machine
    take expensive work at all" -- a constant compared against a constant. MEASURED 2026-08-09 on a
    live unattended run: 12.32 GB across 40 processes with **0.33 GB free**, peak worker climbing
    541 MB -> 1062 MB within six minutes. The floor was satisfied throughout (the box has
    31.71 GiB) while the run walked to the edge of memory. Once M1 lets classes overlap, that edge
    becomes reachable by ADDING a job rather than by one job growing.

    THE ASYMMETRY, again. Refusing a job that would have fitted costs throughput and self-corrects
    next tick. Admitting one that does not fit kills a worker, which `gate.py` reads as `node down`
    and reports INCONCLUSIVE -- so the price is a LOST RUN, not a wrong verdict: tens of minutes of
    machine time, and on the unattended path a group that stays unanswered. Every ambiguity refuses.

    THE RESERVE IS DECLARED, NOT EMERGENT. Fitting a job into the last byte leaves nothing for the
    OS, for the git subprocesses this tick itself spawns, or for the in-run growth measured above.
    "It happened to fit last time" is precisely the reasoning that 0.33 GB sample refutes.
    """
    if available_gib is None:
        return 'cannot read available memory; refusing rather than promising a fit'
    need = _UNPRICED_JOB_GIB if want_gib is None else want_gib
    if available_gib - need < reserve_gib:
        priced = 'unpriced, assumed' if want_gib is None else 'declared'
        return (
            f'needs {need:.1f} GiB ({priced}) plus {reserve_gib:.1f} GiB reserve, '
            f'but only {available_gib:.1f} GiB is available'
        )
    return None


#: How much slower a job runs when it shares the box, as a MEASUREMENT: jmag takes 356 s solo and
#: was still running past 667 s beside femm when it hit its per-test ceiling -- at least 1.9x. One
#: pair, one box, once. Declared here so replacing it requires taking another measurement rather
#: than editing a number at a call site.
SHARED_SLOWDOWN = 1.9


def time_blocker(solo_s: float | None, ceiling_s: float | None, *, sharing: bool) -> str | None:
    """Why co-scheduling this job would push it past its own ceiling, or ``None``.

    THE GAP THIS CLOSES, and the feature that created it was the one shipped an hour earlier.
    Making `femm` and `jmag` genuinely overlap immediately produced an INCONCLUSIVE: jmag runs solo
    in 356 s and hit its per-test `+++ Timeout +++` beside femm. `fanout.md` already names the
    consequence -- "a test already near its ceiling ... DOES void verdicts" -- and M3 priced MEMORY
    and stopped. A timeout is equally a property of a DEDICATED box.

    REFUSE, DO NOT RAISE THE CEILING. Raising jmag's timeout to cover a shared box weakens it for
    the solo case too, which is how a timeout stops catching a hang; `AGENTS.md` forbids answering a
    budget problem with a bigger constant. Refusing costs throughput and self-corrects next tick.

    RUNNING ALONE IS NEVER BLOCKED HERE, even by a job that would exceed its ceiling anyway: that is
    the gate's timeout to report, and pre-empting it would hide a real regression behind a
    scheduling refusal.

    UNPRICED IS ALLOWED TO SHARE -- the opposite of the memory rule, deliberately. There, guessing
    wrong puts two heavy runs on one box; here it wastes one run. Refusing every unmeasured job
    would stop the fleet co-scheduling anything new, and those runs are how a duration gets measured
    in the first place.
    """
    if not sharing or solo_s is None or ceiling_s is None:
        return None
    projected = solo_s * SHARED_SLOWDOWN
    if projected >= ceiling_s:
        return (
            f'would run ~{projected:.0f}s shared ({solo_s:.0f}s solo x {SHARED_SLOWDOWN} measured '
            f'slowdown) against a {ceiling_s:.0f}s ceiling'
        )
    return None


#: A RESULT about the code. Only these may occupy the answer slot.
_ANSWERS = frozenset({'PASS', 'FAIL'})

#: How many INCONCLUSIVE attempts a job may burn before the scheduler stops picking it. A CEILING,
#: not a preference: without one, a candidate whose tree kills a worker every time is picked on
#: every tick forever, burning the box while the queue behind it starves.
_DEFAULT_MAX_RETRIES = 3


def should_retry(results: list[str], max_retries: int) -> bool:
    """Should this job be attempted (again)? Only INCONCLUSIVE earns a retry, and only so many.

    THE TWO ARMS ARE DIFFERENT QUESTIONS. INCONCLUSIVE is INFRASTRUCTURE -- a dead xdist worker, a
    memory kill, a truncated log -- and none of those says anything about the code, so a human
    retrying by hand is pure waste. A FAIL is an ANSWER: retrying a red until it goes green is the
    unearned-green machine this repo's whole verdict design refuses, so the retry arm never touches
    it.

    THE BOUND IS THE POINT, NOT THE RETRY. An unbounded loop is how a broken box looks busy forever
    -- it burns the fleet, and every wasted run makes the queue behind it staler while the status
    output reads "working". Exhausting the budget is a reportable state, not a quieter loop.

    THE LAST ATTEMPT GOVERNS: an old INCONCLUSIVE must not keep a job retrying after it has since
    answered.
    """
    if results and results[-1] in _ANSWERS:
        return False
    # `max(1, ...)`: a job is always worth ONE attempt, so `max_retries = 0` means "try once, then
    # stop" rather than "never run". Only the wasted attempts count against the budget -- an
    # answered run is not waste, and charging the budget for it would starve a job that had one
    # good run and one bad night.
    wasted = sum(1 for r in results if r not in _ANSWERS)
    return wasted < max(1, max_retries)


#: The shard half of a claim key: `s<index>of<width>`. **THE ONE SPELLING.**
#:
#: It had THREE. `admission.claim_key` built it, `Job.claim_key` built it again, and
#: `forge_store.decode_claim_key` parsed it with a regex written from scratch -- one grammar, three
#: independent definitions, and the two writers were byte-identical by luck rather than by
#: construction. Changing any one of them silently strands every live claim: the shard suffix stops
#: matching, shard 1 is refused while shard 0 is held, and the mechanism degrades to serial WITHOUT
#: ERRORING. Both `claim_key` docstrings warn about exactly that failure while being two of the
#: three copies that could cause it.
#:
#: HERE rather than in `job`, because `job` imports this module and the arrow must not reverse.
SHARD_SUFFIX = 's{shard}of{n_shards}'

#: The inverse, for the same reason. A parser written separately from the writer is the drift.
SHARD_SUFFIX_PATTERN = r's(?P<shard>\d+)of(?P<n_shards>\d+)'


def shard_suffix(*, shard: int | None, n_shards: int | None) -> str:
    """The `/s<i>of<n>` tail of a claim key, or `''` for work that is not sharded.

    ``n_shards <= 1`` IS THE UNSHARDED IDENTITY, byte-for-byte: live claims and their leases already
    exist under the bare spelling, and a key that changed shape would strand every in-flight claim
    -- and let a second runner take work that really is running.
    """
    if not n_shards or n_shards <= 1:
        return ''
    return '/' + SHARD_SUFFIX.format(shard=shard, n_shards=n_shards)


def claim_key(job: dict) -> str:
    """The claim namespace for ``job``. A GROUP HAS NO TESTKEY, and `None` is not a key.

    `_pick_group` sets `testkey: None`, so every slow run claimed the literal string ref
    `refs/ci/claims/None/slow`. Combined with a claim that was never released, that meant a group
    could run exactly ONCE, ever: it is keyed by freshness and must re-run each `max_age_hours`,
    and the second attempt's claim was refused for all time.

    THE SHARD IS PART OF THE NAMESPACE (M2b), because a claim is what stops two runners doing the
    same work -- and two runners taking DIFFERENT SLICES of one job is not the same work, it is the
    entire point of sharding. Leave the shard out and shard 1 is refused while shard 0 is held, so
    the mechanism degrades to serial without erroring: nothing fails, the job simply never gets
    faster. The WIDTH is in the key too, since a 2-way shard 1 and a 4-way shard 1 cover different
    tests.

    ``n_shards = 1`` IS THE IDENTITY, deliberately and byte-for-byte: live claims and their leases
    already exist under the current spelling, and a key that changed shape would strand every
    in-flight claim -- and let a second runner take work that really is running.
    """
    base = job['testkey'] if job.get('testkey') else f'group-{job["group"]}'
    n_shards = int(job.get('n_shards') or 1)
    # THE SUFFIX IS NOT SPELLED HERE. See `shard_suffix`: this was one of three copies of one
    # grammar, and the two that wrote it agreed by luck.
    return base + shard_suffix(shard=int(job['shard']) if n_shards > 1 else None, n_shards=n_shards)


#: A claim of this runner's own must be at least this old before the free lock is read as proof the
#: claimer died. `claim` runs BEFORE the expensive lock is taken, so for those microseconds a live
#: run has a claim and no lock -- indistinguishable, without this floor, from an abandoned one.
_OWN_CLAIM_MIN_AGE_S = 120.0


def own_claim_is_abandoned(stamp: str, *, runner: str, now: float, lock_is_held: bool) -> bool:
    """Is this claim MINE, old enough, and provably not backed by a running gate?

    THE POINT: `release_claim` runs in a `finally`, which a hard kill skips. Measured 2026-08-08 --
    stopping the loop mid-run stranded `group-xfemm/xfemm/<epoch>-G`, and with a 4 h lease that is
    half a day of xfemm coverage lost to a Ctrl-C on a one-executor fleet. The lease is the right
    backstop for a runner that VANISHED; it is the wrong answer for a runner that is right here and
    knows it is not running the job.

    WHY THE NAME ALONE IS NOT ENOUGH, and this is the trap worth stating. "Reclaim any claim bearing
    my own name" breaks the fleet guarantee: `claim` happens before the lock, so a second tick could
    strip the claim off a run that is about to start here, and then ANOTHER MACHINE could claim and
    run the same job concurrently -- the exact thing claims exist to prevent, undone by the fix for
    something else.

    So the proof of death is the LOCK, which is already this machine's truth and needs no new
    bookkeeping: a claim in my name while no gate holds the lock means the process behind it is
    gone. The age floor covers the only remaining window -- the moment between claiming and locking.

    A claim from ANOTHER runner is never touched here: this machine's lock says nothing about that
    box, and only the lease may expire it. Nor is a claim whose name does not parse -- claims from
    an older scheme are on this remote, and deleting what we cannot read is not a fix.
    """
    epoch_text, _, holder = stamp.partition('-')
    if not epoch_text.isdigit() or holder != runner or lock_is_held:
        return False
    return now - int(epoch_text) >= _OWN_CLAIM_MIN_AGE_S


def staleness_blocker(behind: int | None) -> str | None:
    """Why this box may not claim a GROUP, or ``None`` if it may. The fifth admission check.

    A group result is about the WORKING TREE, and its freshness ref records only WHEN it ran --
    never WHAT it ran against. So a runner whose checkout has drifted keeps satisfying
    `max_age_hours` with verdicts about old code, which is the unearned-green shape: `status` shows
    the group PASS, the deadline is met, and the tested tree is days old.

    REFUSAL, NOT CORRECTION. Recording `origin/main`'s sha while gating a stale checkout would
    sharpen the lie rather than remove it -- `gate.py` runs the working tree, so the verdict would
    name a tree it never tested. Auto-updating the checkout is worse still: these runners are
    developer boxes, and a scheduler that resets somebody's working tree unattended to make its own
    deadline is a cure worse than the disease.

    THE THRESHOLD IS ZERO. "A few commits behind is probably fine" is how a freshness contract stops
    meaning anything, and the runner cannot know which commits matter -- that is what a `testkey`
    decides on the candidate path, and groups have none by construction, re-running on AGE.
    """
    if not behind:
        return None
    return (
        f'checkout is {behind} commit(s) behind origin/main -- a group result would refresh the '
        f'freshness deadline for code this box is not running. `git pull` (or point ci_loop at an '
        f'up-to-date checkout) and the next tick will take it.'
    )
