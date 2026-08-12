"""THE MODULE CENSUS -- the one place that says what this package contains and how it is stacked.

WHY THIS IS DATA AND NOT A PARAGRAPH: `__init__` has carried the sentence "dependency arrow strictly
L2 -> L1 -> L0" since the package was three modules. It is thirty-three now, and MEASURED
2026-08-11 the arrow held -- no cycles, no upward edges. Nothing checked it. It was right by
accident, which is the state this repo has a name for: a declaration nothing consults.

So the arrow moved here, where `test_the_dependency_arrow_is_enforced.py` reads it, walks every
`import` in the package with `ast`, and reds on an upward edge. The table is also EXHAUSTIVE in both
directions: a new module that is not placed reds, and a placed module that no longer exists reds.
Adding a file to this package now REQUIRES answering which layer it is in.

THE LAYERS, lowest first. A module may import its own layer and anything BELOW it; never above.

    HOST     machine mechanics. Processes, worktrees, atomic writes, terminal binding. Carries NO
             job vocabulary -- nothing here knows what an issue or a verdict is.
    DRIVER   the outside world. Gitea's HTTP surface, credential resolution, and the test doubles
             that stand in for them.
    JOB      the job layer proper -- the reason the package exists. Store, model, atomic claim,
             admission, lifecycles, verdict routing. The executor adapters live here too: a thing
             that turns a Job into a session must speak both vocabularies, and by the package's own
             vocabulary test, needing "issue" at all puts you in this layer.
    ENTRY    operator-facing commands and projections. Imports freely; NOTHING imports it.

WHAT THE ORDERING IS NOT: it is not a claim that each layer is independently shippable. `swarmctl`
is the only module for which that is true today (it never touches `Job`).
"""

from __future__ import annotations

HOST = 0
DRIVER = 1
JOB = 2
ENTRY = 3

LAYER_NAMES = {HOST: 'HOST', DRIVER: 'DRIVER', JOB: 'JOB', ENTRY: 'ENTRY'}

LAYERS: dict[str, int] = {
    # HOST -- no job vocabulary. If one of these ever needs the word "issue", it is misplaced.
    'durable': HOST,
    # `layers` places itself, and the first run of the guard REFUSED it for being absent -- which is
    # the cheapest possible demonstration that the census is exhaustive by machine and not by care.
    # HOST because it imports nothing and every layer may read it.
    'layers': HOST,
    'lanes': HOST,
    'lifetime': HOST,
    'procs': HOST,
    # HOST because `forge` (DRIVER) and `swarmctl` (ENTRY) both read it. That is not a preference:
    # the account scheme had grown FOUR spellings precisely because the fact sat too high for the
    # lowest consumer to reach, so `forge` grew its own literals instead.
    'roles': HOST,
    'provenance': HOST,
    # HOST, and the placement follows the layer's definition rather than the word "verdict" in its
    # docstring: it measures a MACHINE -- this interpreter, this installed set, these files on this
    # disk -- and knows nothing about issues, jobs or who is asking. Its two project facts (which
    # distribution roots the closure, which local paths the tests read) arrive as arguments.
    'environment': HOST,
    # DRIVER -- the outside world and its stand-ins.
    'credentials': DRIVER,
    'forge': DRIVER,
    # DRIVER because of `_origin_url`, and the placement is FORCED rather than chosen: `policy`
    # cross-checks a declared repository against `git remote get-url origin`, which is a subprocess
    # against the outside world. Everything ELSE in it is a decision -- precedence, and the refusal
    # when the two disagree -- and that half deliberately takes a MAPPING somebody else parsed, so a
    # consumer reading TOML, JSON or a database reaches the same code. Reading the FILE is the
    # consumer's job: this layer decides, it does not reach for a path it was never told.
    'policy': DRIVER,
    'testing': DRIVER,
    # JOB -- the layer this package exists to be.
    # FORCED, NOT CHOSEN, and the arrow is what forces it: both adapters take a `Job`, so a DRIVER
    # placement would point UP. They are also here rather than in `agent_executor` because
    # `TestTheSeamDoesNotLeakL0Vocabulary` refuses `subprocess` in that file -- a module that both
    # DECLARES the seam and reaches the OS through it has stopped being a seam.
    'adapters': JOB,
    'admission': JOB,
    'agent_executor': JOB,
    'allocator': JOB,
    'claim': JOB,
    'exclusive': JOB,
    'fabric': JOB,
    'forge_store': JOB,
    'item_index': JOB,
    'job': JOB,
    'loop': JOB,
    'pull': JOB,
    # JOB rather than HOST, and the vocabulary test is what decides it: a verdict, an attempt and a
    # shard are job words. It imports nothing -- it is pure path grammar -- so the placement costs
    # nothing and keeps "needing the word verdict puts you in this layer" true.
    'refs': JOB,
    'roadmap': JOB,
    'seats': JOB,
    'spool': JOB,
    'status': JOB,
    'store': JOB,
    # ENTRY -- commands, projections, and the schedule that pulls a tick.
    'bench': ENTRY,
    'board': ENTRY,
    'clock': ENTRY,
    # The production entry the execution path never had. ENTRY, and a ONE-SHOT: it takes one turn
    # and exits, so it cannot become the unattended runner this design forbids. The loop is
    # `clock`'s and `clock` is a human's.
    'fleet_cli': ENTRY,
    'rem_bridge': ENTRY,
    'swarmctl': ENTRY,
    'tick': ENTRY,
    'workbench_cli': ENTRY,
}

DEV_TOOL = frozenset({'bench', 'rem_bridge'})
"""Development conveniences that happen to live here, NOT part of the job layer.

Borrowed from motronics' four-valued PLACEMENT and for its stated reason: generic makes a module
portable, it does not make it BELONG here; without a list, a job-layer package becomes the
dev-tool drawer. `rem_bridge` is the sharp case -- it parses one specific plugin's file format, and
it passes `test_this_package_names_no_specific_project.py` only because "rem" is not in that
guard's noun list. The guard's SCOPE is narrower than its name, so the exemption is named here
instead of resting on that.

Naming them is not blessing them. The guard keeps the drawer from deepening: nothing outside
`DEV_TOOL` and ENTRY may import one, so no load-bearing path can come to depend on a convenience.
"""


def layer_of(module: str) -> int:
    """The layer of `module`, or KeyError -- deliberately, so an unplaced module cannot default."""
    return LAYERS[module]
