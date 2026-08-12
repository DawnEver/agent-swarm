"""The job model. ONE shape for every instance of the loop; the `kind` is the only difference.

    work item -> atomic claim -> execute on a box with capacity -> verdict -> record to store

|            | collaboration            | test system                        | computation           |
| work item  | issue (dev task)         | candidate (tier awaiting a run)    | one evaluation leg    |
| executor   | worker session (LLM)     | runner (deterministic, ci_loop)    | a solve on one box    |
| capacity   | RAM, session count       | RAM, vendor class, time budget     | RAM, licensed tool    |
| verdict    | gate green + lead        | PASS / FAIL / INCONCLUSIVE         | PASS / FAIL           |

Building those as two systems would build two schedulers, two queues and two verdict vocabularies
for one problem. Conversational versus deterministic is an attribute of the EXECUTOR, not a system
boundary.

A JOB STATES ITS OWN COST, and that is what keeps the single scheduler honest. "Spawn a claude
(~200 MB)" and "run a jmag tier (vendor class, measured 667 s shared)" are the same admission
question with different numbers. A job that could not price itself would force `admission` to
special-case its kind -- which is the second scheduler this design exists to refuse.

UNKNOWN IS `None`, NEVER ZERO. An unpriced job is legal: refusing one would stop the fleet ever
taking anything new, and running it is how a price gets measured. Zero would mean "free", which is
the safe-looking answer that is exactly wrong.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field

from agent_swarm.admission import WHOLE_BOX, shard_suffix


class JobKind(enum.Enum):
    """The one property that distinguishes the instances of the loop.

    An ENUM rather than a string: a typo in free text creates a kind that nothing schedules and
    nothing reports, and it would surface as work that silently never runs.

    ADDING A MEMBER IS ADDING AN INSTANCE OF THE ONE LOOP, NEVER A SECOND SCHEDULER. The test that
    a kind costs this package nothing but a member is `loop.py`'s: nothing there reads `job.kind`.
    So the bar for a new kind is that it fits `work item -> claim -> execute -> verdict` unchanged.

    `COMPUTE` is ONE EVALUATION LEG of a numerical study -- one point, one parameter set -- and its
    id is a CONTENT ADDRESS its consumer computes. A study of N points is N of these, independently
    claimable, with no relationship between them that this package can see. A search that submits
    one fan-out, waits for it, and submits the next is a CLIENT holding that wait: modelling "one
    round of a search" as a job would force this layer to understand populations, barriers and
    stragglers, which is the second scheduler the docstring above refuses. That property is pinned
    by `TestTheSubstrateKnowsNothingAboutBARRIERS` in `tests/test_job.py`, not by this paragraph.
    """

    AGENT_TASK = 'agent-task'
    TEST_RUN = 'test-run'
    COMPUTE = 'compute'


AGENT_TASK = JobKind.AGENT_TASK
TEST_RUN = JobKind.TEST_RUN
COMPUTE = JobKind.COMPUTE


@dataclass(frozen=True, slots=True)
class Job:
    """One unit of claimable work, of either kind.

    FROZEN, because a job mutated after admission was admitted under different numbers -- the
    scheduler would have decided against a cost that no longer exists. Hashable for the same
    reason the scheduler needs it: it holds sets of in-flight work, and an unhashable job forces a
    parallel index that can disagree with the set.

    Attributes:
        id: identity within its kind -- an issue number, a testkey, a group name.
        kind: which instance of the loop this belongs to.
        ram_gib: measured peak RSS, or ``None`` when nobody has measured it yet.
        exclusivity: a class `admission.is_known_class` accepts -- `WHOLE_BOX`, `CHEAP`, or the
            vendor form `vendor:<name>`, whose names are the CONSUMER's to declare, not this
            package's to enumerate. Defaults to the WHOLE BOX, so a job that forgot to declare one
            cannot be granted the right to run beside everything.
        solo_seconds: measured duration running alone, or ``None``.
        ceiling_seconds: the budget beyond which this job's own timeouts fire, or ``None``.
        shard / n_shards: which slice of a split job, and how wide the split is. Both are part of
            the claim identity.
    """

    id: str
    kind: JobKind
    ram_gib: float | None = None
    exclusivity: str = field(default=WHOLE_BOX)
    solo_seconds: float | None = None
    ceiling_seconds: float | None = None
    shard: int | None = None
    n_shards: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, JobKind):
            msg = f'kind must be a JobKind, got {self.kind!r}. A free-text kind is a kind nothing schedules.'
            raise TypeError(msg)

    def claim_key(self) -> str:
        """The namespace this job contends for. Two jobs with the same key are the SAME work.

        THE KIND IS IN THE KEY: an issue and the test run for that issue share an id and are not
        the same work, so claiming one must not block the other.

        THE SHARD AND ITS WIDTH ARE TOO, because a claim is what stops two runners doing the same
        work -- and two runners taking DIFFERENT SLICES is not the same work, it is the entire
        point of sharding. Leave the shard out and shard 2 is refused while shard 1 is held, so the
        mechanism degrades to serial WITHOUT erroring: nothing fails, the job simply never gets
        faster. The width matters because a 2-way shard 1 and a 4-way shard 1 cover different
        slices.

        An unsharded job keys byte-identically to the pre-sharding form, so introducing shards did
        not orphan every claim already in the store.
        """
        # THE SUFFIX GRAMMAR IS `admission.shard_suffix`'s, not this method's -- see it for the
        # three-copy defect this replaces.
        return f'{self.kind.value}/{self.id}' + shard_suffix(shard=self.shard, n_shards=self.n_shards)
