"""Reassembling N slices into one verdict -- the half that can mint a green nobody earned.

PROVENANCE. `compose_shard_verdict` and `next_unfinished_shard` from motronics' `ci_tick.py`, where
they were written and tested BEFORE anything could produce a partial result for them. Splitting a
suite is arithmetic; putting it back together is where absence gets read as success.
"""

from __future__ import annotations

import pytest

from agent_swarm.shards import compose, next_unfinished


# --------------------------------------------------------------------------- precedence


def test_all_present_and_passing_is_PASS():
    assert compose({1: 'PASS', 2: 'PASS'}, 2)['result'] == 'PASS'


def test_one_FAIL_is_FAIL():
    assert compose({1: 'PASS', 2: 'FAIL'}, 2)['result'] == 'FAIL'


def test_a_MISSING_slice_is_INCONCLUSIVE_and_never_PASS():
    """THE RULE WORTH BREAKING THE BUILD OVER. A lost slice and a crashed slice are the same
    epistemic state, and reading absence as success mints a green for work that never ran."""
    assert compose({1: 'PASS'}, 2)['result'] == 'INCONCLUSIVE'


def test_an_INCONCLUSIVE_slice_is_INCONCLUSIVE():
    assert compose({1: 'PASS', 2: 'INCONCLUSIVE'}, 2)['result'] == 'INCONCLUSIVE'


def test_FAIL_OUTRANKS_a_missing_slice():
    """A real result about the code is not unsaid by another slice's infrastructure noise."""
    assert compose({1: 'FAIL'}, 4)['result'] == 'FAIL'


def test_FAIL_OUTRANKS_an_inconclusive_slice():
    assert compose({1: 'FAIL', 2: 'INCONCLUSIVE'}, 2)['result'] == 'FAIL'


def test_an_UNRECOGNISED_result_is_not_a_pass():
    """The default direction. A typo, a truncated word, a new verdict word nobody taught this
    function -- each is "not PASS", and treating an unknown as success is the one way this can be
    wrong that costs a green."""
    assert compose({1: 'PASS', 2: 'probably fine'}, 2)['result'] == 'INCONCLUSIVE'


def test_nothing_reported_at_all_is_INCONCLUSIVE():
    assert compose({}, 4)['result'] == 'INCONCLUSIVE'


# --------------------------------------------------------------------------- the reason


def test_the_reason_NAMES_the_failing_slices():
    """A bare FAIL tells an operator nothing about where to look."""
    assert '[2]' in compose({1: 'PASS', 2: 'FAIL'}, 2)['reason']


def test_the_reason_NAMES_the_missing_slices():
    """And a bare INCONCLUSIVE cannot distinguish "wait for a straggler" from "go look at a dead
    runner"."""
    reason = compose({1: 'PASS'}, 3)['reason']
    assert '[2, 3]' in reason and 'never reported' in reason


def test_the_reason_reports_BOTH_missing_and_inconclusive():
    """Two different problems in one partition. Reporting only the first found would hide the
    other until the first was fixed, one round trip at a time."""
    reason = compose({1: 'INCONCLUSIVE'}, 3)['reason']
    assert 'never reported' in reason and 'INCONCLUSIVE' in reason


def test_a_pass_still_says_how_many_slices_it_covered():
    """The number is the claim's SCOPE. "PASS" alone cannot be checked against the width the job
    was actually split into."""
    assert '3' in compose({1: 'PASS', 2: 'PASS', 3: 'PASS'}, 3)['reason']


# --------------------------------------------------------------------------- the width must agree


def test_an_index_ABOVE_the_width_raises():
    """The caller and the job disagree about N, which means some fraction of the work is
    unaccounted for in a partition that is supposed to be exact."""
    with pytest.raises(ValueError, match='out of range'):
        compose({1: 'PASS', 5: 'PASS'}, 4)


def test_a_ZERO_index_raises():
    """Slices are 1-indexed, owned by whatever performs the partition. A second convention here
    converts at a boundary, and that is how one slice silently never runs."""
    with pytest.raises(ValueError, match='out of range'):
        compose({0: 'PASS'}, 4)


def test_a_width_below_one_raises():
    with pytest.raises(ValueError, match='n_shards'):
        compose({}, 0)


def test_a_ONE_way_split_is_legal():
    """The discriminating half: refusing everything would satisfy all three tests above, and an
    unsharded job is the common case."""
    assert compose({1: 'PASS'}, 1)['result'] == 'PASS'


# --------------------------------------------------------------------------- picking the next one


def test_nothing_done_takes_the_first():
    assert next_unfinished({}, 4) == 1


def test_it_skips_what_is_already_reported():
    assert next_unfinished({1: 'PASS', 2: 'FAIL'}, 4) == 3


def test_it_fills_a_HOLE_rather_than_appending():
    """A slice whose runner died leaves a gap, and by `compose` a permanent gap is INCONCLUSIVE
    forever -- so the picker must go back for it."""
    assert next_unfinished({1: 'PASS', 3: 'PASS', 4: 'PASS'}, 4) == 2


def test_a_complete_set_is_None():
    assert next_unfinished({1: 'PASS', 2: 'PASS'}, 2) is None


def test_lowest_first_is_deterministic():
    """Two racing runners must pick the SAME slice, so the already-tested claim lease resolves it.
    Random picking would make the collision path rare, which is the same as untested."""
    assert next_unfinished({2: 'PASS'}, 4) == next_unfinished({2: 'PASS'}, 4) == 1


def test_a_single_slice_job_is_slice_one():
    assert next_unfinished({}, 1) == 1
    assert next_unfinished({1: 'PASS'}, 1) is None
