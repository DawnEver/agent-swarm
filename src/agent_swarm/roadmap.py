"""THE PRODUCER: a human-authored roadmap becomes claimable `AGENT_TASK` work.

`AGENT_TASK` had a model, a store contract and a loop, and no producer -- so the collaboration half
of the one loop had a queue nothing ever put anything into. This is that half's `_pick_group`.

THE DIVISION OF LABOUR IS THE DESIGN. The human owns vision, roadmap and checkpoint acceptance;
EVERYTHING ELSE IS DERIVED. So this file reads DATA and computes ids, ordering and readiness -- it
never asks the human for anything a machine can work out, and it never invents an answer to
something only they can give (which is why a missing acceptance criterion RAISES rather than getting
a default).

A MANIFEST IS DATA. Narrative belongs in `.claude/memory/`, not in a file the scheduler parses, and
an unknown key is refused at load rather than ignored -- an ignored key is a typo whose value never
takes effect, and the author has no way to tell.

THE SCHEMA (version 1):

    version = 1                        # required; an unknown version refuses rather than guesses

    [[item]]
    key = 'spool-retention'            # required, unique, stable, human-chosen; the id's prefix
    title = 'Retention for refs/ci/…'  # required; the worker's brief. MATERIAL.
    acceptance = 'pytest tests/… -q'   # required, non-blank; how the verdict is decided. MATERIAL.
    priority = 5                       # optional, 0..9 (allocator.PRIORITY_*). A hint.
    needs = ['other-key']              # optional; a cycle RAISES
    exclusivity = 'cheap'              # optional; admission class. Defaults to the whole box.
    ram_gib = 0.5                      # optional; omit when unmeasured -- never write 0

Anything else is an error. `needs`, `priority`, `exclusivity` and `ram_gib` are SCHEDULING facts and
deliberately not part of the identity; see `_material_id`.
"""

from __future__ import annotations

import hashlib
import tomllib
from dataclasses import dataclass

from agent_swarm.allocator import PRIORITY_MAX, PRIORITY_MIN, Candidate
from agent_swarm.job import AGENT_TASK, Job

#: The schema this module implements. An unknown version REFUSES: a scheduler that reads a v2
#: roadmap with v1 rules produces work items that look right and mean something else.
SCHEMA_VERSION = 1

#: Every key an item may carry. The set is closed on purpose -- see the module docstring.
_ITEM_FIELDS = frozenset({'key', 'title', 'acceptance', 'priority', 'needs', 'exclusivity', 'ram_gib'})

#: The fields that decide what the work IS and how it is JUDGED. A change to any of them mints a new
#: id, because a recorded verdict is a statement about a specific brief under a specific criterion,
#: and letting an old PASS answer a rewritten item is the unearned green reached by editing.
_MATERIAL_FIELDS = ('key', 'title', 'acceptance')

#: Length of the id's hash suffix. Long enough that a collision across a human-sized roadmap is not
#: a thing that happens; short enough that the id fits in an issue title beside its key.
_ID_HASH_CHARS = 8


class RoadmapError(ValueError):
    """A roadmap that cannot be turned into work. Always raised at LOAD time, never at claim time.

    One exception type because the caller's response to every case is the same: tell the human, who
    owns this file and is the only one who can fix any of it.
    """


@dataclass(frozen=True, slots=True)
class RoadmapItem:
    """One roadmap entry and the job derived from it.

    The item is kept beside the job rather than folded into it because `acceptance` is what the
    EXECUTOR needs to reach a verdict, and `Job` is the scheduler's shape -- putting the criterion
    there would make every scheduling decision carry a string it must never read.
    """

    key: str
    title: str
    acceptance: str
    priority: int
    needs: tuple[str, ...]
    job: Job


@dataclass(frozen=True, slots=True)
class Roadmap:
    """A validated roadmap: every item well-formed, every dependency present, no cycles."""

    items: tuple[RoadmapItem, ...]

    @property
    def by_key(self) -> dict[str, RoadmapItem]:
        return {i.key: i for i in self.items}

    def candidates(
        self,
        *,
        done: frozenset[str],
        ready_at: dict[str, float] | None = None,
        results: dict[str, tuple[str, ...]] | None = None,
    ) -> list[Candidate]:
        """The items whose dependencies are satisfied, as `allocator.Candidate`s.

        A DEPENDENCY IS ENFORCED HERE, NOT DESCRIBED. An item whose `needs` are not all in ``done``
        is simply not offered, so the ordering the human declared is one the scheduler MUST consult
        -- as opposed to a `needs` list that only documents an intention while the allocator picks
        by score and runs the dependent first.

        ``done`` is the caller's: it comes from recorded verdicts in the store, which this pure
        layer must not read. ``ready_at`` likewise -- and a MISSING entry defaults to 0.0, meaning
        "waiting since the epoch", i.e. maximally aged. That errs toward picking the item, costing
        at worst one out-of-order run; defaulting to the current time would restart the ageing clock
        on every tick and starve the item forever without a single error.
        """
        ready_at = ready_at or {}
        results = results or {}
        return [
            Candidate(
                job=item.job,
                priority=item.priority,
                ready_at=ready_at.get(item.key, 0.0),
                results=results.get(item.key, ()),
            )
            for item in self.items
            if all(need in done for need in item.needs)
        ]


def _material_id(raw: dict) -> str:
    """``<key>@<hash>`` over the material fields only.

    THE KEY IS IN THE ID because a bare hash in an issue title is unreadable by the human who owns
    the roadmap, and the store's ids are the surface they review.

    The hash is over an explicitly ordered, length-prefixed join of `_MATERIAL_FIELDS`, so neither
    the order of keys in the TOML nor a value containing the separator can make two different items
    hash alike.
    """
    parts = ''.join(f'{len(str(raw[f]))}:{raw[f]}' for f in _MATERIAL_FIELDS)
    digest = hashlib.sha256(parts.encode('utf-8')).hexdigest()[:_ID_HASH_CHARS]
    return f'{raw["key"]}@{digest}'


def _require_text(raw: dict, field: str, key: str) -> str:
    value = raw.get(field)
    if not isinstance(value, str) or not value.strip():
        msg = f'item {key!r}: {field} is required and must be non-blank'
        raise RoadmapError(msg)
    return value


def _parse_item(raw: dict) -> RoadmapItem:
    if unknown := sorted(set(raw) - _ITEM_FIELDS):
        msg = f'item {raw.get("key")!r}: unknown field(s) {unknown}. A manifest is data; narrative belongs in memory.'
        raise RoadmapError(msg)
    key = _require_text(raw, 'key', raw.get('key', '<unnamed>'))
    title = _require_text(raw, 'title', key)
    # AN ITEM WITHOUT A CHECKABLE CRITERION IS NOT A WORK ITEM. The verdict vocabulary has three
    # words and none of them means "unanswerable", so queueing such an item produces work that is
    # claimed, run, and then cannot be judged -- and the pressure at that moment is to call it PASS.
    acceptance = _require_text(raw, 'acceptance', key)
    priority = raw.get('priority', PRIORITY_MIN)
    if not isinstance(priority, int) or not (PRIORITY_MIN <= priority <= PRIORITY_MAX):
        msg = f'item {key!r}: priority must be an int in [{PRIORITY_MIN}, {PRIORITY_MAX}], got {priority!r}'
        raise RoadmapError(msg)
    needs = tuple(raw.get('needs', ()))
    job = Job(
        id=_material_id({'key': key, 'title': title, 'acceptance': acceptance}),
        kind=AGENT_TASK,
        ram_gib=raw.get('ram_gib'),
        **({'exclusivity': raw['exclusivity']} if 'exclusivity' in raw else {}),
    )
    return RoadmapItem(key=key, title=title, acceptance=acceptance, priority=priority, needs=needs, job=job)


def _check_dependencies(items: tuple[RoadmapItem, ...]) -> None:
    """Every `needs` names a real item, and the graph is acyclic. Both RAISE.

    A CYCLE MUST NOT DEADLOCK SILENTLY: `candidates` never offers an item whose needs are unmet, so
    a cycle would simply mean those items are never scheduled -- no error, no log, a fleet that
    looks busy and a roadmap section that is never done. That is the starvation failure with a
    different cause, and it is caught here, once, at load.
    """
    known = {i.key for i in items}
    graph = {i.key: tuple(i.needs) for i in items}
    for item in items:
        for need in item.needs:
            if need not in known:
                msg = f'item {item.key!r} needs {need!r}, which is not in this roadmap'
                raise RoadmapError(msg)
    # Iterative DFS with an explicit colour map: an on-stack revisit is a cycle, and the stack names
    # it, because "there is a cycle somewhere" sends the reader through the whole file.
    colour: dict[str, int] = {}
    for start, needs in graph.items():
        if colour.get(start):
            continue
        stack = [(start, iter(needs))]
        colour[start] = 1
        path = [start]
        while stack:
            node, it = stack[-1]
            for nxt in it:
                if colour.get(nxt) == 1:
                    cycle = ' -> '.join([*path[path.index(nxt) :], nxt])
                    msg = f'dependency cycle: {cycle}'
                    raise RoadmapError(msg)
                if not colour.get(nxt):
                    colour[nxt] = 1
                    path.append(nxt)
                    stack.append((nxt, iter(graph[nxt])))
                    break
            else:
                colour[node] = 2
                stack.pop()
                path.pop()


def load(data: dict) -> Roadmap:
    """Validate ``data`` and derive the work items. Raises `RoadmapError` on anything unusable."""
    version = data.get('version')
    if version != SCHEMA_VERSION:
        msg = f'roadmap version {version!r} is not {SCHEMA_VERSION}; refusing to read it under v{SCHEMA_VERSION} rules'
        raise RoadmapError(msg)
    if unknown := sorted(set(data) - {'version', 'item'}):
        msg = f'unknown top-level key(s) {unknown}'
        raise RoadmapError(msg)
    items = tuple(_parse_item(raw) for raw in data.get('item', ()))
    seen: set[str] = set()
    for item in items:
        if item.key in seen:
            msg = f'duplicate item key {item.key!r} -- a key is the stable identity of one piece of work'
            raise RoadmapError(msg)
        seen.add(item.key)
    _check_dependencies(items)
    return Roadmap(items=items)


def loads(text: str) -> Roadmap:
    """Parse TOML ``text`` and `load` it."""
    return load(tomllib.loads(text))
