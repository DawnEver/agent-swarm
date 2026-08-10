"""The work-item index, and the two rulings it implements.

WHY IT EXISTS. `verdict()` answered from a LIST query, and a list is the read measured stale on
GitHub (22/22, up to 6.36 s). A stale miss reads as "not answered yet" and the loop re-runs a gate
that already passed -- 25 minutes, every time it fires, on a fleet meant to run unattended. The only
read fresh on BOTH forges is `GET /issues/{number}`, and you can only issue it if you know the
number. This remembers it.

THE DISCRIMINATING TEST IS `test_a_verdict_is_FOUND_through_the_index_where_the_list_finds_nothing`.
Everything else guards a property that would otherwise be a docstring nobody checks.
"""

from __future__ import annotations

import json
import threading

import pytest

from agent_swarm.forge_store import ForgeStore, NotVisible, Role
from agent_swarm.item_index import (
    NOT_INDEXED,
    IndexCorruptError,
    IndexedLookup,
    ItemIndex,
    NotIndexed,
)
from agent_swarm.job import TEST_RUN, Job
from test_forge_store import RecordingForge, StaleListForge

JOB = Job(id='j1', kind=TEST_RUN)


@pytest.fixture
def index(tmp_path) -> ItemIndex:
    return ItemIndex(tmp_path / 'index' / 'items.json')


class TestAMissIsUNKNOWNNeverAbsent:
    def test_a_miss_returns_NOT_INDEXED(self, index):
        """The same re-typing as NOT_VISIBLE, for the same reason: `if number is None: create()` is
        the line that produced eight duplicate work items, and making `None` un-returnable is the
        cheapest way to stop it being written again.
        """
        answer = index.get('test-run/nothing')
        assert answer is not None
        assert isinstance(answer, NotIndexed)

    def test_a_hit_carries_a_NUMBER_and_nothing_else(self, index):
        """It must not become a second source of truth about verdicts. If a stale entry could carry
        an ANSWER, its failure mode would invert -- from costing a re-run to returning a wrong
        result, which is far worse than the tax being removed.
        """
        index.put('test-run/j1', 42)
        hit = index.get('test-run/j1')
        assert isinstance(hit, IndexedLookup)
        assert hit.number == 42
        assert IndexedLookup.__slots__ == ('number',)

    def test_NOT_INDEXED_is_falsy_but_is_not_None(self):
        assert not NOT_INDEXED
        assert NOT_INDEXED is not None


class TestItSurvivesTheProcessAndTheCrash:
    def test_an_entry_outlives_the_object(self, tmp_path):
        ItemIndex(tmp_path / 'i.json').put('test-run/j1', 7)
        assert ItemIndex(tmp_path / 'i.json').get('test-run/j1').number == 7

    def test_forgetting_is_durable_too(self, tmp_path):
        first = ItemIndex(tmp_path / 'i.json')
        first.put('test-run/j1', 7)
        first.forget('test-run/j1')
        assert isinstance(ItemIndex(tmp_path / 'i.json').get('test-run/j1'), NotIndexed)

    def test_a_DELETED_index_is_a_cold_cache_not_an_error(self, tmp_path):
        """The degradation requirement. A missing index costs re-reads; it must never cost
        correctness, and it must not stop the process starting.
        """
        assert len(ItemIndex(tmp_path / 'never-written.json')) == 0

    def test_the_directory_is_created_on_DEMAND(self, tmp_path):
        ItemIndex(tmp_path / 'deep' / 'nested' / 'i.json').put('k', 1)
        assert (tmp_path / 'deep' / 'nested' / 'i.json').exists()

    def test_a_CORRUPT_index_is_loud_where_a_deleted_one_is_not(self, tmp_path):
        """The distinction matters. Deleted is ordinary; corrupt means something damaged it, and
        treating that as empty would hide whatever did. Same stance as the spool's corrupt entry.
        """
        path = tmp_path / 'i.json'
        path.write_text('{"test-run/j1": ', encoding='utf-8')
        with pytest.raises(IndexCorruptError, match='i.json'):
            ItemIndex(path)

    def test_no_partial_file_is_ever_visible(self, tmp_path):
        """Atomic replace, borrowed from the spool rather than reimplemented -- one scheme, one
        spelling.
        """
        index = ItemIndex(tmp_path / 'i.json')
        for n in range(50):
            index.put(f'test-run/j{n}', n)
        assert len(json.loads((tmp_path / 'i.json').read_text(encoding='utf-8'))) == 50


class TestTheIndexIsAHYPOTHESISUntilConfirmed:
    def test_a_verdict_is_FOUND_through_the_index_where_the_list_finds_nothing(self, tmp_path):
        """THE DISCRIMINATING TEST, and the whole reason this module exists.

        The list cannot see the item; the by-number read can. Without the index the second store
        reads "no verdict" and the loop re-runs a 25-minute gate. With it, the answer is found in one
        fresh read.
        """
        forge = StaleListForge(RecordingForge(), staleness=30.0)
        index = ItemIndex(tmp_path / 'i.json')
        submitter = ForgeStore('ns', forge, role=Role.SUBMITTER, index=index)
        submitter.record_verdict(JOB, verdict='PASS', detail='10646 passed')

        blind = ForgeStore('ns', forge, role=Role.RUNNER)
        assert blind.verdict(JOB) is None, 'precondition: the list-based path cannot see it'

        indexed = ForgeStore('ns', forge, role=Role.RUNNER, index=ItemIndex(tmp_path / 'i.json'))
        assert indexed.verdict(JOB) == 'PASS'

    def test_a_stale_entry_pointing_at_NOTHING_self_corrects_quietly(self, tmp_path):
        """An ordinary miss: the number is simply gone. Forget it and fall through to the list, so a
        wrong entry costs one extra read rather than every future lookup.
        """
        forge = RecordingForge()
        index = ItemIndex(tmp_path / 'i.json')
        index.put(JOB.claim_key(), 999)
        store = ForgeStore('ns', forge, role=Role.SUBMITTER, index=index)
        store.record_verdict(JOB, verdict='PASS', detail='green')
        assert store.verdict(JOB) == 'PASS'
        assert index.get(JOB.claim_key()).number != 999

    def test_an_entry_pointing_at_the_WRONG_item_is_CORRUPTION_not_a_miss(self, tmp_path):
        """Two keys crossed. A store that treated this as a miss would carry on with a mapping that
        is actively wrong -- writing this job's verdict onto somebody else's work item -- so it
        raises. Loud, and named in the message.
        """
        forge = RecordingForge()
        other = Job(id='someone-else', kind=TEST_RUN)
        ForgeStore('ns', forge, role=Role.SUBMITTER).register(other)

        index = ItemIndex(tmp_path / 'i.json')
        index.put(JOB.claim_key(), 1)  # #1 belongs to `other`
        store = ForgeStore('ns', forge, role=Role.SUBMITTER, index=index)
        with pytest.raises(IndexCorruptError, match='corrupt'):
            store.verdict(JOB)

    def test_and_the_corrupt_entry_is_DROPPED_so_the_retry_is_clean(self, tmp_path):
        """Self-correcting AND loud, not one or the other. Raising without forgetting would fail
        forever; forgetting without raising would hide whatever crossed the keys.
        """
        forge = RecordingForge()
        ForgeStore('ns', forge, role=Role.SUBMITTER).register(Job(id='someone-else', kind=TEST_RUN))
        index = ItemIndex(tmp_path / 'i.json')
        index.put(JOB.claim_key(), 1)
        store = ForgeStore('ns', forge, role=Role.SUBMITTER, index=index)
        with pytest.raises(IndexCorruptError):
            store.verdict(JOB)
        assert isinstance(index.get(JOB.claim_key()), NotIndexed)
        assert store.verdict(JOB) is None  # clean lookup, no verdict recorded for THIS job

    def test_a_store_with_NO_index_behaves_exactly_as_before(self, tmp_path):
        """Degradation, asserted rather than assumed. The index is an accelerator; removing it must
        change cost, never behaviour.
        """
        forge = RecordingForge()
        store = ForgeStore('ns', forge, role=Role.SUBMITTER)
        store.record_verdict(JOB, verdict='FAIL', detail='3 failed')
        assert ForgeStore('ns', forge, role=Role.SUBMITTER).verdict(JOB) == 'FAIL'


class TestARunnerMayNotCREATE:
    """Ruling 1, and the assertion is the ELIMINATION rather than the error message."""

    def test_a_runner_mode_store_cannot_reproduce_the_eight_item_race(self):
        """THE DISCRIMINATING TEST. With the list lagging thirty seconds -- the condition that made
        eight racers produce eight work items -- a runner-mode fleet produces NONE, because it
        cannot create at all. The race is not narrowed, it is unreachable from this role.
        """
        forge = StaleListForge(RecordingForge(), staleness=30.0)
        job = Job(id='never-submitted', kind=TEST_RUN)
        stores = [ForgeStore('ns', forge, role=Role.RUNNER) for _ in range(8)]
        refusals: list[str] = []
        lock = threading.Lock()
        barrier = threading.Barrier(8)

        def attempt(n: int) -> None:
            barrier.wait()
            try:
                stores[n].try_claim(job, owner=f'r{n}')
            except PermissionError as exc:
                with lock:
                    refusals.append(str(exc))

        threads = [threading.Thread(target=attempt, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        title = f'[swarm] ns/{job.claim_key()}'
        assert [i for i in forge.inner.list_work_items() if i.title == title] == []
        assert len(refusals) == 8

    def test_the_refusal_names_the_SUBMITTER_path(self):
        """A refusal that does not say what to do instead gets worked around."""
        store = ForgeStore('ns', RecordingForge(), role=Role.RUNNER)
        with pytest.raises(PermissionError, match='register'):
            store.try_claim(JOB, owner='r0')

    def test_a_runner_may_not_register_either(self):
        store = ForgeStore('ns', RecordingForge(), role=Role.RUNNER)
        with pytest.raises(PermissionError):
            store.register(JOB)

    def test_a_runner_CAN_claim_an_item_that_exists(self):
        """The role restricts creation, not work. A runner that could not claim would be useless."""
        forge = RecordingForge()
        ForgeStore('ns', forge, role=Role.SUBMITTER).register(JOB)
        assert ForgeStore('ns', forge, role=Role.RUNNER).try_claim(JOB, owner='r0') is True

    def test_the_role_is_REQUIRED_not_defaulted(self):
        """DEFAULT-DENY. A defaulted role makes the policy quietly opt-out, and a caller who never
        thought about creation would get the permissive half for free -- the exact shape that
        produced the bug this enum exists to prevent.
        """
        with pytest.raises(TypeError):
            ForgeStore('ns', RecordingForge())  # type: ignore[call-arg]

    def test_a_runner_cannot_be_given_creation_by_accident(self, tmp_path):
        """Not even through the index path: an unconfirmable entry falls through to the list, and
        the list falling through to creation is what the role refuses.
        """
        forge = StaleListForge(RecordingForge(), staleness=30.0)
        index = ItemIndex(tmp_path / 'i.json')
        index.put(JOB.claim_key(), 4242)  # points at nothing
        store = ForgeStore('ns', forge, role=Role.RUNNER, index=index)
        assert isinstance(store.work_item_number(JOB), NotVisible)
        with pytest.raises(PermissionError):
            store.record_verdict(JOB, verdict='PASS', detail='')


class TestTheIndexIsActuallyPOPULATED:
    """An index nothing writes to is a cache that can only ever miss.

    FOUND BY MEASURING, NOT BY READING. `register` recorded the number in its in-process dict and
    not in the index -- and the submitter is the ONLY creator, so the index could only be warmed by
    a lookup that had already paid for the list read it exists to avoid. Every test passed, because
    every test either used one store object (in-process cache) or asserted correctness rather than
    cost. Measured against the real forge: a "warm" index cost 5060 ms versus 4627 ms with no index
    at all. After the fix: 235 ms versus 5347 ms.
    """

    def test_REGISTER_writes_the_number_to_the_index(self, tmp_path):
        forge = RecordingForge()
        index = ItemIndex(tmp_path / 'i.json')
        number = ForgeStore('ns', forge, role=Role.SUBMITTER, index=index).register(JOB)
        assert index.get(JOB.claim_key()).number == number

    def test_a_FRESH_process_finds_it_without_touching_the_list(self, tmp_path):
        """The property that makes the index worth having, asserted as a COST rather than a
        behaviour: a new store must resolve the item without a single list call.
        """
        forge = StaleListForge(RecordingForge(), staleness=0.0)
        ForgeStore('ns', forge, role=Role.SUBMITTER, index=ItemIndex(tmp_path / 'i.json')).register(JOB)

        fresh = ForgeStore('ns', forge, role=Role.RUNNER, index=ItemIndex(tmp_path / 'i.json'))
        before = forge.list_calls
        assert fresh.work_item_number(JOB) is not None
        assert forge.list_calls == before, 'the index was consulted and the list was read anyway'
