"""`ForgeStore`: the SAME contract as `test_store.py`, plus what only a real server can settle.

THE CONTRACT SUITE IS REUSED, NOT RESTATED. The `...SatisfiesTheContract` classes subclass the ones
in `test_store.py` and override a single fixture. Every assertion there -- including the
sixteen-thread race -- runs unchanged, twice: once against an in-memory forge and once against the
real server. Copying them here would have let the two drift, and the copy would inevitably have been
the weaker one.

WHAT EACH HALF CAN AND CANNOT PROVE, because this is exactly where a suite lies to itself:

* `RecordingForge` models the two properties the protocol REQUIRES of a deployment -- ids assigned
  by the server, monotonically, and a read that sees every earlier write. Racing the store against
  it proves the STORE's arbitration is right. **It can never prove the DEPLOYMENT has those
  properties**, because it was built to have them.
* The `network` half runs the same arbitration against Gitea, where those properties are a measured
  fact rather than a construction. That is the only half that can say the protocol HOLDS.

Neither half is a mock of the other and nothing here monkeypatches the code under test. Deselect the
server half with `-m 'not network'`.

FOUR ROUNDS, NOT ONE, for the race against the real server. One round electing one winner is what a
BROKEN protocol also does most of the time -- the failure being tested for is rare by construction,
so the evidence has to repeat.
"""

from __future__ import annotations

import io
import threading
import time
import tokenize
import uuid
from pathlib import Path

import pytest

from agent_swarm import forge_store as forge_store_module
from agent_swarm.forge import Comment, Forge, GiteaForge, WorkItem, default_forge
from agent_swarm.forge_store import (
    VERDICT_LABELS,
    ForgeStore,
    decode_claim,
    encode_claim,
)
from agent_swarm.job import TEST_RUN, Job
from agent_swarm.store import VERDICTS, Store

# Imported under private names ON PURPOSE: a name starting with `Test` is COLLECTED wherever it is
# bound, so importing them plainly would re-run the in-memory suite here -- against a `store`
# fixture this module does not define, i.e. fourteen errors that say nothing about the forge.
from test_store import TestClaimIsCompareAndSwap as _ClaimContract
from test_store import TestItIsATOMICUnderRealConcurrency as _AtomicityContract
from test_store import TestVerdictsAreRecorded as _VerdictContract

JOB = Job(id='j1', kind=TEST_RUN)


def _race_one_round(namespace: str, job: Job, *, racers: int) -> list[str]:
    """Release `racers` threads at one job from a barrier; return everyone who believed they won.

    A FUNCTION AND NOT AN INLINE LOOP BODY: the closure captures the barrier, lock and winner list,
    and rebinding those per round inside a loop makes the thread bodies share whichever objects the
    LAST iteration created. It happens to be safe while every round joins before the next begins --
    which is exactly the kind of "safe for now" that a later edit turns into a race inside the test
    for races.
    """
    winners: list[str] = []
    lock = threading.Lock()
    barrier = threading.Barrier(racers)
    stores = [ForgeStore(namespace, default_forge()) for _ in range(racers)]

    def attempt(n: int) -> None:
        barrier.wait()
        if stores[n].try_claim(job, owner=f'r{n:02d}'):
            with lock:
                winners.append(f'r{n:02d}')

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(racers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return winners


class RecordingForge:
    """An in-memory `Forge` that MODELS THE PRECONDITION, and says so.

    It assigns comment ids from a single counter under a lock -- server-assigned, monotonic, unique
    -- and every read sees every completed write. Those are precisely the two properties the claim
    protocol requires of a deployment, and they are here by CONSTRUCTION.

    So: a green run against this forge is evidence that `ForgeStore`'s arbitration is correct. It is
    NOT evidence that any real forge has these properties, and no amount of it ever will be. That is
    what the `network` tests are for, and why they were measured before this was written.

    It is not a mock of the code under test -- the store's logic runs unmodified against it -- and
    it is a second genuine backend, which is what makes the vendor-neutrality claim checkable.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._next_id = 1
        self.items: dict[int, WorkItem] = {}
        self.bodies: dict[int, str] = {}
        self._comments: dict[int, list[Comment]] = {}
        self.item_labels: dict[int, list[str]] = {}
        self.retired: list[int] = []

    def list_work_items(self) -> list[WorkItem]:
        with self._lock:
            return list(self.items.values())

    def create_work_item(self, *, title: str, body: str) -> int:
        with self._lock:
            number = len(self.items) + 1
            self.items[number] = WorkItem(number=number, title=title, state='open')
            self.bodies[number] = body
            self._comments[number] = []
            self.item_labels[number] = []
            return number

    def add_comment(self, number: int, body: str) -> int:
        with self._lock:
            # THE COUNTER IS THE POINT. A per-issue index, or a timestamp, would not be a
            # server-assigned monotonic key and the store's arbitration would be untested.
            comment_id = self._next_id
            self._next_id += 1
            self._comments[number].append(Comment(id=comment_id, body=body))
            return comment_id

    def comments(self, number: int) -> list[Comment]:
        with self._lock:
            return list(self._comments[number])

    def delete_comment(self, number: int, comment_id: int) -> None:
        with self._lock:
            self._comments[number] = [c for c in self._comments[number] if c.id != comment_id]

    def labels(self, number: int) -> list[str]:
        with self._lock:
            return list(self.item_labels[number])

    def add_label(self, number: int, name: str) -> None:
        with self._lock:
            self.item_labels[number].append(name)

    def remove_label(self, number: int, name: str) -> None:
        with self._lock:
            self.item_labels[number] = [x for x in self.item_labels[number] if x != name]

    def close_work_item(self, number: int) -> None:
        with self._lock:
            self.items[number] = WorkItem(number=number, title=self.items[number].title, state='closed')

    def state(self, number: int) -> str:
        with self._lock:
            return self.items[number].state

    def retire_work_item(self, number: int) -> None:
        self.retired.append(number)
        self.close_work_item(number)


@pytest.fixture
def recording_forge():
    return RecordingForge()


@pytest.fixture
def memory_store(recording_forge):
    return ForgeStore('ns', recording_forge)


# --------------------------------------------------------------------------------------------
# The claim comment, pure.
# --------------------------------------------------------------------------------------------


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


class TestNoRefMechanismSURVIVED:
    """Refs are abandoned entirely (user directive 2026-08-09). A deleted mechanism that leaves a
    helper behind is a mechanism that comes back the first time someone needs one.
    """

    def test_the_store_module_runs_no_git_and_names_no_ref(self):
        source = Path(forge_store_module.__file__).read_text(encoding='utf-8')
        code = [
            token.string
            for token in tokenize.generate_tokens(io.StringIO(source).readline)
            if token.type not in (tokenize.STRING, tokenize.COMMENT)
        ]
        # EXACT identifiers, not substrings: `title_prefix` contains "ref" and would make this
        # assertion unpassable, which is how a check gets weakened until it means nothing.
        banned = {'ref', 'refs', 'claim_ref', 'git', 'push', '_push', '_git', 'subprocess', 'ls_remote', 'commit_tree'}
        offenders = sorted({t for t in code if t.lower() in banned})
        assert not offenders, f'ref machinery still in the store: {offenders}'

    def test_the_module_imports_no_SUBPROCESS(self):
        assert not hasattr(forge_store_module, 'subprocess')
        assert not hasattr(forge_store_module, 'claim_ref')


class TestNoVendorLEAKEDIntoTheLogic:
    """The user's constraint, as a check rather than an assurance.

    "把 Gitea/GitHub 降级为单纯的纯粹存储与 UI 界面" -- the forge is storage and a UI, and the
    scheduling logic is ours. A vendor conditional in the store would mean two behaviours where only
    one is ever tested, and cleanup is exactly where nobody would notice it was wrong.
    """

    def test_the_store_module_names_no_vendor_in_its_CODE(self):
        """Tokenised, not grepped. The module docstring cites the Gitea measurements on purpose --
        a plain grep would either flag that (and get suppressed) or be weakened to uselessness. So
        strings and comments are dropped and only executable tokens are examined.
        """
        source = Path(forge_store_module.__file__).read_text(encoding='utf-8')
        code = [
            token.string
            for token in tokenize.generate_tokens(io.StringIO(source).readline)
            if token.type not in (tokenize.STRING, tokenize.COMMENT)
        ]
        offenders = [t for t in code if 'gitea' in t.lower() or 'github' in t.lower()]
        assert not offenders, f'vendor names in the store logic: {offenders}'

    def test_the_store_imports_only_the_ABSTRACTION(self):
        assert not hasattr(forge_store_module, 'GiteaForge')
        assert not hasattr(forge_store_module, 'GitHubForge')

    def test_the_forge_is_REQUIRED_because_a_default_is_a_choice_of_vendor(self):
        with pytest.raises(TypeError):
            ForgeStore('ns')  # type: ignore[call-arg]

    def test_retirement_is_DELEGATED_never_spelled_out(self, memory_store, recording_forge):
        """Gitea cannot delete an issue here and closes-and-retitles instead; another forge may
        hard-delete. The store must ask, not decide, or cleanup grows a vendor conditional.
        """
        memory_store.record_verdict(JOB, verdict='PASS', detail='')
        memory_store.purge_namespace()
        assert recording_forge.retired == [1]

    def test_purging_cannot_reach_ANOTHER_namespace(self, recording_forge):
        ForgeStore('ns-a', recording_forge).record_verdict(JOB, verdict='PASS', detail='')
        ForgeStore('ns-b', recording_forge).purge_namespace()
        assert recording_forge.retired == []


class TestTheProtocolRefusesTheWayTheCONTRACTDemands:
    """Behaviour the contract suite does not reach, checked against the modelled forge."""

    def test_a_REFUSED_claimant_withdraws_its_comment(self, memory_store, recording_forge):
        """Not tidiness. See the next test for what an abandoned one does."""
        memory_store.try_claim(JOB, owner='runner-a')
        before = len(recording_forge.comments(1))
        assert memory_store.try_claim(JOB, owner='runner-b') is False
        assert len(recording_forge.comments(1)) == before, 'the loser left its claim comment behind'

    def test_a_refused_claim_does_not_ACTIVATE_when_the_holder_releases(self, memory_store):
        """THE FAILURE THAT MAKES WITHDRAWAL MANDATORY. A refused comment left in place becomes the
        lowest LIVE claim the moment the holder releases -- and the runner that was told False now
        reads itself as owning a job it never started. Nothing errors; it simply becomes true later.
        """
        memory_store.try_claim(JOB, owner='runner-a')
        assert memory_store.try_claim(JOB, owner='runner-b') is False
        memory_store.release(JOB, owner='runner-a')
        assert memory_store.claim_owner(JOB) is None

    def test_a_re_claim_by_the_HOLDER_leaves_the_original_claim_intact(self, memory_store):
        """The contract refuses it; this checks the refusal did not damage the live claim on its way
        out -- withdrawing the wrong comment would free a job that is being worked on.
        """
        memory_store.try_claim(JOB, owner='runner-a')
        assert memory_store.try_claim(JOB, owner='runner-a') is False
        assert memory_store.claim_owner(JOB) == 'runner-a'

    def test_an_EXPIRED_claim_is_taken_over_by_the_next_racer(self, recording_forge):
        """A machine that dies must not park a job forever. There is deliberately no separate
        takeover path: the dead claim stops counting and ordinary arbitration elects the next one.
        """
        dying = ForgeStore('ns', recording_forge, lease_seconds=0.4)
        assert dying.try_claim(JOB, owner='runner-dead') is True
        time.sleep(0.6)
        assert dying.claim_owner(JOB) is None, 'an expired claim must read as unheld'
        assert ForgeStore('ns', recording_forge).try_claim(JOB, owner='runner-b') is True

    def test_a_LIVE_claim_is_never_taken_over(self, memory_store):
        assert memory_store.try_claim(JOB, owner='runner-a') is True
        assert memory_store.try_claim(JOB, owner='runner-b') is False
        assert memory_store.claim_owner(JOB) == 'runner-a'

    def test_a_ZERO_lease_is_refused_at_CONSTRUCTION(self, recording_forge):
        """A zero lease expires the claim being made, so every runner refuses forever and the job
        silently never runs -- which reads as healthy contention rather than as a bug.
        """
        with pytest.raises(ValueError, match='lease_seconds'):
            ForgeStore('ns', recording_forge, lease_seconds=0.0)

    def test_the_verdict_detail_is_not_a_CLAIM_comment(self, memory_store):
        """Claims and verdicts share one comment stream. A job claimed after its verdict would
        otherwise hand back `CLAIM ...` as gate.py's output -- plausible, wrong, unflagged.
        """
        memory_store.record_verdict(JOB, verdict='FAIL', detail='3 failed, 10643 passed')
        memory_store.try_claim(JOB, owner='runner-a')
        assert '3 failed, 10643 passed' in memory_store.verdict_detail(JOB)

    def test_the_sole_verdict_label_rule_lives_in_the_STORE(self, memory_store, recording_forge):
        """A retry after INCONCLUSIVE must leave ONE label on every backend, so the rule cannot be a
        vendor's. Checked against the backend that has no such rule of its own.
        """
        memory_store.record_verdict(JOB, verdict='INCONCLUSIVE', detail='node down')
        memory_store.record_verdict(JOB, verdict='PASS', detail='green on retry')
        assert [x for x in recording_forge.labels(1) if x.startswith('verdict:')] == ['verdict:pass']

    def test_concurrent_claimants_do_not_each_create_their_OWN_work_item(self, recording_forge):
        """The store-logic half of the creation race. It cannot prove the server's issue numbers are
        monotonic -- that is the network test's job -- but it does prove the store converges on the
        lowest one and retires its own duplicate instead of leaving it in the list.
        """
        job = Job(id='fresh-item', kind=TEST_RUN)
        stores = [ForgeStore('ns', recording_forge) for _ in range(8)]
        winners: list[int] = []
        lock = threading.Lock()
        barrier = threading.Barrier(8)

        def attempt(n: int) -> None:
            barrier.wait()
            if stores[n].try_claim(job, owner=f'r{n}'):
                with lock:
                    winners.append(n)

        threads = [threading.Thread(target=attempt, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # CONVERGENCE is the vendor-neutral property, not "one item exists". Whether a retired
        # duplicate disappears from a title search is the FORGE's business -- this deployment
        # retitles, another may hard-delete -- and asserting it here would put a vendor behaviour
        # in a vendor-agnostic test. What must hold everywhere is that every racer resolved to the
        # same item, because that is what makes their comments contend at all.
        resolved = {store._item_number(job) for store in stores}
        assert len(resolved) == 1, f'racers used different work items: {resolved}'
        assert len(winners) == 1, f'{len(winners)} runners believed they won: {winners}'

    def test_two_jobs_do_not_share_a_work_item(self, memory_store):
        other = Job(id='j2', kind=TEST_RUN)
        assert memory_store.try_claim(JOB, owner='a') is True
        assert memory_store.try_claim(other, owner='b') is True


class TestTheVerdictVocabularyIsClosedBEFOREAnyIO:
    def test_every_verdict_has_exactly_one_label(self):
        assert set(VERDICT_LABELS) == VERDICTS
        assert len(set(VERDICT_LABELS.values())) == len(VERDICTS)

    def test_a_fourth_word_is_refused_without_touching_the_forge(self):
        """Validation precedes I/O, so an unreachable server is not what makes this fail. A store
        that validated after the comment was posted would leave one behind for a verdict it then
        rejected.
        """
        store = ForgeStore('probe-never-contacted', GiteaForge('http://127.0.0.1:1', 'o/r'))
        with pytest.raises(ValueError, match='verdict'):
            store.record_verdict(JOB, verdict='ERROR', detail='')


class TestItIsAStore:
    def test_the_protocol_is_satisfied(self, memory_store):
        assert isinstance(memory_store, Store)

    def test_it_holds_a_forge_rather_than_BEING_one(self, memory_store):
        """Storage is a collaborator, not a base class. Inheriting the vendor client would put the
        decisions and the I/O in one object, and the vendor half is the half we cannot test twice.
        """
        assert not isinstance(memory_store, Forge)


# --------------------------------------------------------------------------------------------
# The contract, against the modelled forge. Proves the STORE; cannot prove a deployment.
# --------------------------------------------------------------------------------------------


class TestTheStoreLogicSatisfiesTheContract(_ClaimContract):
    @pytest.fixture
    def store(self, memory_store):
        return memory_store


class TestTheStoreLogicArbitratesUnderThreads(_AtomicityContract):
    @pytest.fixture
    def store(self, memory_store):
        return memory_store


class TestTheStoreLogicRecordsVerdicts(_VerdictContract):
    @pytest.fixture
    def store(self, memory_store):
        return memory_store


# --------------------------------------------------------------------------------------------
# Everything below talks to the real server, where the ordering properties are MEASURED FACTS
# rather than constructions.
# --------------------------------------------------------------------------------------------


@pytest.fixture
def server_store():
    """A store in a namespace nobody else owns, purged afterwards.

    THE NAMESPACE IS PER-TEST because the server is shared and the contract suite hard-codes one
    job id. Two runs of this file at once would otherwise contend for one work item, and each would
    report the other's claim as a contract violation.
    """
    store = ForgeStore(f'probe-p3-{uuid.uuid4().hex[:10]}', default_forge())
    try:
        yield store
    finally:
        store.purge_namespace()


@pytest.mark.network
class TestTheRealServerSatisfiesTheContract(_ClaimContract):
    @pytest.fixture
    def store(self, server_store):
        return server_store


@pytest.mark.network
class TestTheRealServerElectsOneWinner(_AtomicityContract):
    @pytest.fixture
    def store(self, server_store):
        return server_store


@pytest.mark.network
class TestTheRealServerRecordsVerdicts(_VerdictContract):
    @pytest.fixture
    def store(self, server_store):
        return server_store


@pytest.mark.network
class TestWhatOnlyTheRealServerCanSettle:
    def test_FOUR_rounds_each_elect_exactly_one_winner(self, server_store):
        """THE DISCRIMINATING TEST, and one round is not enough of it.

        The failure mode -- two runners both reading themselves lowest -- is rare by construction,
        so a single round electing a single winner is what a BROKEN protocol also does most of the
        time. Four independent rounds on four fresh work items, sixteen threads released together
        from a barrier, is the evidence the protocol was accepted on.
        """
        for round_number in range(4):
            job = Job(id=f'round{round_number}', kind=TEST_RUN)
            winners = _race_one_round(server_store.namespace, job, racers=16)
            assert len(winners) == 1, f'round {round_number}: {len(winners)} believed they won: {winners}'
            assert server_store.claim_owner(job) == winners[0]

    def test_concurrent_claimants_converge_on_ONE_work_item(self, server_store):
        """THE BUG THE FOUR-ROUND RACE CAUGHT, pinned with the assertion that names it.

        The protocol arbitrates comments on "the job's work item" -- and when that item does not
        exist yet, creating it is itself an unarbitrated race. Sixteen runners read an empty list,
        create sixteen issues, and each then arbitrates perfectly on its OWN: 16/16 winners, with
        every individual claim correct. Counting winners alone would leave the cause ambiguous, so
        this counts the ITEMS.
        """
        job = Job(id='fresh-item', kind=TEST_RUN)
        stores = [ForgeStore(server_store.namespace, default_forge()) for _ in range(16)]
        winners: list[int] = []
        lock = threading.Lock()
        barrier = threading.Barrier(16)

        def attempt(n: int) -> None:
            barrier.wait()
            if stores[n].try_claim(job, owner=f'r{n:02d}'):
                with lock:
                    winners.append(n)

        threads = [threading.Thread(target=attempt, args=(i,)) for i in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        resolved = {store._item_number(job) for store in stores}
        assert len(resolved) == 1, f'racers arbitrated on different work items: {resolved}'
        assert len(winners) == 1, f'{len(winners)} runners believed they won: {winners}'

    def test_comment_ids_come_back_MONOTONIC_under_concurrency(self, server_store):
        """The property the whole protocol rests on, asserted rather than assumed. If ids were not
        server-assigned and increasing, a later racer could carry a lower key -- which is precisely
        the `ci_tick` defect this replaces, reintroduced by the storage layer.
        """
        forge = default_forge()
        number = forge.create_work_item(title=f'[swarm] {server_store.namespace}/idprobe', body='probe')
        posted: list[int] = []
        lock = threading.Lock()
        barrier = threading.Barrier(8)

        def post(n: int) -> None:
            barrier.wait()
            got = forge.add_comment(number, f'CLAIM 99999999999 r{n}')
            with lock:
                posted.append(got)

        threads = [threading.Thread(target=post, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        listed = [c.id for c in forge.comments(number)]
        assert len(set(posted)) == 8, f'ids were not unique: {sorted(posted)}'
        assert listed == sorted(listed), f'the list came back out of id order: {listed}'
        assert sorted(posted) == listed, 'a post was not visible in the list it belongs to'

    def test_a_LIVE_claim_is_not_stolen(self, server_store):
        assert server_store.try_claim(JOB, owner='runner-a') is True
        assert server_store.try_claim(JOB, owner='runner-b') is False
        assert server_store.claim_owner(JOB) == 'runner-a'

    def test_an_EXPIRED_claim_is_taken_over(self, server_store):
        dying = ForgeStore(server_store.namespace, default_forge(), lease_seconds=0.5)
        assert dying.try_claim(JOB, owner='runner-dead') is True
        time.sleep(0.8)
        assert dying.claim_owner(JOB) is None
        assert ForgeStore(server_store.namespace, default_forge()).try_claim(JOB, owner='runner-b') is True

    def test_the_verdict_survives_a_FRESH_store_object(self, server_store):
        """The point of a backing store. An in-process cache would pass every verdict test in the
        contract suite while storing nothing on the server.
        """
        server_store.record_verdict(JOB, verdict='INCONCLUSIVE', detail='node down')
        assert ForgeStore(server_store.namespace, default_forge()).verdict(JOB) == 'INCONCLUSIVE'

    def test_the_detail_is_recorded_where_a_HUMAN_will_read_it(self, server_store):
        """gate.py's output is the evidence behind the verdict; a verdict without it is a claim."""
        server_store.record_verdict(JOB, verdict='FAIL', detail='3 failed, 10643 passed')
        assert '3 failed, 10643 passed' in server_store.verdict_detail(JOB)

    def test_recording_a_verdict_CLOSES_the_item(self, server_store):
        """The board column is driven by state; an answered job left open is one the lead re-reads
        every tick.
        """
        server_store.record_verdict(JOB, verdict='PASS', detail='green')
        assert server_store.item_state(JOB) == 'closed'

    def test_a_second_verdict_REPLACES_the_first(self, server_store):
        """A retry after INCONCLUSIVE must not leave the job carrying two verdict labels, since
        nothing downstream can act on a job that is both inconclusive and green.
        """
        server_store.record_verdict(JOB, verdict='INCONCLUSIVE', detail='node down')
        server_store.record_verdict(JOB, verdict='PASS', detail='green on retry')
        assert server_store.verdict(JOB) == 'PASS'
