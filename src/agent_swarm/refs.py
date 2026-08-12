"""THE REF GRAMMAR: where an answer, an attempt and a slice of one live, in ONE place.

WHAT THIS IS. The ref-namespace vocabulary of motronics' `scripts/ci/ci.py` and `ci_tick.py`,
extracted the day an audit classified every top-level definition under `scripts/` by whether its
CODE names a project noun. None of these do: a testkey, a tier and an environment key are three
opaque strings, and this module composes and decomposes paths out of them.

WHY IT IS A MODULE AND NOT THREE F-STRINGS. It already was three f-strings, and MEASURED at the time
of extraction the attempt namespace was spelled at SIX call sites across two files and the shard
namespace at FIVE. A ref grammar is the purest case of a duplicated scheme: a drift in one copy does
not raise, it writes where nothing looks, and the symptom is a gate that silently re-runs forever or
a retention sweep that misses a namespace and lets it grow without bound.

THE NAMESPACES ARE SEPARATE ON PURPOSE, and the separations are load-bearing:

    verdicts   the ANSWER. Immutable, and only ever a concluded result. An existence probe here is
               what stops a tree being re-tested, so anything that is not an answer for the WHOLE
               of a tier must not be written here.
    attempts   every RUN, append-only, including the inconclusive ones. Two different questions --
               "what is the answer" and "what happened" -- and one slot cannot hold both without
               throwing away three-valued logic at the storage layer.
    shards     ONE SLICE of a partitioned run. Deliberately not a verdict: a slice's PASS in the
               answer slot answers the whole tier on a fraction of the work.
    heartbeat  WHO IS ALIVE, with the time in the ref NAME rather than in the object.
    fleet      WHAT EACH RUNNER CAN SERVE, one ref per capability rather than one list.
    groups     a SCHEDULED tier's last conclusion, keyed by freshness because it has no tree.

THE LAST THREE ARE PATHS ONLY, and the boundary is worth stating so nobody looks for the rest here:
what WRITES them -- the bounded-retry push, the prune of every stamp but the newest, the refusal to
prune after a failed push -- stayed with its consumer, because it needs a git transport this layer
deliberately does not have. What moved is the vocabulary, which is what was duplicated.

THE ENVIRONMENT IS IN THE PATH, not in the payload. A verdict is a claim about what DID run, which
no hash of a tree can identify on its own (see :mod:`agent_swarm.environment`). Putting the third
segment in the ref means a one-shot existence probe still answers "is there a verdict FOR ME", with
no extra round trip and no comparison for a reader to forget.

AND THE WIDTH IS IN THE SHARD PATH. A 2-way slice 1 and a 4-way slice 1 cover different tests, so a
partition whose width changed must start a fresh set rather than read the old refs as part of the
new one -- otherwise some tests are covered twice, others never, and every slot looks full.
"""

from __future__ import annotations

import re

#: The ANSWER namespace. One spelling: a second copy of a ref root drifts, and a drift here reads
#: exactly like a broken gate -- results written where nothing looks.
VERDICTS_ROOT = 'refs/verdicts'

#: Every RUN, append-only, inconclusive ones included.
ATTEMPTS_ROOT = 'refs/ci/attempts'

#: One slice of a partitioned run. Never an answer for the whole tier.
SHARDS_ROOT = 'refs/ci/shards'

#: WHO IS ALIVE. See :func:`heartbeat_ref` for why the time is in the NAME and not in the object.
HEARTBEAT_ROOT = 'refs/ci/heartbeat'

#: WHAT EACH RUNNER CAN SERVE, one ref per capability, so a single prefix listing answers "what can
#: the fleet do" with no fetch and no object read.
FLEET_ROOT = 'refs/ci/fleet'

#: A SCHEDULED GROUP's last conclusion, keyed by FRESHNESS rather than by a tree: the question a
#: group answers is "how long since this last ran", which no testkey can express.
GROUPS_ROOT = 'refs/ci/groups'

_SHARD_SUFFIX = re.compile(r'^(\d+)of(\d+)$')


def verdict_ref(testkey: str, kind: str, envkey: str) -> str:
    """Where the answer to (tree, tier, environment) lives. THREE SEGMENTS, and the third is the
    point: without it a verdict claims more than it knows."""
    return f'{VERDICTS_ROOT}/{testkey}/{kind}/{envkey}'


def verdict_glob(testkey: str) -> str:
    """Every verdict known for a tree, across tiers AND environments.

    A reader wants the ones from other environments too -- "PASS, but not here" is information, and
    hiding it would reintroduce the silence the environment segment exists to prevent.

    THE SECOND WILDCARD IS DOCUMENTATION, NOT NECESSITY, and the correction is on record because
    the opposite was believed. `ls-remote`'s `*` DOES cross `/` (measured 2026-08-12; the table is
    in `InMemoryRefStore`), so one wildcard would match just as much. It is spelled at full depth
    so the pattern SHOWS the shape of what it collects -- and because nothing about the matching
    rule protects a reader who assumes the opposite, every consumer must filter what comes back
    rather than trust the pattern to have narrowed it.
    """
    return f'{VERDICTS_ROOT}/{testkey}/*/*'


def attempt_ref(testkey: str, kind: str, attempt: int) -> str:
    """Where ONE run of a job is recorded, whatever it concluded."""
    return f'{ATTEMPTS_ROOT}/{testkey}/{kind}/{attempt}'


def attempt_glob(testkey: str, kind: str) -> str:
    return f'{ATTEMPTS_ROOT}/{testkey}/{kind}/*'


def attempt_number(ref: str) -> int | None:
    """The attempt number in `ref`, or `None` if the last segment is not one.

    A FUNCTION RATHER THAN `rsplit` AT EACH READER, and the reason is measured: attempts are
    numbered and callers ask which is LAST, so a reader that sorts the SEGMENT as a string puts
    attempt 10 before attempt 2 and reads an answered job as unanswered -- a bug that appears only
    after the tenth attempt, i.e. on exactly the jobs already in trouble. Returning an `int` is what
    makes the correct sort the easy one.
    """
    tail = ref.rsplit('/', 1)[-1]
    return int(tail) if tail.isdigit() else None


def shard_ref(testkey: str, kind: str, shard: int, n_shards: int) -> str:
    """Where ONE slice's result lives. Deliberately NOT under the verdict root.

    An existence probe on a verdict is what stops a tier being re-run, so a slice's PASS written
    there would answer the whole job on one fraction of the suite -- the same collapse as storing a
    non-answer in the answer slot. Only a COMPOSED result is ever a verdict.
    """
    return f'{SHARDS_ROOT}/{testkey}/{kind}/{shard}of{n_shards}'


def shard_prefix(testkey: str, kind: str) -> str:
    """The prefix every width's slices share. A reader FILTERS on the width after listing this."""
    return f'{SHARDS_ROOT}/{testkey}/{kind}/'


def shard_index(ref: str, n_shards: int) -> int | None:
    """The slice number in `ref` IF it belongs to the `n_shards`-way partition, else `None`.

    THE WIDTH CHECK IS THE POINT, not a convenience. Reading a narrower partition's refs as part of
    a wider one makes the set look progressively full while the partition underneath has changed,
    leaving some tests covered twice and others not at all.
    """
    match = _SHARD_SUFFIX.match(ref.rsplit('/', 1)[-1])
    if match is None or int(match.group(2)) != n_shards:
        return None
    return int(match.group(1))


def aged_globs() -> tuple[str, ...]:
    """Every pattern an age-based sweep must list for the namespace to stay bounded.

    IT INCLUDES A SHALLOWER VERDICT DEPTH THAN `verdict_ref` WRITES. The refs it names are real --
    MEASURED on a live remote as 10 written before the environment segment existed -- and a
    migration that silently grandfathers everything predating it is reported by nothing: the only
    symptom is push/fetch negotiation slowing for everybody, months later.

    THE MECHANISM ORIGINALLY GIVEN FOR THIS ENTRY WAS WRONG, and it is corrected here rather than
    quietly dropped. The claim was that git's `*` does not cross a separator, so a deeper pattern
    could not reach a shallower ref. Measured 2026-08-12 against a real remote: it DOES cross, so
    `refs/verdicts/*/*` already matches a three-segment verdict and the deeper pattern is redundant
    rather than load-bearing. Both are kept because a sweep visiting one ref twice costs one extra
    delete of an already-absent ref (which git exits 0 on), while a reader who trims the list on
    the strength of the corrected mechanism has to re-derive which depths were ever written.

    Shards are NOT here, and their absence is a decision. They are collected by LIFECYCLE -- deleted
    with the composed verdict that made them garbage -- which is O(1), exact, and bounds the
    namespace by work IN FLIGHT rather than by guessing how long a partition may stay open.
    """
    return (f'{VERDICTS_ROOT}/*/*', f'{VERDICTS_ROOT}/*/*/*', f'{ATTEMPTS_ROOT}/*/*/*')


# --------------------------------------------------------------------------- liveness and capability


def heartbeat_ref(runner: str, epoch: int) -> str:
    """Where a runner says it was alive at `epoch`.

    THE TIME IS IN THE NAME, and that is the design rather than a detail. Pointing one ref per
    runner at whatever the head commit is encodes no time at all: if the branch does not move, a
    listing returns the same sha whether the runner beat ten seconds ago or died three hours ago,
    and a server-side ref's update time is NOT queryable over the ordinary git protocol -- reflogs
    are local. So the heartbeat could not distinguish the exact two states it exists to
    distinguish. Putting the stamp in the path costs no new object: the ref still points at a
    commit that already exists.
    """
    return f'{HEARTBEAT_ROOT}/{runner}/{epoch}'


def heartbeat_glob(runner: str | None = None) -> str:
    """One runner's stamps, or -- with no argument -- the whole fleet's.

    BOTH FORMS ARE WRITTEN AT FULL DEPTH so the pattern shows what it collects. It is NOT a
    narrowing: `ls-remote`'s `*` crosses `/`, so the fleet-wide pattern would match the same refs
    with one wildcard. What actually separates one runner's stamps from the fleet's is the runner
    SEGMENT, which is why that is the argument.
    """
    return f'{HEARTBEAT_ROOT}/{runner}/*' if runner is not None else f'{HEARTBEAT_ROOT}/*/*'


def heartbeat_stamp(ref: str) -> int | None:
    """The epoch in `ref`, or `None` when the last segment is not one.

    AN INT, so callers compare times rather than strings. This namespace is swept by "delete every
    stamp that is not the one I just wrote", and a caller that picked a MAXIMUM by string order
    would name the wrong survivor every time the digit count changed.
    """
    tail = ref.rsplit('/', 1)[-1]
    return int(tail) if tail.isdigit() else None


def runner_of(ref: str) -> str | None:
    """Which runner a heartbeat or capability ref belongs to, or `None` if it is neither.

    PARSED FROM A KNOWN ROOT rather than by counting segments from the right. A runner id contains
    hyphens and a capability name may contain almost anything, so an index-from-the-end rule is a
    guess that happens to work on today's names.
    """
    for root in (HEARTBEAT_ROOT, FLEET_ROOT):
        if ref.startswith(root + '/'):
            rest = ref.removeprefix(root + '/')
            return rest.split('/', 1)[0] if '/' in rest else None
    return None


def capability_ref(runner: str, capability: str) -> str:
    """One ref per (runner, capability). Not one ref holding a LIST.

    A list means a reader must fetch and parse an object to answer "who can do X"; a ref per
    capability makes it a prefix listing. It also makes REVOCATION expressible -- a capability that
    disappeared is a deleted ref -- where rewriting a list is a read-modify-write two runners can
    lose.
    """
    return f'{FLEET_ROOT}/{runner}/{capability}'


def capability_glob(runner: str | None = None) -> str:
    return f'{FLEET_ROOT}/{runner}/*' if runner is not None else f'{FLEET_ROOT}/*/*'


def capability_of(ref: str) -> str | None:
    """The capability named by `ref`, or `None` if it is not a capability ref."""
    if not ref.startswith(FLEET_ROOT + '/'):
        return None
    rest = ref.removeprefix(FLEET_ROOT + '/')
    return rest.split('/', 1)[1] if '/' in rest else None


# --------------------------------------------------------------------------- scheduled groups


def group_ref(name: str) -> str:
    """A scheduled group's last conclusion. ONE ref per group, overwritten, because the question is
    "how long since this ran" and only the most recent answer can address it."""
    return f'{GROUPS_ROOT}/{name}'


def group_glob() -> str:
    return f'{GROUPS_ROOT}/*'


def group_name(ref: str) -> str | None:
    """The group named by `ref`, or `None`. A prefix strip, not a split, so a name may not be
    invented for a ref from another namespace that happens to have the right depth."""
    if not ref.startswith(GROUPS_ROOT + '/'):
        return None
    return ref.removeprefix(GROUPS_ROOT + '/') or None


def group_attempt_key(name: str) -> str:
    """The pseudo-testkey a group's ATTEMPTS are recorded under: ``group-<name>``.

    A GROUP HAS NO TREE, which is the whole reason this exists. Attempts are keyed by (testkey,
    kind), and a group is keyed by freshness -- so it borrows the attempt namespace under a prefixed
    key rather than growing a fourth namespace. The `group-` prefix is STRUCTURAL: a reader counting
    a group's attempts takes the name from this segment, never from the `kind` segment beside it,
    because the two are written together and only this one is guaranteed.
    """
    return f'group-{name}'
