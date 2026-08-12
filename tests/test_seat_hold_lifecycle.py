"""`hold_for_class` -- the twelve decisions every seat consumer used to write for itself.

WHY THIS FILE EXISTS AT ALL, and the history is the argument. `SeatPool.acquire` was the whole
public surface, so the first real consumer (motronics' gate, holding a JMAG licence seat) wrote 300
lines around it: derive the tool from a job class, exempt an unbounded tool, keep a five-minute
lease alive across a thirty-minute run, separate "no seat now" from "declared nowhere" from "forge
down", release on the way out, and report which happened. MEASURED when the misplacement was
found: 359 lines, of which 2 named that project. It also re-derived its own beat cadence as
`lease / 8`, which is precisely the defect `claim.beat_interval`'s docstring forbids in prose --
written by someone who had read it. A mechanism nobody can reach from the package gets rewritten,
and it gets rewritten with the bugs the original already paid for.

`RecordingForge` MODELS THE PRECONDITION and says so: server-assigned monotonic comment ids, and
every read sees every completed write. A green here is evidence about THIS wiring, never about a
real forge -- that claim belongs to `test_seat_contention_live.py`.
"""

from __future__ import annotations

import time

import pytest

from agent_swarm.forge import ForgeError
from agent_swarm.forge_store import Role
from agent_swarm.seats import (
    PROCEED_WHEN_UNREACHABLE,
    REFUSE_WHEN_UNREACHABLE,
    SEATS_HELD,
    SEATS_LOST,
    SEATS_NOT_APPLICABLE,
    SEATS_UNARBITRATED,
    SEATS_UNLIMITED,
    DeclaredSeats,
    SeatRefused,
    hold_for_class,
    provision_seat_item,
)
from agent_swarm.testing import RecordingForge

NAMESPACE = 'testsite'


@pytest.fixture
def forge():
    return RecordingForge(username='swarm-submitter')


@pytest.fixture
def catalog():
    """Two seats for a floating tool, one tool declared unbounded. Small enough to exhaust."""
    return DeclaredSeats({'jmag': 2}, ('femm',))


def _provision(forge):
    return provision_seat_item(forge, namespace=NAMESPACE, tool='jmag', role=Role.SUBMITTER)


def _hold(forge, catalog, owner='x', job_class='vendor:jmag', **kw):
    kw.setdefault('when_unreachable', PROCEED_WHEN_UNREACHABLE)
    return hold_for_class(job_class, owner=owner, namespace=NAMESPACE, catalog=catalog, forge=forge, **kw)


class TestOnlySeatedToolsReachTheForge:
    """Adding fleet arbitration must not make unrelated work, or a deliberately-unlimited tool,
    depend on a remote being up.
    """

    def test_a_class_naming_no_tool_touches_nothing(self, forge, catalog):
        with _hold(forge, catalog, job_class='expensive') as state:
            pass
        assert state() == SEATS_NOT_APPLICABLE
        assert not forge.items

    def test_an_unlimited_tool_touches_nothing(self, forge, catalog):
        with _hold(forge, catalog, job_class='vendor:femm') as state:
            pass
        assert state() == SEATS_UNLIMITED
        assert not forge.items

    def test_an_unlimited_tool_does_not_even_BUILD_the_forge(self, catalog):
        """THE FACTORY EXISTS FOR THIS. A caller whose forge construction reads config, or fails,
        must not pay for it to be told the tool is exempt.
        """

        def _explode():
            msg = 'a forge was built for a tool that needs none'
            raise AssertionError(msg)

        with hold_for_class(
            'vendor:femm',
            owner='x',
            namespace=NAMESPACE,
            catalog=catalog,
            forge=_explode,
            when_unreachable=PROCEED_WHEN_UNREACHABLE,
        ) as state:
            pass
        assert state() == SEATS_UNLIMITED


class TestTheSeatsAreRationed:
    def test_the_last_seat_is_the_last_seat(self, forge, catalog):
        """N holders in, N+1 REFUSED. The refusal is an exception rather than a falsy return, so a
        caller cannot ignore it by accident.
        """
        _provision(forge)
        open_holds = []
        for i in range(2):
            cm = _hold(forge, catalog, owner=f'r{i}')
            open_holds.append((cm, cm.__enter__()))
        assert [s() for _, s in open_holds] == [SEATS_HELD, SEATS_HELD]

        with pytest.raises(SeatRefused, match='not now'), _hold(forge, catalog, owner='r2'):
            pass

        for cm, _ in open_holds:
            cm.__exit__(None, None, None)

    def test_a_released_seat_is_reusable(self, forge, catalog):
        """The converse, and NOT the same assertion. A pool that refused forever after its first N
        holds would pass the test above and starve the fleet by the end of the day.
        """
        _provision(forge)
        for i in range(5):  # more rounds than there are seats
            with _hold(forge, catalog, owner=f'r{i}') as state:
                assert state() == SEATS_HELD

    def test_the_seat_comes_back_even_when_the_work_raises(self, forge, catalog):
        _provision(forge)

        def _die():
            with _hold(forge, catalog, owner='dies'):
                raise ZeroDivisionError

        for _ in range(2):
            with pytest.raises(ZeroDivisionError):
                _die()
        with _hold(forge, catalog, owner='after') as state:
            assert state() == SEATS_HELD

    def test_a_pool_nobody_provisioned_refuses_rather_than_creating_one(self, forge, catalog):
        """THE 16N DEFECT. If a runner created the item on demand, sixteen runners would create
        sixteen items and each would arbitrate a full pool on its own -- N seats granted sixteen
        times, the protocol working flawlessly on each of sixteen wrong items.
        """
        with pytest.raises(SeatRefused, match='submitter provisions it once'), _hold(forge, catalog):
            pass
        assert not forge.items

    def test_a_tool_declared_nowhere_refuses_and_names_both_lists(self, forge):
        with pytest.raises(SeatRefused) as exc, _hold(forge, DeclaredSeats({'jmag': 1}), job_class='vendor:ansys'):
            pass
        assert 'unlimited tools' in str(exc.value)


class TestTheUnreachablePolicyIsTheCALLERS:
    """NO DEFAULT, and that is the point: both answers are defensible and the choice belongs to
    whoever owns the licence.
    """

    class _Down:
        username = 'swarm-runner'

        def list_work_items(self, **_):
            msg = 'connection refused'
            raise ForgeError(msg)

    def test_proceed_runs_and_says_unarbitrated(self, catalog):
        with _hold(self._Down(), catalog, when_unreachable=PROCEED_WHEN_UNREACHABLE) as state:
            ran = True
        assert ran
        assert state() == SEATS_UNARBITRATED

    def test_refuse_refuses(self, catalog):
        with (
            pytest.raises(SeatRefused, match='unreachable'),
            _hold(self._Down(), catalog, when_unreachable=REFUSE_WHEN_UNREACHABLE),
        ):
            pass

    def test_an_unstated_policy_is_a_ValueError_not_a_guess(self, forge, catalog):
        with (
            pytest.raises(ValueError, match='no default'),
            hold_for_class(
                'vendor:jmag', owner='x', namespace=NAMESPACE, catalog=catalog, forge=forge, when_unreachable='maybe'
            ),
        ):
            pass

    def test_unarbitrated_and_held_are_DIFFERENT_values(self):
        """If these collapsed, work done with the forge down would be indistinguishable from work
        arbitrated against the fleet -- the whole reason the state is reported at all.
        """
        assert len({SEATS_NOT_APPLICABLE, SEATS_UNLIMITED, SEATS_HELD, SEATS_UNARBITRATED, SEATS_LOST}) == 5


class TestALapsedLeaseIsReported:
    def test_a_hold_whose_comment_vanishes_reports_LOST(self, forge, catalog):
        """A lapsed lease says the RESOURCE was over-committed. It says nothing about the work, so
        it is reported without discarding it -- but it must not still say `held`, which would be the
        state lying about the one thing it exists to disclose.
        """
        _provision(forge)
        with _hold(forge, catalog, lease_seconds=0.4) as state:
            assert state() == SEATS_HELD
            number, comment_id = _only_claim(forge)
            forge.delete_comment(number, comment_id)
            _wait_until(lambda: state() == SEATS_LOST, timeout=6.0)
        assert state() == SEATS_LOST


def _only_claim(forge):
    for number in forge.items:
        comments = list(forge.comments(number))
        if comments:
            return number, comments[0].id
    msg = 'no claim comment exists -- the seat was never actually taken'
    raise AssertionError(msg)


def _wait_until(predicate, *, timeout: float) -> None:
    """Poll rather than sleep a fixed amount: a fixed sleep is either flaky or slow, and on a busy
    box it is both.
    """
    waited = 0.0
    while waited < timeout:
        if predicate():
            return
        time.sleep(0.05)
        waited += 0.05
    msg = f'condition never held within {timeout}s'
    raise AssertionError(msg)
