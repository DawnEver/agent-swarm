# agent-swarm

The **L1 job layer** shared by the collaboration system and the test system.

## Why one library and not two systems

Those two are the *same* job loop — store → atomic claim → a machine with capacity executes →
verdict → written back — differing only in a job's `kind` (`agent-task` | `test-run`). There is one
scheduler, not two.

```
L2 project   gate.py, testkey, acceptance bars, the case library
L1 swarm     store adapters, job model, atomic claim, ADMISSION, lifecycles, verdict routing
L0 fabric    session transport: open/send/close, capacity FACTS. Facts, never decisions.
```

Dependency arrow strictly L2 → L1 → L0. The vocabulary test decides the layer: a sentence needing
"issue/branch/gate" is swarm or project; one needing only "session/turn/node/pid" is fabric.

## What is here today

`agent_swarm.admission` — the decision half of the job layer. Eight functions that take plain
values and return a reason or a bool:

| decision | question |
|---|---|
| `classes_conflict` / `admission_blockers` | may these two jobs share a box? |
| `capacity_blocker` | does this box have the memory? |
| `time_blocker` | would co-scheduling push it past its own ceiling? |
| `staleness_blocker` | is this checkout current enough for the result to mean anything? |
| `should_retry` | is this a non-answer worth another attempt, or a verdict? |
| `claim_key` / `own_claim_is_abandoned` | which claim namespace, and is mine dead? |

**Extracted, not written fresh.** All of it comes from motronics' `scripts/ci/ci_tick.py`, where it
had been running and measured for months. Every constant is a measurement — `SHARED_SLOWDOWN` is a
box that really slowed 1.9×, not a guess.

## No runtime dependencies, by design

This layer *decides*; it does not reach. Facts arrive as arguments, stores arrive through adapters.
If something here needs a third-party package, check first whether the decision has quietly grown an
I/O half that belongs in an adapter.

```bash
pip install -e ".[dev]"
pytest
```

## Not here yet

The I/O half of admission (claim push/read, class locks, memory probing, checkout distance) still
lives in motronics' `ci_tick.py`. It is store-adapter work and moves when the store adapter exists.
