"""THE arbitration contract. One implementation, so one place that races it.

WHY RACING AND NOT A SEQUENCE. `tests/test_store.py` states it for one slot and it is more true for
N: a sequential probe -- take, take, take, assert the fourth is refused -- passes for a real
arbitration AND for last-write-wins, for a mutex that forgot to count, and for an implementation
that simply returns None after three calls. Only overlapping calls tell them apart.

WHY MORE THAN ONE ROUND. A single round electing the right number of winners is also what a BROKEN
protocol does most of the time. Every race below runs FOUR rounds, and each round that can uses a
fresh forge and a fresh item, so a round cannot be carried by state the previous one left behind.

WHY THE SLOT COUNTS ARE PARAMETRISED. `slots=1` is the job claim and `slots=3` is a licence; they
are the same code and the test says so by running the same race at both. A separate one-slot test
would be the second copy of the arbitration coming back as a second copy of its evidence.
"""

from __future__ import annotations

import inspect
import io
import threading
import time
import tokenize
from pathlib import Path

import pytest

from agent_swarm import claim as claim_module
from agent_swarm.claim import (
    Arbiter,
    beat_interval,
    ArbitrationUnsound,
    Held,
    Holders,
    LeaseLost,
    decode_claim,
    encode_claim,
)
from agent_swarm.forge import GiteaForge
from agent_swarm.testing import RecordingForge


def _arbiter(forge: RecordingForge, *, slots: int = 1, lease_seconds: float = 300.0) -> Arbiter:
    number = forge.create_work_item(title='[swarm] contended', body='', labels=())
    return Arbiter(forge, item_number=number, slots=slots, lease_seconds=lease_seconds)


@pytest.fixture
def forge() -> RecordingForge:
    return RecordingForge()


class TestTheClaimCommentIsReadableBothWays:
    def test_it_round_trips(self):
        claim = decode_claim(encode_claim(owner='runner-a', expires_at=1000.0), comment_id=7)
        assert claim.owner == 'runner-a'
        assert claim.expires_at == pytest.approx(1000.0)
        assert claim.comment_id == 7

    def test_it_is_readable_by_a_HUMAN_scrolling_the_issue(self):
        """The forge is also the UI. An operator must be able to see who holds a job, and until
        when, without running a tool.
        """
        assert encode_claim(owner='runner-a', expires_at=1000.0).startswith('CLAIM ')
        assert 'runner-a' in encode_claim(owner='runner-a', expires_at=1000.0)

    def test_an_owner_containing_a_SPACE_survives_the_round_trip(self):
        """The expiry is encoded first so the owner can be the whole remainder. Truncating an owner
        at its first word would give two machines one identity -- and a release by either would
        then free the other's claim.
        """
        assert decode_claim(encode_claim(owner='box 7 runner a', expires_at=1.0)).owner == 'box 7 runner a'

    def test_a_NON_claim_comment_decodes_to_None(self):
        """Claims and verdicts share one comment stream, so 'this is not a claim' must be an
        ordinary answer rather than an error.
        """
        assert decode_claim('**PASS**\n\n```\n10646 passed\n```') is None

    def test_a_MALFORMED_claim_RAISES_instead_of_decoding_to_None(self):
        """THE DISTINCTION IS THE WHOLE POINT. If an unreadable claim returned `None` it would be
        skipped exactly like a verdict comment -- so a live claim in a format this version cannot
        read would be invisible, and a second runner would take a running job.
        """
        with pytest.raises(ValueError, match='claim'):
            decode_claim('CLAIM not-a-number runner-a')
        with pytest.raises(ValueError, match='claim'):
            decode_claim('CLAIM 12345')

    def test_a_fresh_claim_is_not_expired(self):
        assert decode_claim(encode_claim(owner='a', expires_at=time.time() + 300)).is_expired(now=time.time()) is False

    def test_a_claim_past_its_expiry_IS_expired(self):
        assert decode_claim(encode_claim(owner='a', expires_at=1000.0)).is_expired(now=1000.5) is True

    def test_the_boundary_instant_is_still_HELD(self):
        assert decode_claim(encode_claim(owner='a', expires_at=1000.0)).is_expired(now=1000.0) is False


class TestExactlyNHoldersUnderRealConcurrency:
    """THE DISCRIMINATING TESTS, and the reason this module exists as one implementation."""

    @pytest.mark.parametrize('slots', [1, 3, 7])
    @pytest.mark.parametrize('round_number', range(4))
    def test_exactly_N_of_sixteen_racers_win(self, slots, round_number):
        """Sixteen threads off a `threading.Barrier` onto ONE fresh item, at three slot counts.

        `slots=1` IS the job claim: the store now runs this exact code, so this is that contract's
        race too. `slots=7` is the awkward one -- nearly half the racers win -- and it is here
        because an off-by-one in the rank comparison survives 1 and 3 far more easily.
        """
        forge = RecordingForge()
        arbiter = _arbiter(forge, slots=slots)
        winners: list[str] = []
        lock = threading.Lock()
        barrier = threading.Barrier(16)

        def attempt(n: int) -> None:
            barrier.wait()
            if arbiter.take(owner=f'runner-{n:02d}') is not None:
                with lock:
                    winners.append(f'runner-{n:02d}')

        threads = [threading.Thread(target=attempt, args=(i,)) for i in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(winners) == slots, f'{len(winners)} racers held {slots} slots: {sorted(winners)}'
        assert {c.owner for c in arbiter.holders().claims} == set(winners)

    @pytest.mark.parametrize('round_number', range(4))
    def test_a_LOSER_leaves_no_comment_behind(self, round_number):
        """A refused claim comment left behind ACTIVATES LATER: once a holder releases it becomes
        one of the lowest N, and a racer that was told "no" reads itself as a holder while running
        nothing. On a licence that is worse than on a job -- a leaked seat is capacity nothing
        downstream ever notices.

        RUN AS A RACE, not as three sequential takes, because the debris a race leaves is what the
        next round of the same race trips over.
        """
        forge = RecordingForge()
        arbiter = _arbiter(forge, slots=3)
        held: list[Held] = []
        lock = threading.Lock()
        barrier = threading.Barrier(12)

        def attempt(n: int) -> None:
            barrier.wait()
            hold = arbiter.take(owner=f'r{n:02d}')
            if hold is not None:
                with lock:
                    held.append(hold)

        threads = [threading.Thread(target=attempt, args=(i,)) for i in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(held) == 3
        assert len(forge.comments(arbiter.item_number)) == 3, 'a loser left its claim comment behind'
        # And the discriminating half: releasing a holder must not promote a refused racer.
        winners = {h.owner for h in held}
        held[0].release()
        assert {c.owner for c in arbiter.holders().claims} <= winners

    def test_occupancy_never_exceeds_the_slots_across_repeated_rounds_on_ONE_item(self, forge):
        """The same item, four rounds, every winner releasing between them. A protocol that leaked a
        loser's comment fails on the SECOND round, not the first -- which is what one round misses.
        """
        arbiter = _arbiter(forge, slots=3)
        for _ in range(4):
            held: list[Held] = []
            lock = threading.Lock()
            barrier = threading.Barrier(8)

            def attempt(n: int, sink=held) -> None:
                barrier.wait()
                hold = arbiter.take(owner=f'r{n}')
                if hold is not None:
                    with lock:
                        sink.append(hold)

            threads = [threading.Thread(target=attempt, args=(i,)) for i in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            assert len(held) == 3
            for hold in held:
                hold.release()
            assert arbiter.holders().claims == ()

    def test_a_released_slot_is_TAKEABLE_again(self, forge):
        arbiter = _arbiter(forge, slots=3)
        holds = [arbiter.take(owner=f'h{i}') for i in range(3)]
        assert arbiter.take(owner='latecomer') is None
        holds[1].release()
        assert arbiter.take(owner='latecomer') is not None

    def test_a_RE_TAKE_by_a_current_holder_is_refused(self, forge):
        """The store contract demands it: a runner that lost track of its own claim must not reset
        the lease and keep a hung job locked forever. Beating is `renew`, deliberately a different
        call.
        """
        arbiter = _arbiter(forge, slots=1)
        arbiter.take(owner='runner-a')
        assert arbiter.take(owner='runner-a') is None
        assert arbiter.holders().claims[0].owner == 'runner-a'


class TestTheLeaseAndTheHeartbeat:
    def test_a_LAPSED_holder_frees_its_slot_without_anyone_intervening(self, forge):
        """A holder that dies must not park a slot for the whole fleet. The expiry is skipped, not
        swept -- there is no takeover path, and a second way to win is where N becomes N+1.
        """
        arbiter = _arbiter(forge, slots=3, lease_seconds=0.05)
        for i in range(3):
            assert arbiter.take(owner=f'dying-{i}') is not None
        assert arbiter.take(owner='waiting') is None
        time.sleep(0.08)
        assert arbiter.take(owner='waiting') is not None

    def test_a_BEATING_holder_keeps_its_slot(self, forge):
        """The heartbeat is the reason the lease above can be short enough to be useful."""
        arbiter = _arbiter(forge, slots=1, lease_seconds=0.2)
        hold = arbiter.take(owner='beating')
        for _ in range(4):
            time.sleep(0.05)
            hold.renew()
        assert arbiter.holders().claims[0].owner == 'beating'

    def test_the_beat_EDITS_IN_PLACE_and_keeps_the_ordering_key(self, forge):
        """A release-and-re-take heartbeat frees the thing it protects for the length of the gap,
        and moves the holder to the back of the ordering -- on a contended pool it would eventually
        beat itself out.
        """
        arbiter = _arbiter(forge, slots=1)
        hold = arbiter.take(owner='beating')
        before, first_expiry = hold.comment_id, hold.expires_at
        time.sleep(0.01)
        expiry = hold.renew()
        assert hold.comment_id == before
        assert expiry > first_expiry, 'the beat did not move the expiry forward'
        assert hold.expires_at == expiry, 'the hold still carries the OLD expiry'
        assert [c.id for c in forge.comments(arbiter.item_number)] == [before]

    def test_beating_a_LAPSED_hold_is_REFUSED_rather_than_resurrecting_it(self, forge):
        """The over-admission case. Once our expiry passes, somebody may have been admitted in our
        place; editing our comment back to the future would make N+1 holders using the mechanism
        that exists to keep the count right.
        """
        arbiter = _arbiter(forge, slots=1, lease_seconds=0.05)
        hold = arbiter.take(owner='napping')
        time.sleep(0.08)
        with pytest.raises(LeaseLost, match='lapse'):
            hold.renew()

    def test_beating_a_PRUNED_hold_is_REFUSED(self, forge):
        """A holder whose comment was deleted underneath it must learn, not beat into the void."""
        arbiter = _arbiter(forge, slots=1)
        hold = arbiter.take(owner='pruned')
        forge.delete_comment(arbiter.item_number, hold.comment_id)
        with pytest.raises(LeaseLost, match='pruned'):
            hold.renew()

    def test_the_beat_CADENCE_is_code_a_loop_consults_not_advice_beside_it(self, forge):
        """It used to be a module constant in the seat layer, documented as "the number a caller's
        loop should use" -- a declaration nothing consulted, and a SECOND spelling, since the job
        claim had no cadence at all. Now one predicate answers it for both.

        A quarter of the lease: three consecutive beats may be lost before the fleet calls the
        holder dead.
        """
        arbiter = _arbiter(forge, slots=1, lease_seconds=100.0)
        hold = arbiter.take(owner='beating')
        taken_at = hold.expires_at - 100.0
        assert hold.needs_beat(now=taken_at + 1.0) is False
        assert hold.needs_beat(now=taken_at + 74.0) is False
        assert hold.needs_beat(now=taken_at + 76.0) is True

    def test_the_cadence_is_NEVER_LONGER_THAN_THE_LEASE_IT_PROTECTS(self):
        """THE REGRESSION TEST FOR A REAL BUG, found 2026-08-11 by a test that used a short lease.

        `workbench_cli.Beater` floored its own interval at 1.0 s. For any lease under four seconds
        the first beat therefore landed AFTER the lease had already expired -- a heartbeat that
        cannot fire in time, which in a diff is indistinguishable from one that can, and which never
        bound at the production lease. The general property is the one the floor violated: the
        cadence must always leave room for a beat.
        """
        for lease in (0.1, 0.4, 1.0, 4.0, 300.0, 10800.0):
            assert beat_interval(lease) < lease, f'a lease of {lease}s cannot be beaten in time'
            assert beat_interval(lease) == pytest.approx(lease / 4)

    def test_a_NON_POSITIVE_lease_has_no_cadence_and_says_so(self):
        """A cadence of zero is a caller spinning against the forge as fast as it can -- the shape
        that gets a fleet rate-limited rather than the shape that gets noticed.
        """
        for lease in (0.0, -1.0):
            with pytest.raises(ValueError, match='cannot derive a beat cadence'):
                beat_interval(lease)

    def test_the_ARBITER_consults_the_one_derivation_and_does_not_repeat_it(self, forge):
        assert _arbiter(forge, lease_seconds=8.0).beat_every == beat_interval(8.0)

    def test_the_cadence_SHRINKS_with_the_lease(self, forge):
        """Derived, not set beside it. A cadence that stayed put while the lease shrank is how a
        short lease quietly stops being beaten in time.
        """
        short = _arbiter(forge, slots=1, lease_seconds=4.0)
        assert short.beat_every == pytest.approx(1.0)
        assert _arbiter(forge, slots=1, lease_seconds=400.0).beat_every == pytest.approx(100.0)

    def test_a_hold_from_ANOTHER_item_is_refused(self, forge):
        one, two = _arbiter(forge, slots=1), _arbiter(forge, slots=1)
        hold = one.take(owner='x')
        with pytest.raises(ValueError, match='belongs to item'):
            two.renew(hold)

    def test_the_loss_is_an_EXCEPTION_and_not_a_value_a_caller_can_forget(self):
        """A LOG IS NOT A SIGNAL, and neither is a False nobody assigned. `renew` has no non-raising
        failure return at all, which is what makes "the caller can tell" structural.
        """
        assert inspect.signature(Arbiter.renew).return_annotation == 'float'
        assert inspect.signature(Held.renew).return_annotation == 'float'


class TestReleaseIsOwnerCheckedAndIdChecked:
    def test_a_STRANGER_cannot_free_a_live_hold(self, forge):
        """Duplicate execution walking back in through the door marked cleanup."""
        arbiter = _arbiter(forge, slots=1)
        hold = arbiter.take(owner='holder')
        arbiter.release(owner='stranger', comment_id=hold.comment_id)
        assert arbiter.holders().claims[0].owner == 'holder'

    def test_a_STALE_hold_cannot_free_the_NEW_holders_slot(self, forge):
        """WHY THE OWNER CHECK IS NOT BELT-AND-BRACES. A retry loop keeping a `Held` across a
        lapse-and-retake calls release with an id that is no longer its own -- and by id alone that
        frees somebody else's live hold.
        """
        arbiter = _arbiter(forge, slots=1, lease_seconds=0.05)
        stale = arbiter.take(owner='first')
        time.sleep(0.08)
        second = arbiter.take(owner='second')
        stale.release()
        assert arbiter.holders().by('second') is not None
        assert second.is_live()

    def test_releasing_an_ALREADY_LOST_hold_is_a_no_op_and_not_an_error(self, forge):
        """`finally: hold.release()` must not replace the real error with this one."""
        arbiter = _arbiter(forge, slots=1)
        hold = arbiter.take(owner='holder')
        hold.release()
        hold.release()
        assert arbiter.holders().claims == ()


class TestHoldersRefusesToBeADecision:
    def test_it_has_NO_truth_value(self, forge):
        with pytest.raises(TypeError, match='UNDER-counts'):
            bool(_arbiter(forge).holders())

    def test_there_is_NO_free_slot_count(self):
        """A free-slot count derived from a list read OVER-counts, which is the over-admitting
        direction. The only sound way to learn a slot is free is to take one.
        """
        assert not hasattr(Holders, 'free')
        assert not hasattr(Arbiter, 'free')

    def test_occupancy_is_reported_as_a_lower_bound(self, forge):
        arbiter = _arbiter(forge, slots=3)
        arbiter.take(owner='a')
        holders = arbiter.holders()
        assert holders.occupied == 1
        assert holders.slots == 3
        assert 'LOWER BOUND' in Holders.occupied.__doc__

    def test_debris_BEYOND_the_slot_count_is_not_reported_as_a_holder(self, forge):
        """A crashed loser's comment is live and was never admitted. Counting it would over-report
        occupancy from wreckage -- and `by()` would name a holder that never held.
        """
        arbiter = _arbiter(forge, slots=1)
        arbiter.take(owner='real')
        forge.add_comment(arbiter.item_number, encode_claim(owner='debris', expires_at=time.time() + 300))
        assert [c.owner for c in arbiter.holders().claims] == ['real']
        assert arbiter.holders().by('debris') is None


class TestRefusedAtConstruction:
    @pytest.mark.parametrize('slots', [0, -1])
    def test_a_NON_POSITIVE_slot_count(self, forge, slots):
        """Refused at construction, not at the take: there, every attempt is refused and a healthy
        resource reads as fully booked.
        """
        with pytest.raises(ValueError, match='slots must be positive'):
            Arbiter(forge, item_number=1, slots=slots, lease_seconds=300.0)

    @pytest.mark.parametrize('lease', [0.0, -1.0])
    def test_a_NON_POSITIVE_lease(self, forge, lease):
        """A zero lease expires the claim being made, so every racer refuses and the work never
        runs -- which reads as healthy contention rather than as a bug.
        """
        with pytest.raises(ValueError, match='lease_seconds must be positive'):
            Arbiter(forge, item_number=1, slots=1, lease_seconds=lease)


class TestABrokenBackendIsLOUD:
    def test_a_comment_we_posted_and_cannot_read_RAISES(self, forge):
        """Returning "not now" would make a backend with no read-after-write consistency look like
        a busy resource: every attempt refused, every holder absent, and an operator chasing
        something that is perfectly healthy. The store used to answer exactly that quiet False.
        """

        class Amnesiac(RecordingForge):
            def comments(self, number):
                return []

        blind = Amnesiac()
        arbiter = _arbiter(blind, slots=1)
        with pytest.raises(ArbitrationUnsound, match='read-after-write'):
            arbiter.take(owner='someone')

    def test_it_WITHDRAWS_before_it_raises(self, forge):
        """The comment it could not see is still on the server. Left behind, it is a hold that
        activates later -- the raise must not skip the withdrawal.
        """
        seen: list[int] = []

        class Amnesiac(RecordingForge):
            def comments(self, number):
                return []

            def delete_comment(self, number, comment_id):
                seen.append(comment_id)
                super().delete_comment(number, comment_id)

        blind = Amnesiac()
        arbiter = _arbiter(blind, slots=1)
        with pytest.raises(ArbitrationUnsound):
            arbiter.take(owner='someone')
        assert len(seen) == 1


class TestThereIsExactlyONEArbitrationInThisPackage:
    """THE POINT OF THE REFACTOR, as a check rather than an assurance."""

    @pytest.mark.parametrize('module', ['forge_store', 'seats', 'pull'])
    def test_no_other_module_posts_a_claim_comment(self, module):
        """A duplicated scheme is the defect; a drifted copy is only its symptom. Nothing outside
        `claim.py` may encode a claim or post one -- so a second arbitration cannot be written
        without deleting this test, which is a visible act.

        SEARCH SCOPE: the code tokens of the three named modules, strings and comments excluded.
        `add_comment` is the primitive a claim is posted with; `record_verdict` still uses it, which
        is why `forge_store` is checked for `encode_claim` and the seat/pull layers for both.
        """
        source = Path(__import__(f'agent_swarm.{module}', fromlist=['x']).__file__).read_text(encoding='utf-8')
        code = {
            token.string
            for token in tokenize.generate_tokens(io.StringIO(source).readline)
            if token.type not in (tokenize.STRING, tokenize.COMMENT)
        }
        banned = {'encode_claim'} if module == 'forge_store' else {'encode_claim', 'add_comment', 'delete_comment'}
        offenders = sorted(code & banned)
        assert not offenders, f'{module} builds its own claim: {offenders}'

    def test_the_arbitration_module_names_no_vendor_in_its_CODE(self):
        """Tokenised, not grepped: the docstring cites the Gitea measurements on purpose."""
        source = Path(claim_module.__file__).read_text(encoding='utf-8')
        code = [
            token.string
            for token in tokenize.generate_tokens(io.StringIO(source).readline)
            if token.type not in (tokenize.STRING, tokenize.COMMENT)
        ]
        offenders = [t for t in code if 'gitea' in t.lower() or 'github' in t.lower()]
        assert not offenders, f'vendor names in the arbitration: {offenders}'


class TestTheDoubleIsNotGENTLERThanTheForge:
    """AUDIT THE INSTRUMENT. A double that accepts a call the vendor rejects hides the breakage."""

    @pytest.mark.parametrize(
        ('method', 'args', 'kwargs'),
        [
            ('add_comment', (1, 'CLAIM 1.0 x'), {}),
            ('comments', (1,), {}),
            ('delete_comment', (1, 2), {}),
            ('update_comment', (1, 2, 'CLAIM 1.0 x'), {}),
            ('create_work_item', (), {'title': 't', 'body': 'b', 'labels': ()}),
            ('list_work_items', (), {'state': 'open'}),
        ],
    )
    def test_every_call_the_arbitration_makes_BINDS_against_the_real_client(self, method, args, kwargs):
        """Every test in this file talks to `RecordingForge`. Binding the same calls against
        `GiteaForge`'s signatures is what stops the double from being a private dialect -- a vendor
        method with an extra required argument would pass every test here and fail live.
        """
        inspect.signature(getattr(GiteaForge, method)).bind(object(), *args, **kwargs)

    def test_the_real_client_raises_CommentGone_which_is_what_the_heartbeat_RESTS_ON(self):
        """`renew` turns `CommentGone` into `LeaseLost`. If the vendor wrapper merely returned, a
        pruned holder would beat into the void and every test here would still pass.
        """
        assert 'CommentGone' in inspect.getsource(GiteaForge.update_comment)
