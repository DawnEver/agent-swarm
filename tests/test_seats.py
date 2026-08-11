"""The SEAT layer: a tool name, a count the caller declares, and the item they are taken on.

**THE ARBITRATION IS NOT TESTED HERE.** It is one implementation and it is raced in
`tests/test_claim.py`, at `slots=1`, `3` and `7`, four rounds each. Re-racing it through this class
would be a second copy of the evidence for a mechanism that no longer has a second copy of the code,
and the copy that drifts is always the one nobody is looking at.

What is left is what the core must not know and therefore cannot check: HOW MANY of a thing a site
bought, WHAT it is called, and that this pool hands the core the right number. That last one is the
only reason a race survives in this file at all -- a `SeatPool` that passed `slots=1` would fail no
test in `test_claim.py`.
"""

from __future__ import annotations

import io
import threading
import time
import tokenize
from pathlib import Path

import pytest

from agent_swarm import seats as seats_module
from agent_swarm.claim import LeaseLost
from agent_swarm.forge_store import Role
from agent_swarm.seats import (
    DeclaredSeats,
    SeatPool,
    UnknownTool,
    find_seat_item,
    provision_seat_item,
    seat_item_title,
)
from agent_swarm.testing import RecordingForge

NAMESPACE = 'seats-test'
TOOL = 'somevendor'
SEATS = 3


@pytest.fixture
def forge() -> RecordingForge:
    return RecordingForge()


@pytest.fixture
def catalog() -> DeclaredSeats:
    return DeclaredSeats({TOOL: SEATS})


def _pool(forge: RecordingForge, catalog: DeclaredSeats, *, tool: str = TOOL, **kwargs) -> SeatPool:
    number = provision_seat_item(forge, namespace=NAMESPACE, tool=tool, role=Role.SUBMITTER)
    return SeatPool(forge, tool=tool, item_number=number, catalog=catalog, **kwargs)


class TestTheCatalogueIsTheCallersAndOnlyTheCallers:
    def test_an_undeclared_tool_RAISES_rather_than_defaulting(self, catalog):
        """A default of 1 serialises a site that bought four; a larger one invents capacity."""
        with pytest.raises(UnknownTool, match='no seat count is declared'):
            catalog.seats('never-declared')

    def test_an_acquire_for_an_undeclared_tool_RAISES_rather_than_refusing(self, forge):
        """`None` means "not now" and must never mean "misconfigured": a caller that retried a
        `None` forever would be waiting on a licence nobody ever declared.
        """
        pool = SeatPool(forge, tool='undeclared', item_number=1, catalog=DeclaredSeats({}))
        with pytest.raises(UnknownTool):
            pool.acquire(owner='someone')

    @pytest.mark.parametrize('count', [0, -1])
    def test_a_NON_POSITIVE_count_is_refused_at_CONSTRUCTION(self, count):
        """Not at the acquire: there, every attempt is refused and a healthy licence reads as full."""
        with pytest.raises(ValueError, match='seats'):
            DeclaredSeats({TOOL: count})

    def test_the_count_is_read_at_EVERY_acquire_not_cached(self, forge):
        """A site that buys two more seats edits configuration. A pool that cached the old number
        would ration to it until every process on the fleet restarted, and nothing would say why.
        """
        counts = {TOOL: 1}
        pool = _pool(forge, DeclaredSeats(counts))
        assert pool.acquire(owner='a') is not None
        assert pool.acquire(owner='b') is None
        counts[TOOL] = 2
        assert pool.acquire(owner='b') is not None

    def test_no_tool_is_named_in_the_CODE_of_this_module(self):
        """The package must not know what FEMM is or how many seats a site bought.

        **SEARCH SCOPE: the CODE tokens of `agent_swarm/seats.py`** -- strings and comments
        excluded, every other module excluded. `claim.py` draws the same line and states why: the
        docstring CITES the motivating vendor because that is where the measurement came from, and
        a test that banned the word everywhere would be satisfied by deleting the evidence. What
        must not exist is a name the code can BRANCH on.
        """
        source = Path(seats_module.__file__).read_text(encoding='utf-8')
        code = [
            token.string
            for token in tokenize.generate_tokens(io.StringIO(source).readline)
            if token.type not in (tokenize.STRING, tokenize.COMMENT)
        ]
        banned = {'femm', 'jmag', 'ansys', 'motronics', 'gitea', 'github'}
        offenders = sorted({t for t in code if t.lower() in banned})
        assert not offenders, f'a tool or vendor is named in the seat logic: {offenders}'


class TestThePoolHandsTheCoreTheRIGHTNumber:
    """THE ONLY RACE THAT BELONGS IN THIS FILE. A pool wired to `slots=1` -- or to the wrong tool's
    count -- passes every test in `test_claim.py`, because the arbitration would be flawless on the
    wrong number. Nothing but a race through `SeatPool` discriminates it.
    """

    @pytest.mark.parametrize('round_number', range(4))
    def test_the_DECLARED_seat_count_is_what_sixteen_racers_get(self, catalog, round_number):
        forge = RecordingForge()
        pool = _pool(forge, catalog)
        winners: list[str] = []
        lock = threading.Lock()
        barrier = threading.Barrier(16)

        def attempt(n: int) -> None:
            barrier.wait()
            if pool.acquire(owner=f'runner-{n:02d}') is not None:
                with lock:
                    winners.append(f'runner-{n:02d}')

        threads = [threading.Thread(target=attempt, args=(i,)) for i in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(winners) == SEATS, f'{len(winners)} racers held {SEATS} declared seats: {sorted(winners)}'
        assert {c.owner for c in pool.holders().claims} == set(winners)

    def test_two_TOOLS_do_not_contend(self, forge):
        """One item per tool, or a four-seat licence and a two-seat rig share a count."""
        catalog = DeclaredSeats({'alpha': 1, 'beta': 1})
        alpha = _pool(forge, catalog, tool='alpha')
        beta = _pool(forge, catalog, tool='beta')
        assert alpha.acquire(owner='x') is not None
        assert beta.acquire(owner='x') is not None
        assert alpha.acquire(owner='y') is None

    def test_the_pool_passes_its_LEASE_through(self, forge, catalog):
        """A pool that dropped it would silently use the core's argument, and a dead holder would
        park a seat for a length nobody chose.
        """
        pool = _pool(forge, catalog, lease_seconds=0.05)
        for i in range(SEATS):
            assert pool.acquire(owner=f'dying-{i}') is not None
        assert pool.acquire(owner='waiting') is None
        time.sleep(0.08)
        assert pool.acquire(owner='waiting') is not None

    def test_a_seat_is_a_HELD_and_beats_and_releases_like_any_other(self, forge, catalog):
        """The seat layer adds no lifecycle of its own -- one heartbeat, one release, one type."""
        pool = _pool(forge, catalog, lease_seconds=0.2)
        seat = pool.acquire(owner='holder')
        first_expiry = seat.expires_at
        time.sleep(0.05)
        assert seat.renew() > first_expiry
        seat.release()
        assert pool.holders().claims == ()
        time.sleep(0.25)
        with pytest.raises(LeaseLost):
            seat.renew()


class TestProvisioningIsTheSubmittersAlone:
    def test_a_RUNNER_may_not_create_the_pool_item(self, forge):
        """Sixteen runners creating sixteen items would each arbitrate a full N seats -- 16N seats
        granted against a licence with N, with the protocol working perfectly on each wrong item.
        """
        with pytest.raises(PermissionError, match='16N'):
            provision_seat_item(forge, namespace=NAMESPACE, tool=TOOL, role=Role.RUNNER)

    def test_a_missing_pool_item_is_NOT_VISIBLE_and_not_None(self, forge):
        """`None` is what every caller turned into a create. The type makes that unwritable."""
        found = find_seat_item(forge, namespace=NAMESPACE, tool=TOOL)
        assert repr(found) == 'NOT_VISIBLE'
        assert not found

    def test_the_item_is_found_by_the_ONE_title_spelling(self, forge):
        number = provision_seat_item(forge, namespace=NAMESPACE, tool=TOOL, role=Role.SUBMITTER)
        assert find_seat_item(forge, namespace=NAMESPACE, tool=TOOL) == number
        assert forge.work_item(number).title == seat_item_title(NAMESPACE, TOOL)
