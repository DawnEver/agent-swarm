"""The pull surface: a human or a TUI agent taking work, on the runner's own primitives.

THE PROPERTY THE WHOLE FILE IS ABOUT: a human and a CI runner must not both take one item. That is
only true if `take` is the SAME compare-and-swap the runner uses -- so the discriminating tests here
race a `Workbench` against a bare `ForgeStore.try_claim`, which is what a CI runner calls, rather
than two Workbenches against each other. Two Workbenches racing would pass even if this module had
invented a private claim of its own, which is exactly the failure worth catching.

Sequential probes are not enough for the same reason `tests/test_store.py` gives, and one round is
not enough for the reason `forge_store`'s protocol note gives: a single round with a single winner
is also what a broken protocol does most of the time.
"""

from __future__ import annotations

import io
import threading
import time
import tokenize
from pathlib import Path

import pytest

from agent_swarm import pull as pull_module
from agent_swarm.claim import LeaseLost
from agent_swarm.forge_store import Claimable, ForgeStore, Role
from agent_swarm.job import TEST_RUN, Job
from agent_swarm.pull import MissingCapability, Ticket, Workbench
from agent_swarm.testing import RecordingForge

NAMESPACE = 'pull-test'


@pytest.fixture
def forge() -> RecordingForge:
    return RecordingForge()


@pytest.fixture
def submitter(forge) -> ForgeStore:
    return ForgeStore(NAMESPACE, forge, role=Role.SUBMITTER)


def _runner(forge, **kwargs) -> ForgeStore:
    return ForgeStore(NAMESPACE, forge, role=Role.RUNNER, **kwargs)


def _bench(forge, *, owner: str, capabilities=(), **kwargs) -> Workbench:
    return Workbench(_runner(forge, **kwargs), owner=owner, capabilities=capabilities)


JOB = Job(id='plain', kind=TEST_RUN)
NEEDY = Job(id='needy', kind=TEST_RUN)


class _CountingForge(RecordingForge):
    """Records what the caller ASKED FOR, which is what decides the cost.

    Counting the REQUESTS rather than timing them, for the reason
    `test_read_cost_does_not_grow_with_history.py` gives: a wall clock measures this box under
    whatever else it is running, while the call count is exact and reproducible.
    """

    def __init__(self) -> None:
        super().__init__()
        self.calls = dict.fromkeys(('list_work_items', 'comments', 'labels', 'work_item'), 0)

    def reset(self) -> None:
        self.calls = dict.fromkeys(self.calls, 0)

    def list_work_items(self, *, state: str = 'all'):
        self.calls['list_work_items'] += 1
        return super().list_work_items(state=state)

    def comments(self, number: int):
        self.calls['comments'] += 1
        return super().comments(number)

    def labels(self, number: int):
        self.calls['labels'] += 1
        return super().labels(number)

    def work_item(self, number: int):
        self.calls['work_item'] += 1
        return super().work_item(number)


class TestTheSurfaceIsPULLAndTheROLEIsStructural:
    def test_a_SUBMITTER_store_is_refused(self, forge):
        """A pull executor that could create work items is a second creator, which is the race the
        role split makes unreachable -- and a human at a keyboard is exactly who would reach for it.
        """
        with pytest.raises(ValueError, match='runner'):
            Workbench(ForgeStore(NAMESPACE, forge, role=Role.SUBMITTER), owner='human')

    def test_an_UNNAMED_owner_is_refused(self, forge):
        with pytest.raises(ValueError, match='owner'):
            _bench(forge, owner='')

    def test_this_module_implements_NO_claim_of_its_own(self):
        """THE LOAD-BEARING TEST OF THE WHOLE DESIGN. If `take` posted its own marker, a human and
        a runner would each hold "the" claim and neither would see the other.

        SEARCH SCOPE: the code tokens of `agent_swarm/pull.py` -- strings and comments excluded, no
        other module. It bans the forge-level comment primitives by name, so a private protocol
        cannot be built here without deleting this test, which is a visible act.
        """
        source = Path(pull_module.__file__).read_text(encoding='utf-8')
        code = [
            token.string
            for token in tokenize.generate_tokens(io.StringIO(source).readline)
            if token.type not in (tokenize.STRING, tokenize.COMMENT)
        ]
        banned = {'add_comment', 'delete_comment', 'update_comment', 'comments', 'forge', 'encode_claim'}
        offenders = sorted({t for t in code if t.lower() in banned})
        assert not offenders, f'pull.py reaches past the store: {offenders}'


class TestAvailableIsCapabilitiesTimesRequires:
    def test_work_this_box_cannot_do_is_NOT_offered(self, forge, submitter):
        submitter.register(JOB)
        submitter.register(NEEDY, requires=['a-licensed-tool'])
        bench = _bench(forge, owner='human', capabilities=())
        assert [j.id for j in bench.available(TEST_RUN).offered.jobs] == ['plain']

    def test_work_this_box_CAN_do_is_offered(self, forge, submitter):
        submitter.register(NEEDY, requires=['a-licensed-tool'])
        bench = _bench(forge, owner='human', capabilities=['a-licensed-tool', 'spare'])
        assert [j.id for j in bench.available(TEST_RUN).offered.jobs] == ['needy']

    def test_EVERY_requirement_must_be_met_not_merely_one(self, forge, submitter):
        """Subset, not intersection. An "any of" reading admits a box holding one of three tools."""
        submitter.register(NEEDY, requires=['one', 'two'])
        assert _bench(forge, owner='h', capabilities=['one']).available(TEST_RUN).offered.jobs == ()
        assert len(_bench(forge, owner='h', capabilities=['one', 'two']).available(TEST_RUN).offered.jobs) == 1

    def test_already_CLAIMED_work_is_not_offered(self, forge, submitter):
        submitter.register(JOB)
        _runner(forge).try_claim(JOB, owner='the-ci-runner')
        assert _bench(forge, owner='human').available(TEST_RUN).offered.jobs == ()

    def test_the_requirements_RIDE_ALONG_so_nothing_re_reads_them(self, forge, submitter):
        """The offered set carries what each job needs, from the SAME listing labels. A pull surface
        that asked per job would be the N+1 measured at 101 calls for 100 items and deleted.
        """
        submitter.register(NEEDY, requires=['one', 'two'])
        offered = _bench(forge, owner='h', capabilities=['one', 'two']).available(TEST_RUN).offered
        assert offered.requirements_for(NEEDY) == frozenset({'one', 'two'})

    def test_the_result_STILL_refuses_to_be_truthy(self, forge, submitter):
        """The distinction must survive the filter. "No work visible" is not "no work exists", and
        a human reading the second one acts on it.
        """
        survey = _bench(forge, owner='human').available(TEST_RUN)
        assert isinstance(survey.offered, Claimable)
        with pytest.raises(TypeError, match='NO WORK VISIBLE'):
            bool(survey.offered)
        # AND THE SURVEY ITSELF, which is the object a caller now holds first. Banning the inner one
        # while leaving the wrapper truthy would move the laundering up a level, not remove it.
        with pytest.raises(TypeError, match='NO WORK VISIBLE'):
            bool(survey)


class TestTheCostOfOfferingWork:
    """The N+1, pinned as a SHAPE so it cannot regress while the design decision is open.

    MEASURED 2026-08-11 against `RecordingForge`, ladder 10 / 100 / 500 / 1000 open items, every one
    claimable and none claimed:

        open items      10      100      500     1000
        forge calls     11      101      501     1001
        in-memory ms   0.1      0.4      1.8      3.8

    Exactly one list plus one comment read per candidate, linear, no hidden constant. What that
    costs against a real server -- and why it REFUTES this method's own justification at 500 items
    -- is written at `Workbench.available`, where the caller reads it.
    """

    @pytest.mark.parametrize('items', [10, 100])
    def test_offering_work_costs_ONE_list_plus_one_read_per_candidate(self, items):
        """The ladder's small rungs, as a test. 500 and 1000 were measured once (above) and are not
        re-run every suite: they assert nothing 100 does not, and they would spend seconds of a
        tier whose speed is the reason anybody runs it.
        """
        forge = _CountingForge()
        submitter = ForgeStore(NAMESPACE, forge, role=Role.SUBMITTER)
        for i in range(items):
            submitter.register(Job(id=f'g/j{i}', kind=TEST_RUN))
        bench = _bench(forge, owner='human')
        forge.reset()

        survey = bench.available(TEST_RUN)

        assert len(survey.offered.jobs) == items
        assert forge.calls['list_work_items'] == 1, 'the listing is read more than once'
        assert forge.calls['comments'] == items, 'the claimed filter is not one read per candidate'
        assert sum(forge.calls.values()) == items + 1

    def test_the_CAPABILITY_filter_costs_NOTHING_extra(self):
        """It rides on labels the listing already returned. If it ever needed a per-item fetch it
        would DOUBLE the N+1, and the cheap half of this method would silently become the expensive
        half -- invisible, because the result would be identical.
        """
        forge = _CountingForge()
        submitter = ForgeStore(NAMESPACE, forge, role=Role.SUBMITTER)
        for i in range(20):
            submitter.register(Job(id=f'g/j{i}', kind=TEST_RUN), requires=['a-licensed-tool'])
        bench = _bench(forge, owner='human', capabilities=[])
        forge.reset()

        survey = bench.available(TEST_RUN)

        assert survey.offered.jobs == ()
        assert forge.calls['labels'] == 0, 'requirements were re-fetched per item'
        # Nothing is read for work this box cannot do: the capability filter runs FIRST, so the
        # expensive half is never reached. Ordering the two the other way round would cost the full
        # N+1 to offer nothing, and no test would have noticed.
        assert forge.calls['comments'] == 0
        assert sum(forge.calls.values()) == 1


class TestTakeIsTheRUNNERSOwnCompareAndSwap:
    @pytest.mark.parametrize('round_number', range(4))
    def test_a_human_and_the_CI_runner_cannot_both_take_one_item(self, round_number):
        """THE DISCRIMINATING RACE, and it is deliberately asymmetric: eight Workbenches against
        eight bare `try_claim` calls -- the call a CI runner makes. If `take` had its own claim,
        both sides would win and this is the only shape that says so.

        Four rounds on a fresh forge each, because one round with one winner is what a broken
        protocol also does most of the time.
        """
        forge = RecordingForge()
        ForgeStore(NAMESPACE, forge, role=Role.SUBMITTER).register(JOB)
        winners: list[str] = []
        lock = threading.Lock()
        barrier = threading.Barrier(16)

        def as_human(n: int) -> None:
            bench = _bench(forge, owner=f'human-{n}')
            barrier.wait()
            if bench.take(JOB) is not None:
                with lock:
                    winners.append(f'human-{n}')

        def as_ci(n: int) -> None:
            store = _runner(forge)
            barrier.wait()
            if store.try_claim(JOB, owner=f'ci-{n}'):
                with lock:
                    winners.append(f'ci-{n}')

        threads = [threading.Thread(target=as_human if i % 2 else as_ci, args=(i,)) for i in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(winners) == 1, f'{len(winners)} executors each believed they held it: {winners}'
        assert _runner(forge).claim_owner(JOB) == winners[0]

    def test_a_LOSER_gets_None_and_not_an_exception(self, forge, submitter):
        """Losing a race is ordinary and retryable; the caller walks to the next item."""
        submitter.register(JOB)
        _runner(forge).try_claim(JOB, owner='someone-else')
        assert _bench(forge, owner='human').take(JOB) is None

    def test_taking_work_this_box_CANNOT_DO_raises_instead_of_returning_None(self, forge, submitter):
        """`None` means "try again"; a missing capability means "never". Collapsing them makes a
        misconfigured box look like a busy queue, and the caller retries forever.
        """
        submitter.register(NEEDY, requires=['a-licensed-tool'])
        with pytest.raises(MissingCapability, match='a-licensed-tool'):
            _bench(forge, owner='human').take(NEEDY)

    def test_the_capability_is_re_read_AT_TAKE_not_trusted_from_the_listing(self, forge, submitter):
        """The listing is the read measured STALE; the by-number read is the one measured fresh. A
        requirement added after the offer must still stop the take -- and a caller that never called
        `available` must be checked at all, which on a human surface is not hypothetical.
        """
        number = submitter.register(NEEDY)
        bench = _bench(forge, owner='human')
        offered = bench.available(TEST_RUN).offered
        assert offered.jobs  # offered while it required nothing
        forge.add_label(number, 'requires:a-licensed-tool')
        with pytest.raises(MissingCapability):
            bench.take(NEEDY)

    def test_a_refused_take_leaves_the_work_CLAIMABLE(self, forge, submitter):
        submitter.register(NEEDY, requires=['a-licensed-tool'])
        with pytest.raises(MissingCapability):
            _bench(forge, owner='human').take(NEEDY)
        assert _runner(forge).claim_owner(NEEDY) is None


class TestTheHeartbeatIsWhatStopsAClosedTerminalParkingTheWork:
    def test_a_taken_item_is_RECLAIMED_after_its_holder_stops_beating(self, forge, submitter):
        """The whole reason a heartbeat is REQUIRED here: a person takes an item, closes the
        terminal, and without this the item is unavailable for the full lease with nothing anywhere
        saying why.
        """
        submitter.register(JOB)
        assert _bench(forge, owner='human', lease_seconds=0.05).take(JOB) is not None
        time.sleep(0.08)
        assert _runner(forge).try_claim(JOB, owner='the-ci-runner') is True

    def test_a_BEATING_holder_keeps_the_work(self, forge, submitter):
        submitter.register(JOB)
        ticket = _bench(forge, owner='human', lease_seconds=0.2).take(JOB)
        for _ in range(4):
            time.sleep(0.05)
            ticket.beat()
        assert _runner(forge).claim_owner(JOB) == 'human'

    def test_the_beat_KEEPS_the_comment_id_so_the_ordering_is_unchanged(self, forge, submitter):
        """A release-and-re-claim beat would free the job it protects, for as long as the gap lasts."""
        number = submitter.register(JOB)
        ticket = _bench(forge, owner='human').take(JOB)
        before = [c.id for c in forge.comments(number)]
        ticket.beat()
        assert [c.id for c in forge.comments(number)] == before

    def test_beating_a_LOST_claim_raises_and_does_not_take_it_back(self, forge, submitter):
        submitter.register(JOB)
        ticket = _bench(forge, owner='human').take(JOB)
        _runner(forge).release(JOB, owner='human')
        assert _runner(forge).try_claim(JOB, owner='the-ci-runner') is True
        with pytest.raises(LeaseLost, match='no longer holds'):
            ticket.beat()
        assert _runner(forge).claim_owner(JOB) == 'the-ci-runner'

    def test_abandoning_gives_the_work_straight_back(self, forge, submitter):
        submitter.register(JOB)
        ticket = _bench(forge, owner='human').take(JOB)
        ticket.abandon()
        assert _runner(forge).try_claim(JOB, owner='the-ci-runner') is True


class TestReportIsTheSameVerdictNamespace:
    @pytest.mark.parametrize('word', ['PASS', 'FAIL', 'INCONCLUSIVE'])
    def test_gate_pys_three_words_round_trip(self, forge, submitter, word):
        submitter.register(JOB)
        ticket = _bench(forge, owner='human').take(JOB)
        ticket.report(verdict=word, detail='ran it by hand')
        assert _runner(forge).verdict(JOB) == word
        assert 'ran it by hand' in _runner(forge).verdict_detail(JOB)

    def test_a_FOURTH_state_is_refused(self, forge, submitter):
        submitter.register(JOB)
        ticket = _bench(forge, owner='human').take(JOB)
        with pytest.raises(ValueError, match='verdict'):
            ticket.report(verdict='DONE', detail='')
        assert _runner(forge).verdict(JOB) is None

    def test_reporting_on_work_this_owner_LOST_raises_and_writes_NOTHING(self, forge, submitter):
        """By then another executor may have run it and answered it. Writing over that would
        replace a live answer with a stale one, and neither would be marked.
        """
        submitter.register(JOB)
        ticket = _bench(forge, owner='human').take(JOB)
        _runner(forge).release(JOB, owner='human')
        _runner(forge).try_claim(JOB, owner='the-ci-runner')
        with pytest.raises(LeaseLost, match='held by'):
            ticket.report(verdict='PASS', detail='')
        assert _runner(forge).verdict(JOB) is None

    def test_the_claim_is_given_back_after_the_answer(self, forge, submitter):
        submitter.register(JOB)
        ticket = _bench(forge, owner='human').take(JOB)
        ticket.report(verdict='PASS', detail='')
        assert _runner(forge).claim_owner(JOB) is None

    def test_answered_work_is_no_longer_OFFERED(self, forge, submitter):
        submitter.register(JOB)
        bench = _bench(forge, owner='human')
        bench.take(JOB).report(verdict='PASS', detail='')
        assert bench.available(TEST_RUN).offered.jobs == ()

    def test_a_ticket_cannot_be_forged_into_a_verdict_for_someone_else(self, forge, submitter):
        """A hand-built ticket is the shape a caller reaches for when a lease has lapsed."""
        submitter.register(JOB)
        _runner(forge).try_claim(JOB, owner='the-ci-runner')
        bench = _bench(forge, owner='human')
        with pytest.raises(LeaseLost):
            Ticket(workbench=bench, job=JOB, owner='human').report(verdict='PASS', detail='')


class TestDifferentOwnersLookAtDifferentWork:
    """THE DEFECT THE BOUND INTRODUCED, and the mechanism that was sitting unused beside it.

    `Claimable.preferred` was measured live on 2026-08-10: rotating each owner's start position by a
    hash of its name took completed jobs per round from 1 to 7 at sixteen runners. **Nothing in this
    package called it.** Unbounded that was waste; once `available` grew a `limit` it became
    starvation -- every owner examining the same first K, racing the same head, K-1 of them doing
    nothing while work sat visible past the edge of the screen.
    """

    def test_two_owners_with_a_BOUND_do_not_examine_the_same_head(self, forge, submitter):
        """THE DISCRIMINATING ASSERTION. Without the rotation both owners see job 0 and nothing else;
        the sets are then identical and this fails.
        """
        for i in range(12):
            submitter.register(Job(id=f'g/j{i}', kind=TEST_RUN))
        first = _bench(forge, owner='alice').available(TEST_RUN, limit=2).offered.jobs
        second = _bench(forge, owner='bob-the-second').available(TEST_RUN, limit=2).offered.jobs
        assert {j.id for j in first} != {j.id for j in second}, (
            'both owners examined the same head; the bound is starvation without the rotation'
        )

    def test_the_rotation_is_a_PERMUTATION_and_never_hides_work(self, forge, submitter):
        """A partition starves: with three items and ten runners, seven get an empty shard. Every
        job must still be reachable by every owner, just later.
        """
        for i in range(6):
            submitter.register(Job(id=f'g/j{i}', kind=TEST_RUN))
        for owner in ('alice', 'bob', 'carol', 'dave'):
            seen = _bench(forge, owner=owner).available(TEST_RUN).offered.jobs
            assert {j.id for j in seen} == {f'g/j{i}' for i in range(6)}

    def test_it_is_DETERMINISTIC_so_a_retry_races_the_item_it_just_lost(self, forge, submitter):
        """By sha256 rather than `hash()`, which is salted per process: two runs of one owner would
        otherwise prefer different items and a retry would race a fresh item instead of its own.
        """
        for i in range(8):
            submitter.register(Job(id=f'g/j{i}', kind=TEST_RUN))
        once = _bench(forge, owner='alice').available(TEST_RUN, limit=3).offered.jobs
        twice = _bench(forge, owner='alice').available(TEST_RUN, limit=3).offered.jobs
        assert [j.id for j in once] == [j.id for j in twice]
