"""The verdict spool: a 25-minute gate must not be able to produce nothing.

WHY THIS EXISTS. The ref design got durability for free -- `_write_ref` KEEPS the local ref when the
push fails, so a later tick republishes rather than re-runs, because git's object store is a local
mirror. **An Issue has no local mirror.** Under the zero-ref schema a gate that runs for 25 minutes
and then fails its `POST` has produced nothing at all, and nothing anywhere says so. This is the
regression the migration is blocked on.

WHAT IS TESTED HERE IS CRASH ORDERING, NOT FEATURES. Every requirement below is a property about
what survives a `kill -9` at a specific instant, so the tests put the pieces in the order a crash
would leave them -- record without draining, publish without marking -- rather than monkeypatching
the code under test. A test that stubbed out the publisher would prove the stub is idempotent.

THE ONE THAT ACTUALLY DUPLICATES is crash-after-POST-before-mark: the comment exists on the forge,
the spool still says pending, and a naive replay posts it twice. It is tested against the real
`ForgePublisher` driving the real `ForgeStore` over an in-memory forge, so the marker scan under
test is the shipping one.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest

from agent_swarm import durable as durable_module
from agent_swarm.durable import DIRECTORY_FSYNC_AVAILABLE
from agent_swarm.forge import Comment
from agent_swarm.forge_store import VERDICT_LABELS, ForgeStore, Role
from agent_swarm.job import TEST_RUN, Job
from agent_swarm.spool import (
    DrainReport,
    ForgePublisher,
    PublishedTextReader,
    Publisher,
    Spool,
    SpoolBacklogError,
    SpoolCorruptError,
    SpoolEntry,
    VerdictTamperedError,
)
from agent_swarm.store import VERDICTS
from test_forge_store import RecordingForge

JOB = Job(id='j1', kind=TEST_RUN)


@pytest.fixture
def spool(tmp_path) -> Spool:
    return Spool(tmp_path / 'spool')


@pytest.fixture
def forge() -> RecordingForge:
    return RecordingForge()


@pytest.fixture
def publisher(forge) -> ForgePublisher:
    return ForgePublisher(ForgeStore('ns', forge, role=Role.SUBMITTER))


class CountingPublisher:
    """Counts publish attempts. Used ONLY where the assertion is about the spool's call pattern."""

    def __init__(self, *, fail: bool = False) -> None:
        self.published: list[str] = []
        self.fail = fail

    def publish(self, entry: SpoolEntry) -> None:
        if self.fail:
            msg = 'the forge is down'
            raise ConnectionError(msg)
        self.published.append(entry.id)

    def is_published(self, entry: SpoolEntry) -> bool:
        return entry.id in self.published


class TestRecordHappensBEFOREPublish:
    """The window in which a verdict exists only in memory must be ZERO."""

    def test_recording_puts_the_verdict_on_disk_immediately(self, spool):
        entry = spool.record(JOB, verdict='PASS', detail='10646 passed')
        assert spool.pending() == [entry]

    def test_a_verdict_recorded_but_never_drained_SURVIVES_a_new_process(self, spool, tmp_path):
        """The kill -9 between the gate finishing and the forge answering. A spool that held the
        entry in memory until `drain` would pass every other test in this file and lose exactly the
        verdict it exists to protect.
        """
        spool.record(JOB, verdict='FAIL', detail='3 failed')
        reopened = Spool(tmp_path / 'spool')
        assert [e.verdict for e in reopened.pending()] == ['FAIL']
        assert reopened.pending()[0].detail == '3 failed'

    def test_the_job_IDENTITY_round_trips(self, spool):
        sharded = Job(id='slow', kind=TEST_RUN, shard=2, n_shards=4)
        spool.record(sharded, verdict='PASS', detail='')
        assert spool.pending()[0].job.claim_key() == sharded.claim_key()

    def test_a_word_outside_the_VOCABULARY_is_refused_before_anything_is_written(self, spool):
        """A fourth state must not reach the disk either. Recording first and validating at drain
        would put an unpublishable entry in the spool permanently -- which then trips the backlog
        alarm forever, for a reason the alarm does not name.
        """
        with pytest.raises(ValueError, match='verdict'):
            spool.record(JOB, verdict='ERROR', detail='')
        assert spool.pending() == []

    @pytest.mark.parametrize('word', sorted(VERDICTS))
    def test_the_vocabulary_is_the_GATE_vocabulary(self, spool, word):
        assert spool.record(JOB, verdict=word, detail='').verdict == word


class TestTheWriteItselfIsCrashSafe:
    def test_a_half_written_entry_is_never_VISIBLE(self, spool):
        """Atomic replace, so a crash mid-write leaves a scratch file rather than a truncated
        entry. A reader that saw the partial name would report corruption for a verdict that was
        never claimed to exist.
        """
        spool.record(JOB, verdict='PASS', detail='x' * 50_000)
        assert list(spool.pending_dir.glob('*.tmp')) == []
        assert len(spool.pending()) == 1

    def test_an_abandoned_SCRATCH_file_is_ignored_not_read(self, spool):
        """Exactly what a crash mid-write leaves behind. It is not an entry and must not be read as
        one -- nor counted toward the backlog alarm.
        """
        spool.record(JOB, verdict='PASS', detail='real')
        (spool.pending_dir / 'abandoned.json.tmp').write_text('{"truncated', encoding='utf-8')
        assert [e.detail for e in spool.pending()] == ['real']

    def test_a_CORRUPT_entry_is_REPORTED_not_skipped(self, spool):
        """THE DISGUISE THIS FORBIDS: a reader that skipped unparseable files would turn a damaged
        verdict into no verdict, silently, and the spool would look empty and healthy.
        """
        spool.record(JOB, verdict='PASS', detail='real')
        (spool.pending_dir / 'broken.json').write_text('{"id": "broken", "verd', encoding='utf-8')
        with pytest.raises(SpoolCorruptError, match='broken.json'):
            spool.pending()

    def test_an_entry_missing_a_REQUIRED_field_is_corrupt_too(self, spool):
        """Not just unparseable JSON. A well-formed object with no verdict is exactly what a schema
        change would leave behind, and reading it as "no verdict" is the same loss.
        """
        (spool.pending_dir / 'partial.json').write_text('{"id": "partial"}', encoding='utf-8')
        with pytest.raises(SpoolCorruptError, match='partial.json'):
            spool.pending()

    def test_the_corruption_report_NAMES_the_file(self, spool):
        (spool.pending_dir / 'named.json').write_text('nonsense', encoding='utf-8')
        with pytest.raises(SpoolCorruptError) as caught:
            spool.pending()
        assert 'named.json' in str(caught.value)


class TestDrainingPublishesAndMarks:
    def test_a_drained_entry_stops_being_pending(self, spool, publisher):
        spool.record(JOB, verdict='PASS', detail='green')
        spool.drain(publisher)
        assert spool.pending() == []

    def test_a_drained_entry_is_KEPT_not_deleted(self, spool, publisher):
        """The record of what was published is the audit trail. Deleting it would make "we never
        recorded that" and "we published it and threw the evidence away" indistinguishable.
        """
        entry = spool.record(JOB, verdict='PASS', detail='green')
        spool.drain(publisher)
        assert (spool.published_dir / f'{entry.id}.json').exists()

    def test_the_verdict_reaches_the_forge(self, spool, publisher, forge):
        spool.record(JOB, verdict='FAIL', detail='3 failed, 10643 passed')
        spool.drain(publisher)
        assert ForgeStore('ns', forge, role=Role.SUBMITTER).verdict(JOB) == 'FAIL'
        assert '3 failed, 10643 passed' in ForgeStore('ns', forge, role=Role.SUBMITTER).verdict_detail(JOB)

    def test_a_FAILED_publish_leaves_the_entry_pending(self, spool):
        """The whole point. The verdict must outlive the forge being down."""
        spool.record(JOB, verdict='PASS', detail='green')
        report = spool.drain(CountingPublisher(fail=True))
        assert len(spool.pending()) == 1
        assert report.failed and 'the forge is down' in report.failed[0][1]

    def test_a_failed_publish_does_not_stop_the_OTHERS(self, spool, publisher):
        """A per-entry failure that aborted the drain would let one poisoned entry hold every later
        verdict hostage -- and the backlog alarm would blame the wrong thing.
        """
        spool.record(JOB, verdict='PASS', detail='a')
        spool.record(Job(id='j2', kind=TEST_RUN), verdict='FAIL', detail='b')
        report = spool.drain(publisher)
        assert len(report.published) == 2

    def test_the_report_says_what_happened(self, spool, publisher):
        assert isinstance(spool.drain(publisher), DrainReport)
        entry = spool.record(JOB, verdict='PASS', detail='')
        assert spool.drain(publisher).published == [entry.id]


class TestReplayIsIdempotent:
    """The crash windows, put in the order a crash would leave them."""

    def test_CRASH_AFTER_POST_BEFORE_MARK_does_not_post_twice(self, spool, publisher, forge):
        """THE ONE THAT DUPLICATES, and the reason the spool assigns an id at all.

        The publish succeeded, the machine died before the entry moved to `published/`, and the
        entry is still pending. A replay that trusted the spool alone would post the verdict a
        second time -- one job, two verdict comments, and a reader with no way to tell which is
        current. The marker in the comment body is what makes the forge answer "already done".
        """
        entry = spool.record(JOB, verdict='PASS', detail='10646 passed')
        publisher.publish(entry)  # the POST lands...
        # ...and the process dies here, before `mark_published`.
        assert len(spool.pending()) == 1, 'precondition: the crash left the entry pending'

        before = len(forge.comments(1))
        spool.drain(publisher)
        assert len(forge.comments(1)) == before, 'the replay posted a second verdict comment'
        assert spool.pending() == []

    def test_the_marker_is_what_the_replay_recognises(self, spool, publisher, forge):
        entry = spool.record(JOB, verdict='PASS', detail='green')
        publisher.publish(entry)
        assert any(entry.marker() in c.body for c in forge.comments(1))
        assert publisher.is_published(entry) is True

    def test_an_UNPUBLISHED_entry_is_not_mistaken_for_a_published_one(self, spool, publisher):
        entry = spool.record(JOB, verdict='PASS', detail='green')
        assert publisher.is_published(entry) is False

    def test_a_DIFFERENT_verdict_for_the_same_job_is_not_mistaken_for_this_one(self, spool, publisher, forge):
        """A retry after INCONCLUSIVE is a legitimate second verdict. Keying idempotence on the JOB
        rather than on the spool entry would silently drop it -- the job would keep its stale
        INCONCLUSIVE forever, and the retry that fixed it would look published.
        """
        first = spool.record(JOB, verdict='INCONCLUSIVE', detail='node down')
        spool.drain(publisher)
        second = spool.record(JOB, verdict='PASS', detail='green on retry')
        assert publisher.is_published(second) is False
        spool.drain(publisher)
        assert ForgeStore('ns', forge, role=Role.SUBMITTER).verdict(JOB) == 'PASS'
        assert first.id != second.id

    def test_CRASH_AFTER_COMMENT_BEFORE_LABEL_is_repaired_not_repeated(self, spool, publisher, forge):
        """`record_verdict` is comment -> label -> close, and it is not atomic. A crash between the
        comment and the label leaves the marker present but `verdict()` answering None: the spool
        would call it published and the job would keep an unearned no-verdict forever.

        So a replay CONVERGES rather than repeating -- it finishes the interrupted publish instead
        of posting a second comment.
        """
        entry = spool.record(JOB, verdict='PASS', detail='green')
        store = ForgeStore('ns', forge, role=Role.SUBMITTER)
        number = store.work_item_number(entry.job, create=True)
        forge.add_comment(number, f'**PASS**\n\n```\ngreen\n```\n\n{entry.marker()}')
        # dead here: comment posted, no label, still open.
        assert store.verdict(JOB) is None
        assert publisher.is_published(entry) is False, 'a comment alone is not a published verdict'

        before = len(forge.comments(number))
        spool.drain(publisher)
        assert store.verdict(JOB) == 'PASS'
        assert store.item_state(JOB) == 'closed'
        assert len(forge.comments(number)) == before, 'the repair posted a duplicate comment'

    def test_draining_TWICE_changes_nothing(self, spool, publisher, forge):
        spool.record(JOB, verdict='PASS', detail='green')
        spool.drain(publisher)
        before = len(forge.comments(1))
        spool.drain(publisher)
        assert len(forge.comments(1)) == before

    def test_a_replay_after_a_FAILED_publish_still_publishes_once(self, spool, publisher, forge):
        spool.record(JOB, verdict='PASS', detail='green')
        spool.drain(CountingPublisher(fail=True))
        spool.drain(publisher)
        assert ForgeStore('ns', forge, role=Role.SUBMITTER).verdict(JOB) == 'PASS'
        assert len([c for c in forge.comments(1) if '**PASS**' in c.body]) == 1


class TestAnUndrainableSpoolIsLOUD:
    """Silent accumulation is how "the runners are working" and "no verdicts since Tuesday"
    coexist. The alarm has to be something a caller cannot ignore by not looking.
    """

    def test_a_stuck_entry_RAISES_once_it_is_stale(self, tmp_path):
        spool = Spool(tmp_path / 's', stale_after_seconds=0.05)
        spool.record(JOB, verdict='PASS', detail='green')
        time.sleep(0.15)
        with pytest.raises(SpoolBacklogError):
            spool.drain(CountingPublisher(fail=True))

    def test_the_alarm_names_the_AGE_and_the_COUNT(self, tmp_path):
        """An alarm that says only "backlog" sends the reader to look for the wrong thing. It must
        carry how many and how old, because one entry stuck for a minute and two hundred stuck for a
        day are different incidents.
        """
        spool = Spool(tmp_path / 's', stale_after_seconds=0.05)
        for n in range(3):
            spool.record(Job(id=f'j{n}', kind=TEST_RUN), verdict='PASS', detail='')
        time.sleep(0.15)
        with pytest.raises(SpoolBacklogError) as caught:
            spool.drain(CountingPublisher(fail=True))
        message = str(caught.value)
        assert '3' in message
        assert 'oldest' in message.lower()

    def test_a_FRESH_failure_is_not_yet_an_alarm(self, tmp_path):
        """A forge blip that the next tick fixes is not an incident. An alarm that fired on the
        first failed publish would be tuned out within a day, which is the same as not having one.
        """
        spool = Spool(tmp_path / 's', stale_after_seconds=60.0)
        spool.record(JOB, verdict='PASS', detail='green')
        report = spool.drain(CountingPublisher(fail=True))
        assert report.failed

    def test_a_spool_that_DRAINS_is_silent(self, tmp_path, publisher):
        spool = Spool(tmp_path / 's', stale_after_seconds=0.0)
        spool.record(JOB, verdict='PASS', detail='green')
        spool.drain(publisher)

    def test_the_alarm_fires_even_when_nothing_was_attempted_this_tick(self, tmp_path):
        """The dangerous shape is a spool nobody drains at all. `drain` on an empty-looking tick
        must still inspect what is already sitting there.
        """
        spool = Spool(tmp_path / 's', stale_after_seconds=0.05)
        spool.record(JOB, verdict='PASS', detail='green')
        time.sleep(0.15)
        with pytest.raises(SpoolBacklogError):
            spool.drain(CountingPublisher(fail=True))

    def test_backlog_is_readable_WITHOUT_draining(self, tmp_path):
        """A monitor must be able to ask without publishing anything."""
        spool = Spool(tmp_path / 's', stale_after_seconds=0.05)
        spool.record(JOB, verdict='PASS', detail='green')
        time.sleep(0.15)
        assert spool.stale_entries() != []


class TestItIsAPublisher:
    def test_the_forge_publisher_satisfies_the_protocol(self, publisher):
        assert isinstance(publisher, Publisher)

    def test_the_counting_publisher_does_too(self):
        assert isinstance(CountingPublisher(), Publisher)


class TestConcurrentRecordersDoNotCOLLIDE:
    def test_sixteen_threads_recording_at_once_lose_nothing(self, spool):
        """Two runners on one box record at the same instant. An id derived from the clock, or from
        the job, would have them overwrite each other -- and the loss would be invisible, because
        each thread's own `record` returned successfully.
        """
        barrier = threading.Barrier(16)

        def attempt(n: int) -> None:
            barrier.wait()
            spool.record(Job(id=f'j{n}', kind=TEST_RUN), verdict='PASS', detail=f'run {n}')

        threads = [threading.Thread(target=attempt, args=(i,)) for i in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len({e.id for e in spool.pending()}) == 16


class TestTheSpoolFormatIsInspectable:
    def test_an_entry_is_readable_JSON_on_disk(self, spool):
        """An operator recovering a box must be able to read a verdict without this package. A
        pickled or binary spool would make the durability guarantee depend on the code that failed.
        """
        entry = spool.record(JOB, verdict='PASS', detail='10646 passed')
        raw = json.loads((spool.pending_dir / f'{entry.id}.json').read_text(encoding='utf-8'))
        assert raw['verdict'] == 'PASS'
        assert raw['detail'] == '10646 passed'

    def test_the_marker_is_derived_from_the_id_alone(self):
        entry = SpoolEntry(id='abc123', job=JOB, verdict='PASS', detail='', recorded_at=0.0)
        assert entry.marker() == '[spool:abc123]'

    def test_the_label_vocabulary_is_the_stores(self):
        """The spool publishes through the store, so it must not invent a second mapping."""
        assert set(VERDICT_LABELS) == VERDICTS

    def test_the_directories_are_created_on_DEMAND(self, tmp_path):
        """A spool whose directory must pre-exist is one that silently does nothing on a fresh box."""
        spool = Spool(tmp_path / 'deep' / 'nested' / 'spool')
        spool.record(JOB, verdict='PASS', detail='')
        assert Path(spool.pending_dir).is_dir()


class TestTamperingIsDETECTEDNeverRepaired:
    """A comment is editable by anyone with write access, including a human "fixing" a red result.

    No forge restores immutability -- locking an issue prevents new comments, not edits. The spool
    narrows this by accident of its design: it already holds the text it published, so it can
    compare. What it must NOT do is put the text back.
    """

    def test_an_untouched_verdict_audits_clean(self, spool, publisher):
        spool.record(JOB, verdict='FAIL', detail='3 failed, 10643 passed')
        spool.drain(publisher)
        assert spool.audit(publisher) == []

    def test_an_EDITED_verdict_is_reported(self, spool, publisher, forge):
        """The named regression: someone edits a red result into a green one and nothing notices."""
        entry = spool.record(JOB, verdict='FAIL', detail='3 failed, 10643 passed')
        spool.drain(publisher)
        number = publisher.store.work_item_number(JOB)
        edited = next(c for c in forge.comments(number) if entry.marker() in c.body)
        forge._comments[number] = [
            Comment(id=edited.id, body=f'**PASS**\n\n```\nall good actually\n```\n\n{entry.marker()}')
            if c.id == edited.id
            else c
            for c in forge.comments(number)
        ]
        with pytest.raises(VerdictTamperedError) as caught:
            spool.audit(publisher)
        assert [f.entry_id for f in caught.value.findings] == [entry.id]

    def test_a_DELETED_verdict_is_reported_differently_from_an_edited_one(self, spool, publisher, forge):
        """An edit is a disagreement to have with a person; a disappearance is an incident. A single
        "tampered" verdict would send the reader to do the wrong thing half the time.
        """
        spool.record(JOB, verdict='FAIL', detail='3 failed')
        spool.drain(publisher)
        number = publisher.store.work_item_number(JOB)
        forge._comments[number] = []
        with pytest.raises(VerdictTamperedError) as caught:
            spool.audit(publisher)
        assert 'no longer present' in caught.value.findings[0].reason

    def test_the_audit_does_NOT_put_the_text_back(self, spool, publisher, forge):
        """THE REQUIREMENT MOST EASILY BROKEN BY BEING HELPFUL. Restoring silently converts a
        visible disagreement into an invisible fight between a person and a daemon -- and the
        person, who can see the issue and cannot see the daemon, loses without learning why.
        """
        entry = spool.record(JOB, verdict='FAIL', detail='3 failed')
        spool.drain(publisher)
        number = publisher.store.work_item_number(JOB)
        tampered = f'edited by a human\n\n{entry.marker()}'
        forge._comments[number] = [Comment(id=999, body=tampered)]
        with pytest.raises(VerdictTamperedError):
            spool.audit(publisher)
        assert forge.comments(number)[0].body == tampered, 'the audit rewrote the forge'
        assert len(forge.comments(number)) == 1, 'the audit posted a correction'

    def test_the_finding_carries_EVERY_mismatch_not_just_the_first(self, spool, publisher, forge):
        for n in range(3):
            spool.record(Job(id=f'j{n}', kind=TEST_RUN), verdict='FAIL', detail=f'detail {n}')
        spool.drain(publisher)
        for item in forge.list_work_items():
            forge._comments[item.number] = []
        with pytest.raises(VerdictTamperedError) as caught:
            spool.audit(publisher)
        assert len(caught.value.findings) == 3

    def test_an_UNPUBLISHED_entry_is_not_audited(self, spool, publisher):
        """The comparison needs both halves. Auditing something never sent would report every
        pending verdict as missing -- an alarm that fires on the normal case is not an alarm.
        """
        spool.record(JOB, verdict='PASS', detail='green')
        assert spool.audit(publisher) == []

    def test_the_marker_alone_does_not_certify_the_TEXT(self, spool, publisher, forge):
        """The limit, as a test rather than a sentence. A body carrying the right marker and the
        wrong text is exactly what a delete-and-repost produces, and the audit catches it ONLY
        because it compares the text -- not because the marker told it anything.
        """
        entry = spool.record(JOB, verdict='FAIL', detail='the original evidence')
        spool.drain(publisher)
        number = publisher.store.work_item_number(JOB)
        forge._comments[number] = [Comment(id=1000, body=f'different text {entry.marker()}')]
        with pytest.raises(VerdictTamperedError):
            spool.audit(publisher)

    def test_the_forge_publisher_can_be_READ_back(self, publisher):
        assert isinstance(publisher, PublishedTextReader)


class TestTheDurabilityLimitIsNAMED:
    def test_directory_fsync_availability_is_INSPECTABLE(self):
        """A caller deciding how much to trust this spool must be able to READ the answer rather
        than infer it from a silent `except OSError`. On Windows it is False, and our runners are
        Windows -- so `kill -9` is fully covered and a power cut is not.
        """
        assert DIRECTORY_FSYNC_AVAILABLE is (os.name != 'nt')

    def test_the_no_op_is_documented_AS_a_no_op(self):
        """A hole that is named is a different defect from a hole the docs imply does not exist."""
        doc = durable_module._fsync_directory.__doc__
        assert 'DOES NOTHING' in doc
        assert 'power cut' in doc.lower()
