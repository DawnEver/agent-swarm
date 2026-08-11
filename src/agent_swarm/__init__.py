"""agent-swarm: the L1 job layer shared by the collaboration system and the test system.

The founding observation, from motronics' `design-final-architecture-collaboration-and-test-system-unified.md`:
those two are the SAME job loop -- store -> atomic claim -> a machine with capacity executes ->
verdict -> written back -- differing only in a job's `kind` (`agent-task` | `test-run`). There is
one scheduler, not two.

    L2 project   gate.py, testkey, acceptance bars, the case library
    L1 swarm     store adapters, job model, atomic claim, ADMISSION, lifecycles, verdict routing
    L0 fabric    session transport: open/send/close, capacity FACTS. Facts, never decisions.

Dependency arrow strictly L2 -> L1 -> L0. The vocabulary test decides the layer: a sentence needing
"issue/branch/gate" is swarm or project; one needing only "session/turn/node/pid" is fabric.

WHAT IS HERE TODAY: `admission` (the decision layer), `job` (one model, two kinds) and `store`
(the claim/verdict contract, with a reference in-memory implementation). `admission` -- extracted from motronics' `ci_tick.py`, where it had been
running and measured for months, rather than written fresh. That is deliberate: M1's first block is
the extraction, not a new scheduler. Everything in it is stdlib-only by construction so the move
was a file move.
"""

from agent_swarm.admission import (
    CHEAP,
    SHARED_SLOWDOWN,
    VENDOR_PREFIX,
    WHOLE_BOX,
    admission_blockers,
    capacity_blocker,
    claim_key,
    classes_conflict,
    is_known_class,
    own_claim_is_abandoned,
    should_retry,
    staleness_blocker,
    time_blocker,
)
from agent_swarm.job import AGENT_TASK, TEST_RUN, Job, JobKind
from agent_swarm.loop import Box, Executor, Outcome, run_one
from agent_swarm.store import VERDICTS, InMemoryStore, Store

__all__ = [
    'AGENT_TASK',
    'CHEAP',
    'SHARED_SLOWDOWN',
    'TEST_RUN',
    'VENDOR_PREFIX',
    'VERDICTS',
    'WHOLE_BOX',
    'Box',
    'Executor',
    'InMemoryStore',
    'Job',
    'JobKind',
    'Outcome',
    'Store',
    'admission_blockers',
    'capacity_blocker',
    'claim_key',
    'classes_conflict',
    'is_known_class',
    'own_claim_is_abandoned',
    'run_one',
    'should_retry',
    'staleness_blocker',
    'time_blocker',
]
