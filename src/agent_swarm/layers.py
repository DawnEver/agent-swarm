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
    # HOST: a hostname, a registry read, a file under /etc, a hash. No job vocabulary at all --
    # which is what the layer definition asks, and identity is the fact every OTHER layer keys on.
    'identity': HOST,
    'lanes': HOST,
    'lifetime': HOST,
    'procs': HOST,
    # HOST because `forge` (DRIVER) and `swarmctl` (ENTRY) both read it. That is not a preference:
    # the account scheme had grown FOUR spellings precisely because the fact sat too high for the
    # lowest consumer to reach, so `forge` grew its own literals instead.
    'roles': HOST,
    # HOST, and the placement is FORCED by the arrow rather than chosen: `loop` (JOB) imports it, so
    # anything higher would point up. It also earns the placement on the layer's own definition --
    # it speaks about free memory and worker counts and knows nothing about issues, verdicts or
    # jobs, and its capacity source ARRIVES as a callable rather than being reached for.
    'scaling': HOST,
    'provenance': HOST,
    # HOST: it reads TEXT a test runner printed and does arithmetic on it. No job vocabulary at all
    # -- it knows nothing of issues, verdicts or who asked; the run-level kill markers whose
    # vocabulary IS a consumer's arrive as an argument, which is what keeps the placement true.
    'testlog': HOST,
    # HOST, and it is the layer's definition rather than the word "test" that decides: it parses
    # source, imports modules and replays a function. It speaks no job vocabulary -- no issue, no
    # verdict, no runner -- and the two facts that WOULD be a consumer's (the replay denylist, the
    # directories to scan) arrive as arguments rather than being reached for.
    'vacuity': HOST,
    # HOST: file names, file contents, a declared map and a git diff. It knows nothing about issues
    # or verdicts, and every fact that would be a consumer's -- where its source and tests live,
    # which prefixes are scanned, which tests are expensive -- arrives as an argument.
    'selection': HOST,
    # HOST, and the placement follows the layer's definition rather than the word "verdict" in its
    # docstring: it measures a MACHINE -- this interpreter, this installed set, these files on this
    # disk -- and knows nothing about issues, jobs or who is asking. Its two project facts (which
    # distribution roots the closure, which local paths the tests read) arrive as arguments.
    'environment': HOST,
    # HOST: `pre-commit run`, `git diff --cached`, `git add`. Processes and an index, nothing more.
    'hooks': HOST,
    # HOST for `environment`'s reason: it measures THIS interpreter's installed set and THIS disk,
    # and knows nothing about issues, jobs or who is asking. Its consumer facts -- which
    # distributions are in-tree, which licence strings are curated -- arrive as arguments.
    'notices': HOST,
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
    # DRIVER: it is a git remote behind a subprocess. The four-operation PROTOCOL could sit lower,
    # but splitting a seam from its only real implementation buys nothing and costs a file.
    'refstore': DRIVER,
    # DRIVER, and FORCED rather than chosen: it runs `git ls-remote` and `git credential fill`
    # against a real server to tell "you may not see this repository" from "the host is down". The
    # decision half takes the requirement strings already parsed, exactly as `policy` does, so it
    # never reaches for a manifest path it was not told about.
    'installer': DRIVER,
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
    # JOB: "who is alive" and "what can the fleet do" are questions ABOUT runners and the work they
    # take. It reads a RefStore (DRIVER) and the ref grammar, so the arrow points down.
    'liveness': JOB,
    'loop': JOB,
    'pull': JOB,
    # JOB rather than HOST, and the vocabulary test is what decides it: a verdict, an attempt and a
    # shard are job words. It imports nothing -- it is pure path grammar -- so the placement costs
    # nothing and keeps "needing the word verdict puts you in this layer" true.
    'refs': JOB,
    'roadmap': JOB,
    'seats': JOB,
    # JOB, and the vocabulary test is what decides it: a base, a head and a declared intent crossing
    # into the trunk are job words. It imports `refs` (JOB) and `refstore` (DRIVER), at or below it.
    'submission': JOB,
    # JOB, and NOT ENTRY even though it is the end of the road: nothing about it is operator-facing.
    # It takes a store, a trunk name and an injected verdict function, and is meant to be called by
    # a loop rather than typed. What keeps the placement true is that the verdict ARRIVES as a
    # callable -- a module that reached for a test command would be speaking one consumer's language.
    'integration': JOB,
    # JOB because the three words it folds -- PASS, FAIL, INCONCLUSIVE -- are the verdict
    # vocabulary. It imports nothing; the placement is about what it SPEAKS, not what it needs.
    'shards': JOB,
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
    # THE FOUR THAT ARRIVED 2026-08-12, and ENTRY is what they are: each is a command with its own
    # `main`, nothing in this package imports any of them, and every consumer fact they need --
    # a store directory, the trees to sweep, which roots hold importable code, which package and
    # distribution to resolve against -- is a required argument. They are `DEV_TOOL` below.
    'pins': ENTRY,
    'callsites': ENTRY,
    'staged_imports': ENTRY,
    'installed_symbols': ENTRY,
}

DEV_TOOL = frozenset({'bench', 'rem_bridge', 'pins', 'callsites', 'staged_imports', 'installed_symbols'})
"""Development conveniences that happen to live here, NOT part of the job layer.

Borrowed from motronics' four-valued PLACEMENT and for its stated reason: generic makes a module
portable, it does not make it BELONG here; without a list, a job-layer package becomes the
dev-tool drawer. `rem_bridge` is the sharp case -- it parses one specific plugin's file format, and
it passes `test_this_package_names_no_specific_project.py` only because "rem" is not in that
guard's noun list. The guard's SCOPE is narrower than its name, so the exemption is named here
instead of resting on that.

THIS SET IS THE "SHARED DEV-TOOLS HOME" THAT DID NOT EXIST, 2026-08-12. motronics' migration table
had classified four modules `DEV_TOOL` with the reason "they wait here until a shared dev-tools home
exists to want them" -- and no such home was ever built, so "waiting for a destination" and "nobody
did the work" looked identical in that tree. They are the same shape as `bench`: a command, with a
`main`, that nothing in this package imports. So the home is HERE, it is this list plus the ENTRY
layer, and the four are in it. The alternative -- keeping a fourth verdict value alive in a consumer
for a destination nobody was building -- is how legacy debt is created on purpose.

Naming them is not blessing them. The guard keeps the drawer from deepening: nothing outside
`DEV_TOOL` and ENTRY may import one, so no load-bearing path can come to depend on a convenience.
"""


def layer_of(module: str) -> int:
    """The layer of `module`, or KeyError -- deliberately, so an unplaced module cannot default."""
    return LAYERS[module]
