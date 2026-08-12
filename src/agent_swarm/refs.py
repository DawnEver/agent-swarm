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

THE THREE NAMESPACES ARE SEPARATE ON PURPOSE, and the separations are load-bearing:

    verdicts   the ANSWER. Immutable, and only ever a concluded result. An existence probe here is
               what stops a tree being re-tested, so anything that is not an answer for the WHOLE
               of a tier must not be written here.
    attempts   every RUN, append-only, including the inconclusive ones. Two different questions --
               "what is the answer" and "what happened" -- and one slot cannot hold both without
               throwing away three-valued logic at the storage layer.
    shards     ONE SLICE of a partitioned run. Deliberately not a verdict: a slice's PASS in the
               answer slot answers the whole tier on a fraction of the work.

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

_SHARD_SUFFIX = re.compile(r'^(\d+)of(\d+)$')


def verdict_ref(testkey: str, kind: str, envkey: str) -> str:
    """Where the answer to (tree, tier, environment) lives. THREE SEGMENTS, and the third is the
    point: without it a verdict claims more than it knows."""
    return f'{VERDICTS_ROOT}/{testkey}/{kind}/{envkey}'


def verdict_glob(testkey: str) -> str:
    """Every verdict known for a tree, across tiers AND environments.

    TWO WILDCARDS, because git's `*` does not cross a separator. A reader wants the ones from other
    environments too -- "PASS, but not here" is information, and hiding it would reintroduce the
    silence the environment segment exists to prevent.
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

    IT INCLUDES A SHALLOWER VERDICT DEPTH THAN `verdict_ref` WRITES, and that is deliberate rather
    than dead. Git's `*` does not cross a separator, so the day the environment segment was added,
    a sweep globbing the new depth alone stopped reaching every ref written before it -- MEASURED on
    a live remote as 10 immortal refs. A migration that silently grandfathers everything predating
    it is reported by nothing: the only symptom is push/fetch negotiation slowing for everybody,
    months later.

    Shards are NOT here, and their absence is a decision. They are collected by LIFECYCLE -- deleted
    with the composed verdict that made them garbage -- which is O(1), exact, and bounds the
    namespace by work IN FLIGHT rather than by guessing how long a partition may stay open.
    """
    return (f'{VERDICTS_ROOT}/*/*', f'{VERDICTS_ROOT}/*/*/*', f'{ATTEMPTS_ROOT}/*/*/*')
