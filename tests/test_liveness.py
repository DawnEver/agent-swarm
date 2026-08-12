"""Who is alive, what the fleet can do, and the three orderings that keep both answers honest.

PROVENANCE. `heartbeat`, `last_heartbeat`, `publish_capabilities`, `live_runners` and
`fleet_capabilities` from motronics' `ci_tick.py`, rewritten against `RefStore`. Every property here
was paid for by an incident, and all three of the important ones are SILENT in production: an
emptied heartbeat namespace, a runner called dead for having history, and a capability union
carrying a machine that was decommissioned.

WHY THE DOUBLE AND NOT A REAL REMOTE. The failures being tested are a FAILED PUSH and an
UNREACHABLE LISTING -- both of which are hard to produce against a working remote and are exactly
the states the seam exists to make expressible. `InMemoryRefStore` is deliberately as ill-behaved as
git is (segment-bounded globs, rotated listing order), so a test cannot pass here by relying on a
kindness the real transport does not offer.
"""

from __future__ import annotations

import pytest

from agent_swarm import refs
from agent_swarm.liveness import (
    BeatFailed,
    beat,
    fleet_capabilities,
    last_beat,
    live_runners,
    publish_capabilities,
)
from agent_swarm.refstore import RefUnreachable
from agent_swarm.testing import InMemoryRefStore

pytestmark = pytest.mark.unit

NOW = 1_755_000_000


@pytest.fixture
def store() -> InMemoryRefStore:
    return InMemoryRefStore()


# --------------------------------------------------------------------------- beating


def test_a_beat_publishes_a_stamped_ref(store):
    ref = beat(store, 'boxA', NOW)
    assert ref == refs.heartbeat_ref('boxA', NOW)
    assert ref in store.refs


def test_a_beat_points_at_an_EXISTING_commit(store):
    """A marker ref says everything in its name, so it must not mint an object. A per-tick
    namespace that grows the repository is one somebody eventually turns off."""
    assert store.refs[beat(store, 'boxA', NOW)] == store.head()


def test_a_beat_COLLECTS_the_runners_older_stamps(store):
    """One ref per runner, not one per minute."""
    beat(store, 'boxA', NOW - 60)
    beat(store, 'boxA', NOW)
    assert list(store.refs) == [refs.heartbeat_ref('boxA', NOW)]


def test_the_prune_LISTS_rather_than_assuming_the_previous_stamp(store):
    """A tick that crashed after writing leaves a stamp nothing remembers -- and the stalest one is
    exactly the one a reader must not see."""
    store.refs[refs.heartbeat_ref('boxA', NOW - 9999)] = 'orphan-from-a-crashed-tick'
    beat(store, 'boxA', NOW)
    assert list(store.refs) == [refs.heartbeat_ref('boxA', NOW)]


def test_a_beat_does_not_touch_ANOTHER_runners_stamps(store):
    """The prune is scoped to this runner. Scoped one segment wider it would erase the fleet, which
    is the same mutual-erasure incident the salted runner id exists to prevent, from the other
    side."""
    beat(store, 'boxB', NOW - 30)
    beat(store, 'boxA', NOW)
    assert refs.heartbeat_ref('boxB', NOW - 30) in store.refs


def test_a_beat_retries_before_giving_up(store, monkeypatch):
    """A transient push failure must not cost a beat: the fleet's read of this box is binary."""
    attempts = {'n': 0}
    real = store.write

    def flaky(ref, commit):
        attempts['n'] += 1
        return (False, 'transient') if attempts['n'] < 3 else real(ref, commit)

    monkeypatch.setattr(store, 'write', flaky)
    assert beat(store, 'boxA', NOW, tries=3)
    assert attempts['n'] == 3


def test_a_FAILED_beat_RAISES_rather_than_returning(store):
    """A warning on an unchanged success return is the forbidden shape: the caller has to be able to
    tell, without reading a log, that this box is now invisible to the fleet."""
    store.fail_writes = 'remote: permission denied'
    with pytest.raises(BeatFailed):
        beat(store, 'boxA', NOW)


def test_a_FAILED_beat_KEEPS_THE_PREVIOUS_STAMP(store):
    """THE INCIDENT, and it is the sharpest test in this file. Pruning after a failed write empties
    the namespace, this runner reads as DEAD to the whole fleet, and every scheduled tier is
    reported unservable by a box that can serve all of them. Observed live."""
    beat(store, 'boxA', NOW - 60)
    store.fail_writes = 'remote: permission denied'
    with pytest.raises(BeatFailed):
        beat(store, 'boxA', NOW)
    assert refs.heartbeat_ref('boxA', NOW - 60) in store.refs, 'the last good stamp was collected'


def test_a_FAILED_beat_carries_the_transports_own_words(store):
    """Naming only the consequence produced two occurrences and no way to act on either: an expired
    token, a partition and a protected branch all read identically."""
    store.fail_writes = 'remote: pre-receive hook declined'
    with pytest.raises(BeatFailed, match='pre-receive hook declined'):
        beat(store, 'boxA', NOW)


def test_the_write_comes_BEFORE_the_prune(store):
    """The order is the property. Asserted on the log because the final state is identical either
    way -- and the wrong order is only visible on the run where the write fails."""
    beat(store, 'boxA', NOW - 60)
    store.log.clear()
    beat(store, 'boxA', NOW)
    assert store.log[0].startswith('write ')
    assert any(entry.startswith('delete ') for entry in store.log)


# --------------------------------------------------------------------------- reading a beat


def test_the_last_beat_is_the_NEWEST_stamp(store):
    store.refs[refs.heartbeat_ref('boxA', NOW - 60)] = 'x'
    store.refs[refs.heartbeat_ref('boxA', NOW)] = 'x'
    assert last_beat(store, 'boxA') == NOW


def test_the_last_beat_compares_NUMERICALLY(store):
    """Nine digits and ten: sorted as strings the older stamp wins, and the runner reads as older
    than it is at exactly the moment the epoch gains a digit."""
    store.refs[refs.heartbeat_ref('boxA', 999_999_999)] = 'x'
    store.refs[refs.heartbeat_ref('boxA', 1_000_000_000)] = 'x'
    assert last_beat(store, 'boxA') == 1_000_000_000


def test_a_runner_that_never_beat_is_None(store):
    """Not zero. Zero is a time, and a time compares as very stale rather than as absent."""
    assert last_beat(store, 'ghost') is None


# --------------------------------------------------------------------------- who is live


def test_a_fresh_beat_is_live(store):
    beat(store, 'boxA', NOW)
    assert live_runners(store, NOW + 5) == {'boxA'}


def test_a_stale_beat_is_not(store):
    beat(store, 'boxA', NOW)
    assert live_runners(store, NOW + 7200) == set()


def test_NEWEST_WINS_so_history_does_not_kill_a_healthy_runner(store):
    """The layout keeps several epochs per runner, so "every beat is recent" calls a healthy box
    dead the moment it has any history. Any fresh beat is proof of life; the rest is its log."""
    store.refs[refs.heartbeat_ref('boxA', NOW - 99999)] = 'x'
    store.refs[refs.heartbeat_ref('boxA', NOW)] = 'x'
    assert live_runners(store, NOW + 5) == {'boxA'}


def test_junk_in_the_namespace_is_SKIPPED_not_fatal(store):
    """This feeds operator-facing reports, and a traceback here hides the summary a reader came
    for."""
    store.refs['refs/ci/heartbeat/boxA/not-a-number'] = 'x'
    beat(store, 'boxB', NOW)
    assert live_runners(store, NOW + 5) == {'boxB'}


def test_an_UNREACHABLE_remote_RAISES_rather_than_reporting_an_empty_fleet(store):
    """THE MEASURED DEFECT. A swallowed listing error is indistinguishable from an empty namespace,
    so an offline box reported that nobody in the fleet was alive and refused work on that basis."""
    beat(store, 'boxA', NOW)
    store.unreachable = True
    with pytest.raises(RefUnreachable):
        live_runners(store, NOW + 5)


# --------------------------------------------------------------------------- capability


def test_capabilities_are_ONE_REF_EACH(store):
    publish_capabilities(store, 'boxA', ['native_fea', 'big_ram'])
    assert set(store.refs) == {
        refs.capability_ref('boxA', 'native_fea'),
        refs.capability_ref('boxA', 'big_ram'),
    }


def test_a_capability_no_longer_reported_is_WITHDRAWN(store):
    """The direction that matters: a stale capability does not fail loudly, it wins a job the box
    can no longer run."""
    publish_capabilities(store, 'boxA', ['native_fea', 'big_ram'])
    publish_capabilities(store, 'boxA', ['native_fea'])
    assert set(store.refs) == {refs.capability_ref('boxA', 'native_fea')}


def test_publishing_does_not_withdraw_ANOTHER_runners_capabilities(store):
    publish_capabilities(store, 'boxB', ['femm_like'])
    publish_capabilities(store, 'boxA', ['native_fea'])
    assert refs.capability_ref('boxB', 'femm_like') in store.refs


def test_the_union_covers_every_LIVE_runner(store):
    beat(store, 'boxA', NOW)
    beat(store, 'boxB', NOW)
    publish_capabilities(store, 'boxA', ['native_fea'])
    publish_capabilities(store, 'boxB', ['big_ram'])
    assert fleet_capabilities(store, NOW + 5) == {'native_fea', 'big_ram'}


def test_a_DEAD_runners_capabilities_do_NOT_count(store):
    """A machine decommissioned last year would otherwise go on covering a tier forever -- a
    coverage claim with no machine behind it."""
    beat(store, 'boxA', NOW - 99999)
    publish_capabilities(store, 'boxA', ['native_fea'])
    assert fleet_capabilities(store, NOW) == set()


def test_a_runner_that_never_BEAT_does_not_count_either(store):
    """Publishing a capability is not a liveness signal. Two signals would be free to disagree
    about who is alive, and the fleet would have two answers to its load-bearing question."""
    publish_capabilities(store, 'ghost', ['native_fea'])
    assert fleet_capabilities(store, NOW) == set()


def test_an_UNREACHABLE_remote_is_an_EMPTY_UNION_here(store):
    """The deliberate asymmetry with `live_runners`. For a scheduler, "cannot confirm anyone" and
    "nobody can do it" lead to the same correct action -- serve nothing, retry -- so this reader
    catches what that one raises. Stated as a test so the difference is a decision on record."""
    beat(store, 'boxA', NOW)
    publish_capabilities(store, 'boxA', ['native_fea'])
    store.unreachable = True
    assert fleet_capabilities(store, NOW + 5) == set()
