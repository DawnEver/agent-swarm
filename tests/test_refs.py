"""The ref grammar: three namespaces that must not collapse into each other.

PROVENANCE. Extracted from motronics' `scripts/ci/ci.py` and `ci_tick.py`, where the attempt
namespace was spelled at six call sites and the shard namespace at five. Every property pinned here
was written in answer to a measured incident, and all of them are silent failures: a slice answering
a whole tier, a non-answer in the answer slot, attempt 10 sorting before attempt 2, a width change
making a half-filled partition look complete, and a retention sweep that stopped reaching the refs
written before a segment was added.
"""

from __future__ import annotations

import pytest

from agent_swarm.refs import (
    ATTEMPTS_ROOT,
    SHARDS_ROOT,
    VERDICTS_ROOT,
    aged_globs,
    attempt_glob,
    attempt_number,
    attempt_ref,
    shard_index,
    shard_prefix,
    shard_ref,
    verdict_glob,
    verdict_ref,
)

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------- the answer namespace


def test_the_verdict_ref_carries_all_three_facts():
    assert verdict_ref('KEY', 'fast', 'ENV') == 'refs/verdicts/KEY/fast/ENV'


def test_two_environments_are_two_refs():
    """The whole reason the third segment exists. One ref for both would let a verdict earned
    somewhere else answer here, silently."""
    assert verdict_ref('K', 'fast', 'ENV-A') != verdict_ref('K', 'fast', 'ENV-B')


def test_the_verdict_glob_has_a_WILDCARD_PER_SEGMENT():
    """Git's `*` does not cross a separator, so a glob one wildcard short finds nothing and reads
    as "no verdicts exist" rather than as a broken query."""
    assert verdict_glob('K') == f'{VERDICTS_ROOT}/K/*/*'


# --------------------------------------------------------------------- attempts


def test_an_attempt_is_not_a_verdict():
    """Two questions -- "what is the answer" and "what happened" -- and one slot cannot hold both
    without discarding three-valued logic at the storage layer."""
    assert not attempt_ref('K', 'fast', 1).startswith(VERDICTS_ROOT + '/')
    assert attempt_ref('K', 'fast', 1) == 'refs/ci/attempts/K/fast/1'


def test_the_attempt_glob_matches_what_the_attempt_ref_writes():
    """The pair that a drift would break silently: refs written where the reader does not look."""
    assert attempt_glob('K', 'fast') == 'refs/ci/attempts/K/fast/*'
    assert attempt_ref('K', 'fast', 7).startswith(attempt_glob('K', 'fast')[:-1])


def test_the_attempt_number_comes_back_as_an_INT():
    """Sorted as strings, attempt 10 precedes attempt 2 and an answered job reads as unanswered --
    a bug that only appears after the tenth attempt, on the jobs already in trouble."""
    refs = [attempt_ref('K', 'fast', n) for n in (2, 10)]
    assert sorted(refs, key=attempt_number) == refs  # type: ignore[arg-type]
    assert attempt_number(attempt_ref('K', 'fast', 10)) == 10


def test_a_ref_that_is_not_an_attempt_yields_None():
    """The group namespace and the verdict namespace share the prefix shape, so "not a number" has
    to be representable rather than an exception at every reader."""
    assert attempt_number('refs/ci/attempts/K/fast') is None
    assert attempt_number(verdict_ref('K', 'fast', 'ENV')) is None


# --------------------------------------------------------------------- shards


def test_a_shard_is_never_written_in_the_answer_slot():
    """THE REGRESSION THIS NAMESPACE EXISTS FOR. An existence probe on a verdict is what stops a
    re-run, so one slice's PASS there answers the whole tier on a fraction of the suite."""
    assert not shard_ref('K', 'fast', 1, 4).startswith(VERDICTS_ROOT + '/')
    assert shard_ref('K', 'fast', 2, 4) == 'refs/ci/shards/K/fast/2of4'


def test_the_WIDTH_is_in_the_path():
    """A 2-way slice 1 and a 4-way slice 1 cover different tests. Same ref, and editing the shard
    count leaves some tests covered twice and others never, with every slot looking full."""
    assert shard_ref('K', 'fast', 1, 4) != shard_ref('K', 'fast', 1, 2)


def test_every_slice_of_a_partition_is_a_distinct_ref():
    assert len({shard_ref('K', 'fast', i, 4) for i in range(1, 5)}) == 4


def test_the_shard_prefix_is_shared_by_every_width():
    """Because a reader LISTS the prefix and filters on the width -- it cannot glob a width it is
    trying to discover has changed."""
    for width in (2, 4):
        assert shard_ref('K', 'fast', 1, width).startswith(shard_prefix('K', 'fast'))


def test_the_index_comes_back_only_for_the_MATCHING_width():
    """The filter, stated as its own property: this is the function that refuses the stale
    partition, and a version that ignored `n_shards` would satisfy every other test here."""
    assert shard_index(shard_ref('K', 'fast', 3, 4), 4) == 3
    assert shard_index(shard_ref('K', 'fast', 1, 2), 4) is None


def test_a_malformed_shard_segment_yields_None():
    assert shard_index('refs/ci/shards/K/fast/junk', 4) is None


# --------------------------------------------------------------------- retention


def test_the_sweep_reaches_the_DEEPER_verdict_namespace():
    """Left at two wildcards, every verdict written after the environment segment landed becomes
    IMMORTAL, and the only symptom is push negotiation slowing for everyone months later."""
    assert f'{VERDICTS_ROOT}/*/*/*' in aged_globs()


def test_the_sweep_still_reaches_the_depth_written_BEFORE_it():
    """The half a forward-looking test cannot see, and it was a live hole for one commit: sweeping
    only the new shape grandfathers everything predating the migration, forever, silently."""
    assert f'{VERDICTS_ROOT}/*/*' in aged_globs()


def test_the_sweep_reaches_attempts_too():
    assert f'{ATTEMPTS_ROOT}/*/*/*' in aged_globs()


def test_shards_are_deliberately_NOT_age_swept():
    """Stated as a test so the absence is a decision on record rather than an omission. They are
    collected by lifecycle -- with the composed verdict that made them garbage -- which is exact and
    O(1), where an age sweep can only guess how long a partition may legitimately stay open."""
    assert not any(pattern.startswith(SHARDS_ROOT) for pattern in aged_globs())
