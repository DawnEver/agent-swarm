"""agent-swarm: the L1 job layer shared by the collaboration system and the test system.

The founding observation, from motronics' `design-final-architecture-collaboration-and-test-system-unified.md`:
those two are the SAME job loop -- store -> atomic claim -> a machine with capacity executes ->
verdict -> written back -- differing only in a job's `kind`. There is one scheduler, not two, and
a kind added since (`compute`, one evaluation leg of a numerical study) added no second one either:
the bar for a member of `JobKind` is that it changes nothing but the enum.

    L2 project   gate.py, testkey, acceptance bars, the case library
    L1 swarm     store adapters, job model, atomic claim, ADMISSION, lifecycles, verdict routing
    L0 fabric    session transport: open/send/close, capacity FACTS. Facts, never decisions.

Dependency arrow strictly L2 -> L1 -> L0. The vocabulary test decides the layer: a sentence needing
"issue/branch/gate" is swarm or project; one needing only "session/turn/node/pid" is fabric.

THE MODULE CENSUS IS IN `layers.py`, NOT IN THIS PARAGRAPH -- and that is the point. What stood here
was a hand-maintained inventory of three modules, written when there were three and still saying
"what is here today" at thirty-three. A longer list would have drifted identically. `layers.py`
places EVERY module in HOST / DRIVER / JOB / ENTRY, and `test_the_dependency_arrow_is_enforced.py`
reads it, walks every import with `ast`, and reds on an upward edge, a cycle, an unplaced module or
a placement whose module is gone. The arrow above stops being a sentence and becomes a check.

`admission` was EXTRACTED from motronics' `ci_tick.py`, where it had been running and measured for
months, rather than written fresh. That is deliberate: M1's first block is the extraction, not a new
scheduler. It is stdlib-only by construction, so the move was a file move.
"""

# THE JOB LAYER'S MODULES, re-exported by name. Not decoration: `__all__` used to carry symbols
# from five modules, so `claim` -- the arbitration everything else is built on -- was invisible from
# the front door. `test_the_dependency_arrow_is_enforced.py` asserts this list stays complete.
from agent_swarm import (
    adapters,
    admission,
    agent_executor,
    allocator,
    claim,
    evidence,
    exclusive,
    fabric,
    forge_store,
    integration,
    item_index,
    job,
    layers,
    liveness,
    loop,
    pull,
    refs,
    roadmap,
    scaling,
    seats,
    shards,
    signing,
    spool,
    status,
    store,
    submission,
    throughput,
)
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
from agent_swarm.exclusive import (
    LockBusy,
    LockOwner,
    exclusive_lock,
    lock_dir,
    lock_path_for_class,
    read_owner,
)
from agent_swarm.job import AGENT_TASK, COMPUTE, TEST_RUN, Job, JobKind
from agent_swarm.loop import Box, Executor, Outcome, RegulatedExecutor, RegulatedRun, run_one, run_regulated
from agent_swarm.scaling import (
    Adjustment,
    CapacityUnreadable,
    Regulation,
    Regulator,
    WidthNotHonoured,
    workers_for,
)
from agent_swarm.evidence import RecordVerdict, sign_verdict, verify_verdict
from agent_swarm.store import VERDICTS, InMemoryStore, Store

__all__ = [
    'AGENT_TASK',
    'CHEAP',
    'COMPUTE',
    'SHARED_SLOWDOWN',
    'TEST_RUN',
    'VENDOR_PREFIX',
    'VERDICTS',
    'WHOLE_BOX',
    'Adjustment',
    'Box',
    'CapacityUnreadable',
    'Executor',
    'InMemoryStore',
    'Job',
    'JobKind',
    'LockBusy',
    'LockOwner',
    'Outcome',
    'RegulatedExecutor',
    'RegulatedRun',
    'Regulation',
    'Regulator',
    'Store',
    'WidthNotHonoured',
    'adapters',
    'admission',
    'admission_blockers',
    'agent_executor',
    'allocator',
    'capacity_blocker',
    'claim',
    'claim_key',
    'classes_conflict',
    'evidence',
    'exclusive',
    'exclusive_lock',
    'fabric',
    'forge_store',
    'is_known_class',
    'integration',
    'item_index',
    'job',
    'layers',
    'liveness',
    'lock_dir',
    'lock_path_for_class',
    'loop',
    'own_claim_is_abandoned',
    'pull',
    'read_owner',
    'RecordVerdict',
    'refs',
    'roadmap',
    'run_one',
    'run_regulated',
    'scaling',
    'seats',
    'shards',
    'should_retry',
    'sign_verdict',
    'signing',
    'spool',
    'staleness_blocker',
    'status',
    'store',
    'submission',
    'throughput',
    'time_blocker',
    'verify_verdict',
    'workers_for',
]
