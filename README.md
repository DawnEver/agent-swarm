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

`agent_swarm.seats` — a **fleet-wide** resource with N holders (a floating licence, a bench of
physical rigs). `admission`'s `vendor:*` class plus `exclusive`'s lock already serialise one box;
a file in `%TEMP%` cannot see another host, so ten boxes each holding their own local lock is ten
concurrent sessions against a four-seat licence. `SeatPool` generalises the comment-id claim from
"lowest wins" to "the lowest N win" — the soundness argument is unchanged, because your rank among
live claims can only fall. `seats(tool) -> int` is configuration the CALLER supplies; an undeclared
tool raises rather than defaulting, since 1 serialises a site that bought four and anything larger
invents capacity. CAS *and* lease *and* owner-checked release *and* a heartbeat: `ci_tick.claim`
traded the first for the second and parked jobs for hours, and the trade was never necessary.

`agent_swarm.pull` — the surface for executors that can be INFORMED but not COMMANDED: a human, a
TUI agent. `available` / `take` / `report`, over the primitives the CI runner already uses, so a
person and a runner contend for one claim instead of holding two. No new protocol: only one kind of
executor had ever been built, which is why this looked like part of the gate.

`agent_swarm.workbench_cli` — the way IN to that surface, and without it the third executor kind is
implemented and unusable. `list` / `take` / `report` over the same claim the CI runner uses:

```bash
python -m agent_swarm.workbench_cli --repo OWNER/NAME --namespace ns list
python -m agent_swarm.workbench_cli --repo OWNER/NAME --namespace ns take test-run/abc -- pytest -q
```

**There is no detached `take`** — a claim is only ever held by a live process, because "claim it and
walk away" is the parking defect with a friendly name. `take` claims, beats the lease, runs your
command as a child, reports the verdict and releases. Close the terminal and you lose the ticket in
minutes rather than hours: the beater dies with the process, and `lifetime` binds the child so the
work stops too. Every failure is an exit code, and an empty queue (`0`) is a different code from an
unreachable forge (`3`).

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
