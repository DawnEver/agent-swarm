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
    GROUPS_ROOT,
    SHARDS_ROOT,
    VERDICTS_ROOT,
    aged_globs,
    attempt_glob,
    attempt_number,
    attempt_ref,
    capability_glob,
    capability_of,
    capability_ref,
    group_attempt_key,
    group_name,
    group_ref,
    heartbeat_glob,
    heartbeat_ref,
    heartbeat_stamp,
    runner_of,
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


def test_the_verdict_glob_is_written_at_FULL_DEPTH():
    """The pattern SHOWS the shape of what it collects. It is not a narrowing -- `ls-remote`'s `*`
    crosses `/`, measured, so a shorter pattern would match the same refs. Pinned so nobody
    "simplifies" it and loses the only place the shape is written down."""
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
    """Kept because the shape is worth stating; NOT because two wildcards would miss it. The
    mechanism once given for this entry was refuted by measurement -- see `aged_globs`."""
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


# --------------------------------------------------------------------------- liveness


def test_the_heartbeat_stamp_is_in_the_PATH():
    """Not in the object. A ref pointing at whatever the head commit is encodes no time at all, and
    a server-side ref's update time is not queryable over the ordinary git protocol -- so the
    heartbeat could not distinguish the two states it exists to distinguish."""
    assert heartbeat_ref('boxA-1234abcd', 1755000000) == 'refs/ci/heartbeat/boxA-1234abcd/1755000000'


def test_the_stamp_comes_back_as_an_INT():
    """This namespace is swept by "delete every stamp but the newest". Picking the maximum by
    STRING order names the wrong survivor the moment the digit count changes -- and the survivor is
    the only thing standing between this runner and reading as dead."""
    refs = [heartbeat_ref('r', 999999999), heartbeat_ref('r', 1000000000)]
    assert max(refs, key=heartbeat_stamp) == refs[1]  # type: ignore[arg-type]


def test_a_ref_with_no_stamp_yields_None():
    assert heartbeat_stamp('refs/ci/heartbeat/boxA') is None


def test_the_FLEET_WIDE_glob_is_written_at_full_depth():
    """What separates one runner from the fleet is the runner SEGMENT, not the wildcard count --
    the depth is documentation. See `InMemoryRefStore` for the measured matching rule."""
    assert heartbeat_glob() == 'refs/ci/heartbeat/*/*'
    assert heartbeat_glob('boxA') == 'refs/ci/heartbeat/boxA/*'


def test_the_fleet_wide_glob_MATCHES_what_one_runner_writes():
    """The pair a drift would break silently. Asserted by shape rather than by eye, because the two
    are written and read in different files."""
    ref = heartbeat_ref('boxA', 1)
    root, _, pattern = heartbeat_glob().partition('*')
    assert ref.startswith(root) and pattern == '/*'


def test_a_runner_id_with_HYPHENS_survives_the_parse():
    """Runner ids are `<hostname>-<salt>`, so a parse that counted segments from the right or split
    on a hyphen would mangle every real one."""
    assert runner_of(heartbeat_ref('some-box-1234abcd', 17)) == 'some-box-1234abcd'


def test_runner_of_refuses_a_ref_from_another_namespace():
    """The discriminating half: a parser that just took a segment would happily name a testkey as a
    runner and report a fleet member nobody owns."""
    assert runner_of(verdict_ref('K', 'fast', 'ENV')) is None


# --------------------------------------------------------------------------- capability


def test_a_capability_is_ONE_REF_not_a_list():
    """A list means fetching and parsing an object to answer "who can do X"; a ref per capability
    makes it a prefix listing, and makes REVOCATION a deletion rather than a read-modify-write two
    runners can lose."""
    assert capability_ref('boxA', 'native_fea') == 'refs/ci/fleet/boxA/native_fea'


def test_the_capability_and_the_runner_are_both_recoverable():
    ref = capability_ref('some-box-1234abcd', 'vendor:thing')
    assert runner_of(ref) == 'some-box-1234abcd'
    assert capability_of(ref) == 'vendor:thing'


def test_capability_of_refuses_a_heartbeat():
    """The two namespaces have the same DEPTH, so only the root can tell them apart -- and reading
    an epoch as a capability would advertise the fleet as able to do "1755000000"."""
    assert capability_of(heartbeat_ref('boxA', 1755000000)) is None


def test_the_capability_globs_match_what_is_written():
    assert capability_glob() == 'refs/ci/fleet/*/*'
    assert capability_glob('boxA') == 'refs/ci/fleet/boxA/*'


# --------------------------------------------------------------------------- groups


def test_a_group_ref_is_ONE_ref_overwritten():
    """The question is "how long since this ran", and only the most recent answer can address it --
    so unlike an attempt, a group's conclusion is a single mutable ref."""
    assert group_ref('slow') == 'refs/ci/groups/slow'
    assert group_name(group_ref('slow')) == 'slow'


def test_group_name_refuses_another_namespace():
    assert group_name('refs/ci/fleet/boxA/cap') is None
    assert group_name(GROUPS_ROOT) is None


def test_a_groups_attempts_borrow_the_attempt_namespace_under_a_PREFIXED_key():
    """A group has no tree, so it cannot have a testkey. The `group-` prefix is STRUCTURAL: a
    counter takes the name from this segment and never from the `kind` beside it, because the two
    are written together and only this one is guaranteed."""
    key = group_attempt_key('slow')
    assert key == 'group-slow'
    assert attempt_ref(key, 'slow', 3) == 'refs/ci/attempts/group-slow/slow/3'


def test_a_group_attempt_is_still_an_attempt():
    """It must age out on the same retention sweep as everything else, or the namespace grows
    forever in the one place nobody thinks to look."""
    assert attempt_number(attempt_ref(group_attempt_key('slow'), 'slow', 3)) == 3
    assert f'{ATTEMPTS_ROOT}/*/*/*' in aged_globs()
