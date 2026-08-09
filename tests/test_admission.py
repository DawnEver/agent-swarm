"""Admission decides who may run what. These are the properties, independent of any caller.

PROVENANCE. Every function under test was extracted from motronics' `scripts/ci/ci_tick.py`, where
it had been running and measured for months -- M1's first block is the EXTRACTION, not a new
scheduler. The numbers in these tests are therefore measurements, not invented examples, and each
one names where it came from.

THE LIBRARY MUST BE TESTABLE WITHOUT ITS CALLERS. motronics has its own suite covering the
scheduler that consumes this; that suite cannot vouch for the library, because it only ever
exercises the paths its scheduler happens to take. A vendor class the test system never uses is
still a class this layer promises to handle.
"""

from __future__ import annotations

import pytest

from agent_swarm import (
    CHEAP,
    KNOWN_CLASSES,
    SHARED_SLOWDOWN,
    WHOLE_BOX,
    admission_blockers,
    capacity_blocker,
    claim_key,
    classes_conflict,
    should_retry,
    staleness_blocker,
    time_blocker,
)


class TestTheClassRelation:
    """`expensive` means the whole box; two vendors do not conflict; two cheap jobs never do."""

    def test_the_whole_box_conflicts_with_everything(self):
        for other in (WHOLE_BOX, CHEAP, 'vendor:femm', 'vendor:jmag'):
            assert classes_conflict(WHOLE_BOX, other), other

    def test_two_different_vendors_do_not_conflict(self):
        """The measurement this exists for: femm 669 s and jmag 667 s ran genuinely overlapped on
        one box (motronics, 2026-08-09). Before the class layer, jmag returned INCONCLUSIVE in 1 s.
        """
        assert not classes_conflict('vendor:femm', 'vendor:jmag')

    def test_the_same_vendor_conflicts_with_itself(self):
        """One install, one licence, one COM server."""
        assert classes_conflict('vendor:femm', 'vendor:femm')

    def test_two_cheap_jobs_never_conflict(self):
        assert not classes_conflict(CHEAP, CHEAP)

    @pytest.mark.parametrize('unknown', [None, '', 'typo', 'vendor', 'EXPENSIVE'])
    def test_anything_unrecognised_is_the_whole_box(self, unknown):
        """DEFAULT-DENY. The alternative is that a typo in policy silently grants a job the right to
        run beside everything, surfacing as two heavy runs starving each other rather than as a
        config error.
        """
        assert classes_conflict(unknown, CHEAP)

    @pytest.mark.parametrize(
        ('a', 'b'),
        [(WHOLE_BOX, CHEAP), ('vendor:femm', CHEAP), ('vendor:femm', 'vendor:jmag'), (None, 'vendor:femm')],
    )
    def test_the_relation_is_SYMMETRIC(self, a, b):
        """An order-dependent answer would make overlap depend on which job arrived first -- a race
        visible only under load, which is the worst kind to debug.
        """
        assert classes_conflict(a, b) == classes_conflict(b, a)

    def test_the_vocabulary_is_closed_and_named(self):
        assert {WHOLE_BOX, CHEAP} <= KNOWN_CLASSES


class TestAdmissionBlockers:
    def test_nothing_held_admits_anything(self):
        assert admission_blockers({}, WHOLE_BOX) == []

    def test_a_conflicting_holder_blocks(self):
        assert admission_blockers({WHOLE_BOX: True}, 'vendor:femm')

    def test_a_non_conflicting_holder_does_not(self):
        assert admission_blockers({'vendor:jmag': True}, 'vendor:femm') == []


class TestCapacity:
    """Memory pricing. A job admitted onto a box that cannot hold it dies as `node down`, which the
    gate reports INCONCLUSIVE -- so the cost is a LOST RUN, not a wrong verdict.
    """

    def test_a_box_with_room_admits(self):
        assert capacity_blocker(64.0, 12.5, 2.0) is None

    def test_a_box_without_room_refuses_and_says_the_numbers(self):
        blocker = capacity_blocker(4.0, 12.5, 2.0)
        assert blocker is not None
        assert '12.5' in blocker or '12' in blocker, blocker

    def test_unknown_available_memory_REFUSES(self):
        """DEFAULT-DENY here, and it is the OPPOSITE of `staleness_blocker(None)`. That asymmetry
        is deliberate and worth stating, because the next reader will want to make them agree:

        * unmeasurable MEMORY -> refuse. Guessing wrong means an OOM-killed worker, which reads as
          `node down` -> INCONCLUSIVE: a lost run, and the box will keep losing them.
        * unmeasurable STALENESS -> admit. Guessing wrong costs one result about slightly old code,
          while refusing would stop every group on every box the moment the remote hiccups.

        The rule is not "deny on unknown" or "admit on unknown" -- it is "take the cheaper mistake",
        and the two have different cheaper mistakes.
        """
        assert capacity_blocker(None, 12.5, 2.0) is not None

    def test_the_reserve_is_SUBTRACTED_not_ignored(self):
        """DISCRIMINATING: a want that fits the raw figure but not the figure minus the reserve
        must be refused, or the reserve is decorative.
        """
        assert capacity_blocker(13.0, 12.5, 2.0) is not None
        assert capacity_blocker(15.0, 12.5, 2.0) is None


class TestTime:
    """Co-scheduling must not push a job past its own ceiling."""

    def test_running_alone_is_never_time_blocked(self):
        assert time_blocker(356.0, 600.0, sharing=False) is None

    def test_the_MEASURED_case_is_refused(self):
        """jmag: 356 s solo, ~1.9x shared, against the 600 s ceiling it actually hit when the class
        layer first let it overlap with femm (motronics, 2026-08-09).
        """
        assert time_blocker(356.0, 600.0, sharing=True) is not None

    def test_room_to_spare_may_share(self):
        assert time_blocker(356.0, 1800.0, sharing=True) is None

    def test_an_unpriced_job_may_share(self):
        """Refusing every unmeasured job would stop the fleet co-scheduling anything new -- and
        sharing is how a duration gets measured in the first place.
        """
        assert time_blocker(None, 600.0, sharing=True) is None

    def test_the_slowdown_is_at_least_what_was_observed(self):
        assert SHARED_SLOWDOWN >= 1.9


class TestStaleness:
    """A result about an old checkout must not refresh a freshness deadline."""

    def test_up_to_date_is_admitted(self):
        assert staleness_blocker(0) is None

    def test_one_commit_behind_is_refused(self):
        """The threshold is ZERO: 'a few behind is probably fine' is how a freshness contract stops
        meaning anything, and the runner cannot know which commits matter.
        """
        assert staleness_blocker(1) is not None

    def test_an_unknown_distance_does_not_block(self):
        assert staleness_blocker(None) is None


class TestRetry:
    def test_a_FAIL_is_an_ANSWER_and_is_NOT_retried(self):
        """The distinction this layer exists to make. A FAIL means the tests ran and something is
        broken; retrying it burns the fleet to reproduce a known result. Only a NON-answer --
        INCONCLUSIVE -- is worth another attempt.
        """
        assert not should_retry(['FAIL'], 3)

    def test_an_INCONCLUSIVE_is_retried(self):
        assert should_retry(['INCONCLUSIVE'], 3)

    def test_the_LAST_attempt_governs(self):
        """An old INCONCLUSIVE must not keep a job retrying after it has since answered."""
        assert not should_retry(['INCONCLUSIVE', 'PASS'], 3)

    def test_retries_are_BOUNDED(self):
        """An unbounded retry turns one broken job into a runner that never does anything else."""
        assert not should_retry(['INCONCLUSIVE'] * 10, 3)

    def test_a_job_is_always_worth_ONE_attempt(self):
        """`max_retries = 0` means "try once, then stop", never "never run"."""
        assert should_retry([], 0)


class TestClaimKey:
    def test_a_group_job_keys_by_its_group(self):
        assert claim_key({'group': 'femm', 'kind': 'femm'})

    def test_a_candidate_job_keys_by_its_TESTKEY(self):
        """Two branches whose test inputs hash the same are the same work -- that is the whole
        point of a testkey, and the claim namespace has to agree with it.
        """
        assert claim_key({'testkey': 'abc123', 'kind': 'fast'}) == claim_key({'testkey': 'abc123', 'kind': 'fast'})

    def test_two_shards_of_one_job_key_DIFFERENTLY(self):
        """Otherwise shard 2 is refused while shard 1 is held, and sharding degrades to serial
        WITHOUT erroring: nothing fails, the job simply never gets faster.
        """
        a = claim_key({'group': 'g', 'kind': 'k', 'shard': 1, 'n_shards': 2})
        b = claim_key({'group': 'g', 'kind': 'k', 'shard': 2, 'n_shards': 2})
        assert a != b

    def test_the_WIDTH_is_in_the_key_too(self):
        """A 2-way shard 1 and a 4-way shard 1 cover different slices."""
        assert claim_key({'group': 'g', 'kind': 'k', 'shard': 1, 'n_shards': 2}) != claim_key(
            {'group': 'g', 'kind': 'k', 'shard': 1, 'n_shards': 4}
        )
