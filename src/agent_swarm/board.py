"""The kanban board: work state PROJECTED into columns, and never the other way round.

THE DIRECTIVE IS NARROWER THAN "ADD A BOARD": the project is ONLY a kanban view (user directive,
"project 仅仅作为看板就行"). This module is the whole of that -- a pure function from work items to
columns -- and the interesting thing about it is what it deliberately cannot do.

WHY A ONE-WAY PROJECTION IS AN ARCHITECTURAL REQUIREMENT AND NOT A PREFERENCE
=============================================================================

Work state has exactly ONE writer: `ForgeStore`, arbitrating claims on server-assigned monotonic
comment ids. That protocol's correctness argument -- sixteen racers, one winner, measured on two
forges -- is an argument about there being one writer. It does not survive a second one.

A board that can move a card, and whose moved card moves the ITEM, is exactly that second writer,
and it arrives through the UI where none of this layer's arbitration exists. Gitea's issue API has no
compare-and-swap; the assignee field was measured NOT to be one. So the two writers would not race
loudly, they would race quietly, and the losing write would look like a card someone forgot to drag.

SO THE REFUSAL IS STRUCTURAL RATHER THAN DISCIPLINARY. This module takes no `Forge`, holds no
`Forge`, and imports neither the protocol nor its implementations nor the store. There is no object
here on which a write could be performed, so adding a write-back would have to begin by changing this
module's signature -- a visible edit rather than one more line inside a function.
`tests/test_the_board_is_a_one_way_projection.py` fails if any mutating forge method is so much as
NAMED in this file, with "mutating" derived from the protocol rather than hand-listed.

NOTHING RENDERS THIS BOARD TODAY. IT IS A VALUE, NOT A SCREEN.
==============================================================

**Read this before concluding a board exists somewhere.** `project` returns a `Board`, and no code
anywhere publishes it to a forge, a UI or a file. That is DELIBERATE and externally forced rather
than half-finished -- but a reader who assumed otherwise would be making this project's own named
mistake, taking a flag that exists for a runner that runs. The hole is NAMED here, which is the
difference between a decision and a defect.

WHY IT CANNOT BE PUBLISHED. **No RELEASED Gitea has a Projects REST API.** Measured and
source-verified 2026-08-10:

* this deployment is Gitea **1.26.4** (`GET /api/v1/version`, measured by the coordinator);
* `routers/api/v1/api.go` at tag v1.26.4 has no `/projects` route group at all -- its only two
  "project" matches are error strings in a permission helper -- and at v1.27.1 the file has ZERO
  matches. `templates/swagger/v1_json.tmpl` at v1.26.4 has no `/projects` path either; its
  `has_projects` / `projects_mode` keys are repo UNIT TOGGLES, not board endpoints. So
  `/api/v1/repos/{owner}/{repo}/projects` 404s against this host;
* the routes exist on `main` ONLY -- `addProjectRoutes`, landed by PR #38691 "feat(api): add project
  APIs" on 2026-08-08, **milestone 1.28.0, unreleased**. The earlier attempt PR #36008 stalled in
  December 2025, and tracking issue #36824 states the position outright: "Gitea has no REST API for
  repository project boards."

Publishing this board is therefore blocked on a Gitea UPGRADE, which is a human action on a shared
host and not something this package can route around.

AND THE PART THAT SURVIVES THE UPGRADE -- READ THIS BEFORE WRITING THE PUBLISHER
================================================================================

**"Map labels to columns and get the board for free" is REFUTED, and not by the version.** In
Gitea's model labels and columns are two INDEPENDENT relations: columns live in `project_column`
linked through `project_issue`, labels are the separate `issue_label` relation, and
`models/project/column.go` and `models/project/issue.go` contain no reference to labels at all.
Moving a card changes no label; applying a label moves no card. `services/issue/status.go` closes
the other half -- closing an issue touches no column, so an answered job's card stays where it was.
The only automatic placement is the project's DEFAULT column.

So even on 1.28 a board FOLLOWS nothing. A publisher must call the move endpoint explicitly for
every card whose column changed, which makes it a WRITER with everything that implies: its own
idempotence, its own failure handling, and above all it must move cards WITHOUT ever writing back to
the work item, or it becomes the second writer this module exists to prevent. That is a component
with a real design, not the free consequence the plan assumed.

WHY IN PROGRESS IS AN ARGUMENT AND NOT A LABEL
==============================================

Four columns are label-derived. The fifth is not: a claim is a COMMENT, so nothing on the work item
says "somebody is running this", and the labels a list endpoint returns cannot answer it. That is a
real gap, and the way it is handled is `claimed` being REQUIRED. A default of `frozenset()` would
render every running job as Pending -- unknown silently read as none, on the one column that
distinguishes a busy fleet from an idle one. The caller that schedules already reads claims and can
answer honestly; one that cannot must say so rather than have this module guess on its behalf.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from agent_swarm.forge_store import READY_LABEL, VERDICT_LABELS

#: The columns, IN THE ORDER WORK FLOWS. The order is the board: a reader must see a pipeline rather
#: than five buckets, and a terminal column must not sit before a live one.
COLUMNS = ('Pending', 'In Progress', 'PASS', 'FAIL', 'INCONCLUSIVE')

#: Which verdict is SHOWN when an item carries more than one verdict label.
#:
#: THAT STATE IS REACHABLE AND OUR CODE CANNOT PRODUCE IT: `POST /labels` accepted twelve identical
#: names from twelve concurrent racers on the measured deployment, and a duplicate definition
#: attached under a higher id survived a strip -- an item carrying both PASS and FAIL at once, on
#: record. Picking by list order would make the projected verdict depend on whichever order the
#: endpoint happened to return, so two calls on unchanged data could disagree.
#:
#: FAIL over INCONCLUSIVE over PASS: a projection that can be wrong should be wrong in the direction
#: that sends somebody to look.
_VERDICT_PRECEDENCE = ('FAIL', 'INCONCLUSIVE', 'PASS')


@dataclass(frozen=True, slots=True)
class Card:
    """One work item's computed column. A VALUE -- it carries no way to reach the item, and no
    claim that anything is rendering it; see the module docstring on why nothing does yet."""

    number: int
    title: str
    column: str


@dataclass(frozen=True, slots=True)
class Board:
    """The projection. Frozen, because a view a caller can edit is a view a caller is tempted to
    edit INSTEAD OF the item -- the write-back arriving as a mutable attribute rather than as a
    forge call.
    """

    cards: tuple[Card, ...]

    def column(self, name: str) -> tuple[Card, ...]:
        """The cards in `name`, in item order.

        Raises:
            KeyError: there is no such column. A typo returning an empty tuple would read as
                "nothing is pending", which is indistinguishable from an empty queue and is the
                wrong answer nobody checks.
        """
        if name not in COLUMNS:
            msg = f'no such column {name!r}; the board has {list(COLUMNS)}'
            raise KeyError(msg)
        return tuple(card for card in self.cards if card.column == name)


def column_for(labels: Iterable[str], *, claimed: bool) -> str:
    """Which column an item with these labels belongs in. PURE, and the whole decision lives here.

    PRECEDENCE, and each step is a state the system actually reaches:

    1. A VERDICT WINS over a claim. `record_verdict` labels before it releases, so a crash between
       the two leaves an answered job still carrying a claim comment; showing it as In Progress
       would report a finished job as running forever.
    2. A CLAIM wins over nothing.
    3. Otherwise the work is Pending.
    """
    names = list(labels)
    for word in _VERDICT_PRECEDENCE:
        if VERDICT_LABELS[word] in names:
            return word
    return 'In Progress' if claimed else 'Pending'


def project(items: Iterable[object], *, claimed: frozenset[int]) -> Board:
    """Work items -> a board. Reads nothing, writes nothing, and cannot.

    Args:
        items: whatever a listing already returned. Each needs `number`, `title` and `labels`; the
            type is not narrowed to `WorkItem` because narrowing it would be the one plausible
            reason to import from the forge module, and nothing here needs more than those three
            attributes.
        claimed: item numbers with a LIVE claim. Required -- see the module docstring. The caller
            that schedules already knows; this module must not guess on its behalf.

    ITEMS WITHOUT THE HANDOVER LABEL ARE NOT ON THE BOARD AT ALL. An item without `swarm:ready` is
    not work, whoever created it and whatever its title -- the store says exactly that, and a human's
    bug report showing up in Pending would put it in a queue nothing services.
    """
    cards = tuple(
        Card(
            number=item.number,  # type: ignore[attr-defined]
            title=item.title,  # type: ignore[attr-defined]
            column=column_for(item.labels, claimed=item.number in claimed),  # type: ignore[attr-defined]
        )
        for item in items
        if READY_LABEL in item.labels  # type: ignore[attr-defined]
    )
    return Board(cards=cards)
