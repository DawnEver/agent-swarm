"""N SEATS AGAINST A REAL SERVER. The measurement the offline suite cannot make.

WHY THIS FILE EXISTS, and it is a gap that was named in prose before it was a test. The arbitration
in `agent_swarm.claim` was measured live at ONE slot -- Gitea 1.26.4, 16 threads off a
`threading.Barrier`, four rounds, exactly one winner each, ids monotonic and gapless, ~280 ms under
16-way contention. The generalisation to N rests on an argument: your rank among the live claims can
only FALL, because a later comment carries a higher server-assigned id and an expired claim never
returns to the live set. **That argument is not a measurement**, and until this file existed the gap
lived only in a docstring -- something a reader had to remember rather than something anybody runs.

A MARKED TEST IS THE DIFFERENCE. It is deselected with the rest of the live tier, and `conftest`
prints how many were NOT RUN, so the missing evidence is visible in every default run instead of
being invisible until someone re-reads the module. When the live tier is next run, the answer
arrives by itself.

WHAT ONLY A REAL SERVER CAN ANSWER, and it is precisely the thing `RecordingForge` cannot: the
double assigns comment ids from a counter under a lock and every read sees every completed write --
the protocol's two preconditions, true BY CONSTRUCTION. A green offline run is evidence that the
arbitration is correct GIVEN those properties, and no amount of it is evidence that a deployment has
them. Read-after-write consistency at N is the same precondition as at 1, but the CONSEQUENCE of
losing it is worse: at one slot a stale read duplicates a job, at N it admits an (N+1)th holder to a
licence that has N, and the licence server refuses in the middle of somebody's 25-minute run.

    pytest -m live_forge tests/test_seat_contention_live.py

It creates items in a throwaway namespace and retires them, so it is safe against the real
deployment. IT IS NOT FREE: 16 racers x 4 rounds is roughly 180 API calls, most of them writes.
"""

from __future__ import annotations

import os
import secrets
import threading

import pytest

from agent_swarm.claim import Held, decode_claim
from agent_swarm.forge import DEFAULT_GITEA_BASE_URL, GiteaForge
from agent_swarm.forge_store import ForgeStore, Role
from agent_swarm.seats import DeclaredSeats, SeatPool, provision_seat_item

#: The declared seat count under test. THREE, not two: with two seats and a bug that admits the
#: lowest TWO ranks plus anybody whose rank equals the count, the winner count is 3 and the error is
#: one -- which reads like noise. At three the same bug yields 4 and the shape is unmistakable.
SEATS = 3

#: Racers per round. Sixteen, the same width the one-slot protocol was measured at, so the two
#: measurements are comparable rather than merely both green.
RACERS = 16

#: Independent rounds. FOUR, and not one, for the reason the offline suite gives and this file
#: inherits: a single round electing the right number of winners is also what a BROKEN protocol does
#: most of the time. Each round gets a FRESH item, so no round can be carried by the previous one.
ROUNDS = 4


@pytest.fixture
def live():
    """A real client against a throwaway namespace, cleaned up afterwards.

    The namespace prefixes every seat item's title, and `purge_namespace` matches on that prefix, so
    the cleanup reaches the seat items this file creates without being told about them.
    """
    repo = os.environ.get('SWARM_REPO') or 'Tianjie-Zou-Team/motronics-studio'
    forge = GiteaForge(os.environ.get('SWARM_BASE_URL') or DEFAULT_GITEA_BASE_URL, repo, username='swarm-agent')
    namespace = f'seats-{secrets.token_hex(3)}'
    yield forge, namespace
    ForgeStore(namespace, forge, role=Role.SUBMITTER).purge_namespace()


def _race(pool: SeatPool, *, racers: int) -> list[Held]:
    """`racers` threads released together onto one pool. Returns what each of them was granted.

    RELEASED FROM A BARRIER, not started in a loop. Threads started sequentially against a server
    with ~60 ms of round-trip latency do not overlap at all -- the first would finish before the
    last began, and the test would be a sequence wearing the costume of a race.
    """
    won: list[Held] = []
    lock = threading.Lock()
    barrier = threading.Barrier(racers)

    def attempt(n: int) -> None:
        barrier.wait()
        seat = pool.acquire(owner=f'racer-{n:02d}')
        if seat is not None:
            with lock:
                won.append(seat)

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(racers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return won


@pytest.mark.live_forge
def test_exactly_N_seats_are_granted_per_round_against_a_real_forge(live):
    """THE MEASUREMENT THIS FILE EXISTS FOR. Per-round winner counts, asserted, not summed.

    ASSERTED PER ROUND AND REPORTED PER ROUND. A test that only asserted the total (12 across four
    rounds) would pass for 3/3/3/3 and equally for 1/5/3/3 -- and the second is a protocol that is
    wrong twice and lucky once. The per-round list goes into the failure message so a red run says
    WHICH round broke, which is the difference between a bug report and a rerun.
    """
    forge, namespace = live
    catalog = DeclaredSeats({'probe': SEATS})
    per_round: list[int] = []

    for round_number in range(ROUNDS):
        # A FRESH ITEM PER ROUND. Reusing one would let round 2 inherit round 1's debris, and a
        # protocol that leaks a loser's comment would then fail for a reason this test cannot name.
        tool = f'probe-r{round_number}'
        number = provision_seat_item(forge, namespace=namespace, tool=tool, role=Role.SUBMITTER)
        pool = SeatPool(forge, tool=tool, item_number=number, catalog=DeclaredSeats({tool: SEATS}))
        won = _race(pool, racers=RACERS)
        per_round.append(len(won))
        for seat in won:
            seat.release()

    assert catalog.seats('probe') == SEATS  # the declared number this run was judged against
    assert per_round == [SEATS] * ROUNDS, (
        f'seat grants per round were {per_round}, expected {[SEATS] * ROUNDS}. A round granting MORE '
        f'than {SEATS} means the deployment admitted an extra holder -- check read-after-write '
        f'consistency before checking the arbitration. A round granting FEWER means racers were '
        f'refused seats that were free.'
    )


@pytest.mark.live_forge
def test_a_LOSER_leaves_no_seat_comment_behind_on_a_real_forge(live):
    """The withdrawal, measured where it matters. A refused claim comment left behind ACTIVATES
    LATER: once a holder releases, it becomes one of the lowest N live comments and a racer that was
    told "no" is counted as a seat holder while running nothing. On a licence that is worse than on
    a job, because nothing downstream ever notices a leaked seat.

    Counted on the SERVER's comment list rather than on our own return values -- the whole point is
    what the next reader will see, not what this process believes it did.
    """
    forge, namespace = live
    tool = 'probe-withdraw'
    number = provision_seat_item(forge, namespace=namespace, tool=tool, role=Role.SUBMITTER)
    pool = SeatPool(forge, tool=tool, item_number=number, catalog=DeclaredSeats({tool: SEATS}))

    won = _race(pool, racers=RACERS)
    assert len(won) == SEATS

    live_claims = [c for c in (decode_claim(c.body, comment_id=c.id) for c in forge.comments(number)) if c]
    assert len(live_claims) == SEATS, (
        f'{len(live_claims)} claim comments survive for {SEATS} seats: {RACERS - SEATS} losers should '
        f'have withdrawn. Every extra one is a seat that activates the moment a holder releases.'
    )
    assert {c.owner for c in live_claims} == {seat.owner for seat in won}


@pytest.mark.live_forge
def test_the_server_assigns_comment_ids_MONOTONICALLY_under_N_way_contention(live):
    """THE PRECONDITION, PROBED DIRECTLY RATHER THAN INFERRED FROM THE OUTCOME.

    The whole protocol rests on the ordering key being the server's, assigned at insert, increasing.
    The test above would go green on a server that violated this and got lucky; this one asks the
    question on its own, so a green race and a sound precondition stay distinguishable.

    Ids need not be GAPLESS -- another repo sharing the deployment consumes them too -- so only
    uniqueness and ordering are asserted. The one-slot measurement saw 161->176 for 16 posts, which
    was gapless, and pinning that would make this test fail for a reason that is nobody's bug.
    """
    forge, namespace = live
    tool = 'probe-ordering'
    number = provision_seat_item(forge, namespace=namespace, tool=tool, role=Role.SUBMITTER)
    pool = SeatPool(forge, tool=tool, item_number=number, catalog=DeclaredSeats({tool: RACERS}))

    # EVERY racer wins here (seats == racers), so all sixteen comments survive to be inspected --
    # a race where thirteen withdraw would leave nothing to check the ordering of.
    won = _race(pool, racers=RACERS)
    assert len(won) == RACERS

    ids = [c.id for c in forge.comments(number)]
    assert len(set(ids)) == len(ids), f'the server REUSED a comment id: {ids}'
    assert ids == sorted(ids), f'comments came back out of id order: {ids}'
