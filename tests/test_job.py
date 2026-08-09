"""One job model for both instances. The `kind` is the ONLY difference between them.

THE FOUNDING OBSERVATION, from the design doc:

    the collaboration system and the test system are NOT orthogonal -- they are two instances of
    the same loop, and building them as two systems would build two schedulers, two queues and two
    verdict vocabularies for one problem.

    work item -> atomic claim -> execute on a box with capacity -> verdict -> record to store

    |            | collaboration          | test system                        |
    | work item  | issue (dev task)       | candidate (tier awaiting a run)    |
    | executor   | worker session (LLM)   | runner (deterministic, ci_loop)    |
    | capacity   | RAM, session count     | RAM, vendor class, time budget     |
    | verdict    | gate green + lead      | PASS / FAIL / INCONCLUSIVE         |

    The only difference is a JOB PROPERTY: `kind: agent-task | test-run`.

So these tests are written to fail if the two ever drift into separate shapes -- a field only one
kind has, an admission path only one kind takes, a verdict word only one kind may use. Every
assertion below is really the same assertion: **one loop, two kinds**.

WHY THE JOB CARRIES ITS OWN ADMISSION INPUTS. The scheduler must be able to decide without asking
the executor anything: "spawn a claude (~200 MB)" and "run a jmag tier (vendor class, measured
667 s shared)" are the same question with different numbers. A job that cannot state its own cost
forces the admission layer to special-case its kind, which is exactly the second scheduler the
design refuses.
"""

from __future__ import annotations

from dataclasses import fields

import pytest

from agent_swarm import KNOWN_CLASSES, WHOLE_BOX
from agent_swarm.job import AGENT_TASK, TEST_RUN, Job, JobKind


class TestTheTwoKinds:
    def test_both_kinds_exist_and_are_distinct(self):
        assert AGENT_TASK != TEST_RUN
        assert {AGENT_TASK, TEST_RUN} == set(JobKind)

    def test_a_kind_is_the_ONLY_structural_difference(self):
        """DISCRIMINATING. Two jobs identical but for their kind must differ in NOTHING else --
        no extra field, no missing one. The moment one kind needs a field the other cannot have,
        the single loop has quietly become two.
        """
        common = {'id': 'x', 'ram_gib': 1.0, 'exclusivity': WHOLE_BOX}
        a = Job(kind=AGENT_TASK, **common)
        b = Job(kind=TEST_RUN, **common)
        # `dataclasses.fields`, not `vars()`: the model is slotted (no instance __dict__), and the
        # declared fields are the stricter probe anyway -- they are the SHAPE, while a __dict__
        # only shows what happens to be set.
        assert {f.name for f in fields(a)} == {f.name for f in fields(b)}
        assert all(getattr(a, f.name) == getattr(b, f.name) for f in fields(a) if f.name != 'kind')

    def test_the_kind_is_not_free_text(self):
        """A string would let a typo create a third kind that nothing schedules."""
        with pytest.raises((ValueError, TypeError)):
            Job(id='x', kind='test_run')  # type: ignore[arg-type]


class TestItStatesItsOwnCost:
    """The scheduler decides without asking the executor. Both kinds answer the same questions."""

    def test_an_agent_task_prices_itself(self):
        """~200 MB for a claude session is the design's own example."""
        job = Job(id='x', kind=AGENT_TASK, ram_gib=0.2)
        assert job.ram_gib == pytest.approx(0.2)

    def test_a_test_run_prices_itself(self):
        job = Job(id='x', kind=TEST_RUN, ram_gib=12.5, solo_seconds=356.0, ceiling_seconds=600.0)
        assert job.solo_seconds == pytest.approx(356.0)

    def test_an_unpriced_job_is_LEGAL(self):
        """Refusing unpriced work would stop the fleet ever taking anything new -- and running it
        is how a price gets measured. `None` means unknown, never zero.
        """
        job = Job(id='x', kind=TEST_RUN)
        assert job.ram_gib is None
        assert job.solo_seconds is None

    def test_the_default_exclusivity_is_the_WHOLE_BOX(self):
        """DEFAULT-DENY carries into the model: a job that forgot to declare its class must not be
        granted the right to run beside everything.
        """
        assert Job(id='x', kind=TEST_RUN).exclusivity == WHOLE_BOX

    def test_a_declared_exclusivity_must_be_a_KNOWN_class(self):
        job = Job(id='x', kind=TEST_RUN, exclusivity='vendor:femm')
        assert job.exclusivity in KNOWN_CLASSES


class TestTheClaimIdentity:
    def test_a_job_has_a_stable_claim_key(self):
        assert Job(id='abc', kind=TEST_RUN).claim_key() == Job(id='abc', kind=TEST_RUN).claim_key()

    def test_two_KINDS_of_the_same_id_are_DIFFERENT_work(self):
        """An issue and the test run for that issue share an id and are not the same job; claiming
        one must not block the other.
        """
        assert Job(id='abc', kind=AGENT_TASK).claim_key() != Job(id='abc', kind=TEST_RUN).claim_key()

    def test_shards_of_one_job_claim_SEPARATELY(self):
        """Otherwise shard 2 is refused while shard 1 is held and sharding degrades to serial
        WITHOUT erroring -- nothing fails, the job simply never gets faster.
        """
        a = Job(id='j', kind=TEST_RUN, shard=1, n_shards=2)
        b = Job(id='j', kind=TEST_RUN, shard=2, n_shards=2)
        assert a.claim_key() != b.claim_key()

    def test_the_shard_WIDTH_is_part_of_the_identity(self):
        """A 2-way shard 1 and a 4-way shard 1 cover different slices."""
        a = Job(id='j', kind=TEST_RUN, shard=1, n_shards=2)
        b = Job(id='j', kind=TEST_RUN, shard=1, n_shards=4)
        assert a.claim_key() != b.claim_key()

    def test_an_unsharded_job_keys_WITHOUT_a_shard_suffix(self):
        """Byte-identical to the pre-sharding key, so introducing shards did not orphan every
        claim already in the store.
        """
        assert '/' not in Job(id='j', kind=TEST_RUN).claim_key().removeprefix('test-run/')


class TestItIsHashableAndComparable:
    def test_jobs_are_usable_as_dict_keys(self):
        """The scheduler holds sets of in-flight work; an unhashable job forces a parallel index."""
        assert len({Job(id='a', kind=TEST_RUN), Job(id='a', kind=TEST_RUN)}) == 1

    def test_a_job_is_immutable(self):
        """A job mutated after admission would have been admitted under different numbers."""
        job = Job(id='a', kind=TEST_RUN, ram_gib=1.0)
        with pytest.raises(Exception):  # noqa: B017 -- dataclass raises FrozenInstanceError
            job.ram_gib = 99.0  # type: ignore[misc]
