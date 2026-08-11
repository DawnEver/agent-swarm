---
created: 2026-08-11
accessed: 2026-08-11
---

# The file hierarchy of agent-swarm, and why it stopped being prose

Landed as `191aad3`. Suite 1212 passed / 3 skipped / 51 live deselected.

## The finding

33 modules, 11.5k lines of source against 15.1k of tests, **acyclic, no upward edges** — and
**nothing that would have noticed any of those going wrong**. `__init__` had carried the sentence
"dependency arrow strictly L2 -> L1 -> L0" since the package was three modules. It was **right by
accident**, which is this project's named defect class: a declaration nothing consults.

The fix is not a better sentence. It is `layers.py` — a census the suite reads.

## The hierarchy

Lowest first. **A module may import its own layer and anything below it; never above.**

| layer | modules | the test that decides membership |
|---|---|---|
| `HOST` | `durable` `lanes` `layers` `lifetime` `procs` `provenance` | machine mechanics — nothing here knows what an issue or a verdict IS |
| `DRIVER` | `credentials` `forge` `testing` | the outside world and its stand-ins |
| `JOB` | `admission` `agent_executor` `allocator` `claim` `exclusive` `fabric` `forge_store` `item_index` `job` `loop` `pull` `roadmap` `seats` `spool` `status` `store` | the layer the package exists to be |
| `ENTRY` | `bench` `board` `clock` `rem_bridge` `swarmctl` `tick` `workbench_cli` | operator-facing; imports freely, **nothing imports it** |

The executor adapters (`agent_executor`, `fabric`) sit in `JOB`, not below it. By the package's own
vocabulary test, a thing that turns a Job into a session must speak both vocabularies, and **needing
the word "issue" at all puts you in this layer**.

Same-layer edges are legal, so the ordering alone has no teeth against a two-module cycle inside
`JOB`. **Acyclicity is asserted separately** — that is the real guard; the layer check is the
readable one.

## What the guard refuses

`test_the_dependency_arrow_is_enforced.py` walks every import with `ast` and reds on: an upward
edge, a cycle, **a module with no placement**, **a placement whose module is gone** (the dead-entry
converse — without it a deletion leaves coverage-shaped residue forever), a dev tool on a
load-bearing path, and a front door that lies.

**The census proved itself on its first run by refusing `layers.py`.** That is the cheapest possible
evidence that it is exhaustive by machine rather than by care.

## Three lying declarations this removed

1. **The front door described a package that no longer existed.** "WHAT IS HERE TODAY: `admission`,
   `job`, `store`" — with 33 modules behind it, in the first place any reader looks. **The fix is
   delegation, not a longer list**: a 33-name inventory drifts identically. The docstring now says
   where the census lives and the test asserts it still does.
2. **`__all__` exported five modules' symbols out of 33** — `claim`, the arbitration everything else
   is built on, was invisible from the front door. Now every `JOB` module is exported, enforced.
3. **`test_this_package_names_no_specific_project` had a scope narrower than its name.** Its
   docstring promised an exemption for "docstrings"; it collected only `body[0]`, so an **attribute
   docstring** — which `help()` renders and every reader calls a docstring — was rejected. That is
   the scope-lie variant: it does not make you distrust the check, **it makes you reword true
   provenance to appease it**. Replaced with the exact rule — a bare string statement is evaluated
   and discarded, so it can never be a value the code uses — plus a control proving the exemption
   did not switch the scanner off.

## The drawer, named so it cannot deepen

`DEV_TOOL = {bench, rem_bridge}`. Borrowed from motronics' four-valued `PLACEMENT` and for its
stated reason: **generic makes a module portable, it does not make it BELONG here**; without a list,
a job-layer package becomes the dev-tool drawer.

`rem_bridge` is the sharp case — it parses **one specific plugin's file format** and passed the
no-specific-project guard only because "rem" is not in that guard's noun list. The exemption is now
named rather than resting on a gap. Nothing outside `DEV_TOOL` and `ENTRY` may import one.

## Still open, deliberately not done

- **`swarmctl` is 1636 lines, 14% of the package, and never touches `Job`.** It is the only module
  for which "independently shippable" is true today. Splitting it is correct direction and
  **expensive** — it blocks nothing, so it stays.
- **`fabric` has no production entry point** — see the correction below. This is the real one.
- `loop` / `tick` / `clock` is a good decomposition that **readers will confuse**, and only `clock`'s
  docstring says why the three are separate.

---

## CORRECTION (same day, from an audit): the "7 unimported modules" claim was wrong

I wrote above that seven modules have no in-package importer and that **"AVAILABLE, not ENFORCED" is
a global property of this package**. That generalised from a count without checking any of the
seven. Audited one by one:

| module | verdict |
|---|---|
| `lanes` | **WIRED** — `motronics/scripts/lanes/new_lane.py:55`, `prune_lanes.py:36` |
| `rem_bridge` | **WIRED and final** — runnable as `python -m agent_swarm.rem_bridge`; `DEV_TOOL`; no importer is CORRECT |
| `provenance` | **DECLINED on purpose**, in writing, by its intended consumer |
| `board` | **OWED but externally BLOCKED**, and it says so itself |
| `bench` | **OWED an entry point** — the only one truly unreachable |
| `seats` | **OWED** — needs a user input (seat counts), not a code change |
| `fabric` | **OWED** — the real gap |

**The error was collapsing "no importer" into "no caller"** — the exact distinction I had warned the
auditor about one message earlier. A count is not a finding.

### The two facts that make the audit trustworthy rather than a grep

- **Zero dynamic loads inside `agent_swarm`**, so static analysis IS complete for this package —
  a property, not an assumption.
- **`motronics/scripts/ci/ci_tick.py:1532-1533` imports `agent_swarm.forge`/`status` via
  `importlib`**, so over there a `from agent_swarm` grep alone misses live consumers.
- NOT searched: any other checkout on the box, and the TUI agents' repos. Stated so the reader does
  not supply "everything".

All seven were introduced 2026-08-10/11. **Nothing here is a rotted orphan**; "DEAD" is the answer
for none of them.

### `provenance` is the item worth remembering

Its consumer read it and **declined it deliberately**: importing `agent_swarm.provenance` into
`gate.py` would give the verdict instrument a **version floor on the dependency whose identity it
exists to record**. The exact state you most want reported — agent_swarm present, its `provenance`
submodule absent — would then produce **no gate at all** rather than **no provenance line**.

**An instrument that refuses to measure when the thing it measures is unusual is worse than one that
reports a gap.** The decision is registered with its own cost: the redaction rule now has two
spellings held together by a sentence in each file, and the note says that mechanism is weak.

`board` is the same shape from the other side — **a guard that names its hole is not the defect; the
defect is a doc implying there isn't one.**

### The one real gap

**`fabric` / `agent_executor` / `Fleet` are constructed only in TESTS, in both repos.** The chain
exists and every link is tested; not one is built in production code. `swarmctl` and `workbench_cli`
are the two real entries and neither builds a `Fleet`.

So it is not "fabric is unimported" — **the entire agent-execution half is built, tested, and
unreachable.** The missing call site is whatever daemon owns `tick`, and that daemon does not exist
in either repo. A design decision, not a wiring oversight, and the next milestone after the
credential pin bump.
