"""The kanban board is a computed VALUE. It must have no path by which it can write a work item.

NOTHING RENDERS IT TODAY -- no released Gitea has a Projects REST API (PR #38691, milestone 1.28.0,
unreleased; this host is 1.26.4). `board.py`'s docstring carries the evidence and the consequence.
These tests are therefore about the PROJECTION and its refusal to write, and none of them claims a
board is visible anywhere.

THE DIRECTIVE, and it is narrower than it sounds: "project 仅仅作为看板就行" -- the project is ONLY a
kanban view. So the board shows where work stands and decides nothing, and the reason that matters is
not tidiness. Work state has exactly one writer today: `ForgeStore`, arbitrating claims on
server-assigned comment ids. A board that could move a card and have the card move the ITEM would be
a SECOND writer of the same fact, racing the first over an API with no compare-and-swap. The claim
protocol's whole correctness argument -- sixteen racers, one winner, measured -- is an argument about
one writer, and it does not survive a second one arriving through the UI.

SO THE TEST HAS TO CATCH A PATH EXISTING, NOT OBSERVE THAT NONE DOES TODAY. "There is no write here"
is satisfied by any module that happens not to call a writer this week, and the next person adds
`forge.add_label(...)` to sync a moved card and every one of those tests still passes. Two things
close that:

* the projection TAKES NO FORGE. It is handed data and returns a value, so there is no object on
  which a write could be performed -- a structural refusal rather than a discipline;
* `_writers_reachable_from` reads the module's AST and rejects any reference to a mutating `Forge`
  method, with "mutating" DERIVED as "not in the read-only set" rather than hand-listed. A method
  added to the protocol tomorrow is therefore mutating by default, which is the safe direction: the
  failure mode of a hand-list is that the newest, least-reviewed method is the one missing from it.

WHY THE COLUMN IS NOT SIMPLY THE LABEL. Four of the five columns are label-derived, and IN PROGRESS
IS NOT: a claim is a comment, not a label, so nothing on the work item says "somebody is running
this". That is a real gap and it is handled by making `claimed` a REQUIRED argument rather than one
defaulting to empty. A default would render every running job as Pending -- unknown silently read as
none, on the one column that distinguishes a busy fleet from an idle one.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from agent_swarm import board
from agent_swarm.board import COLUMNS, Board, column_for, project
from agent_swarm.forge import WorkItem
from agent_swarm.forge_store import READY_LABEL, VERDICT_LABELS

#: Everything a `Forge` can do WITHOUT changing anything. Every other protocol method is treated as
#: a writer -- see the module docstring on why the list is drawn this way round.
READ_ONLY_FORGE_METHODS = frozenset({'list_work_items', 'work_item', 'comments', 'labels', 'state'})


def _forge_writers() -> frozenset[str]:
    """Mutating `Forge` methods, derived from the protocol itself.

    Derived rather than spelled, so a method added to `Forge` is covered by this file on the day it
    is added and without anyone remembering to come here.
    """
    from agent_swarm.forge import Forge

    return frozenset(n for n in Forge.__protocol_attrs__ if not n.startswith('_')) - READ_ONLY_FORGE_METHODS


def _writers_reachable_from(source: str) -> list[str]:
    """Every mutating-forge name this source so much as MENTIONS -- called, imported or referenced.

    MENTIONS, not "calls", deliberately. `getattr(forge, 'add_' + kind)` is a call this would miss if
    it looked for `ast.Call`; a name that is merely bound (`writer = forge.add_label`) is a write
    path with one more step. The check is cheap and the false-positive it risks -- a docstring
    naming a method -- does not arise, because docstrings are not names.
    """
    writers = _forge_writers()
    tree = ast.parse(source)
    found: list[str] = []
    for node in ast.walk(tree):
        names: tuple[str, ...] = ()
        if isinstance(node, ast.Attribute):
            names = (node.attr,)
        elif isinstance(node, ast.Name):
            names = (node.id,)
        elif isinstance(node, ast.alias):
            # BOTH HALVES. `from x import set_status as s` renames the path and not the capability,
            # so checking only the local name would let an alias launder every writer there is.
            names = (node.name, node.asname or node.name)
        found.extend(f'line {getattr(node, "lineno", 0)}: {n}' for n in dict.fromkeys(names) if n in writers)
    return found


# ------------------------------------------------------------------ the detector actually detects


def test_the_writer_set_is_not_empty():
    """The instrument first. An empty set would make every assertion below vacuously true, and a
    typo in the read-only list is exactly how it would become empty.
    """
    assert _forge_writers()
    assert 'add_label' in _forge_writers()
    assert 'close_work_item' in _forge_writers()
    assert 'list_work_items' not in _forge_writers()


def test_the_detector_catches_a_write_back():
    """The path somebody would actually add: a moved card syncing itself back onto the item."""
    assert _writers_reachable_from('def on_move(forge, n):\n    forge.add_label(n, "done")\n')


def test_the_detector_catches_a_write_bound_but_not_called():
    """A name bound now and called later is the same path with a step in it, and a check that only
    looked at `ast.Call` would report it clean.
    """
    assert _writers_reachable_from('def f(forge):\n    later = forge.close_work_item\n    return later\n')


def test_the_detector_catches_a_write_smuggled_in_by_import():
    """`from agent_swarm.forge_store import ForgeStore` is not the shape; a direct import of a
    writer's NAME is, and so is an alias of one.
    """
    assert _writers_reachable_from('from x import set_status as s\n')


def test_the_detector_passes_a_read_only_module():
    """The discriminating half. A detector that flagged everything would be trivially satisfied and
    would say nothing about this module.
    """
    assert not _writers_reachable_from('def f(items):\n    return [i.labels for i in items]\n')


# ------------------------------------------------------------------ and the board has no write path


def test_the_board_module_mentions_no_forge_writer():
    """THE ARCHITECTURE ASSERTION. Not "the board does not write today" -- the board module cannot
    name the things that write.
    """
    source = Path(board.__file__).read_text(encoding='utf-8')
    found = _writers_reachable_from(source)
    assert not found, 'the board can reach a work-item writer:\n  ' + '\n  '.join(found)


def test_the_board_module_imports_no_forge_and_no_store():
    """The structural half. A module holding a `Forge` has a write path whatever its current code
    says, and `ForgeStore` is the single writer this projection must not become a second of.
    """
    source = Path(board.__file__).read_text(encoding='utf-8')
    imported = {
        alias.name for node in ast.walk(ast.parse(source)) if isinstance(node, ast.ImportFrom) for alias in node.names
    }
    assert not imported & {'Forge', 'GiteaForge', 'GitHubForge', 'ForgeStore', 'StatusPublisher', 'default_forge'}


def test_project_takes_no_forge():
    """The refusal that makes the rest structural: with no forge in the signature there is no object
    to write through, so a write-back would have to start by CHANGING THIS SIGNATURE -- a visible
    edit rather than one more line in a function body.
    """
    import inspect

    params = inspect.signature(project).parameters
    assert 'forge' not in params
    assert not any('forge' in str(p.annotation).lower() for p in params.values())


def test_projecting_does_not_touch_a_forge_that_is_merely_nearby():
    """The behavioural end of the same claim, kept because the structural tests are about SOURCE and
    this one is about a RUN: a projection performed while a live forge exists leaves it untouched.
    """
    from agent_swarm.testing import RecordingForge

    forge = RecordingForge()
    number = forge.create_work_item(title='t', body='b', labels=[READY_LABEL])
    before = (dict(forge.item_labels), dict(forge.items), list(forge.retired))
    project(forge.list_work_items(state='open'), claimed=frozenset())
    assert (dict(forge.item_labels), dict(forge.items), list(forge.retired)) == before
    assert number in forge.items


# ------------------------------------------------------------------ the projection itself


def _item(number: int, *labels: str, state: str = 'open') -> WorkItem:
    return WorkItem(number=number, title=f'item {number}', state=state, labels=tuple(labels))


def test_a_ready_unclaimed_item_is_pending():
    board_ = project([_item(1, READY_LABEL)], claimed=frozenset())
    assert [c.number for c in board_.column('Pending')] == [1]


def test_a_claimed_item_is_in_progress():
    """The column no label carries. `claimed` comes from the caller, which already reads claims to
    schedule -- the projection does not go looking, because looking would be I/O and this is a view.
    """
    board_ = project([_item(1, READY_LABEL)], claimed=frozenset({1}))
    assert [c.number for c in board_.column('In Progress')] == [1]
    assert not board_.column('Pending')


@pytest.mark.parametrize('verdict', sorted(VERDICT_LABELS))
def test_a_verdict_label_puts_the_card_in_its_verdict_column(verdict: str):
    board_ = project([_item(1, READY_LABEL, VERDICT_LABELS[verdict], state='closed')], claimed=frozenset())
    assert [c.number for c in board_.column(verdict)] == [1]


def test_a_verdict_beats_a_stale_claim():
    """An answered job may still carry a claim comment -- `record_verdict` labels before the release,
    and a crash between the two leaves exactly this state. Showing it as In Progress would report a
    finished job as running forever.
    """
    board_ = project([_item(1, READY_LABEL, VERDICT_LABELS['PASS'])], claimed=frozenset({1}))
    assert [c.number for c in board_.column('PASS')] == [1]


def test_two_verdict_labels_resolve_to_the_UNSAFE_ONE_being_shown():
    """A STATE THE VENDOR CAN REACH AND OUR CODE CANNOT. `POST /labels` accepted twelve identical
    names from twelve racers on the measured deployment, and a duplicate definition attached under a
    higher id survived a strip -- an item carrying both PASS and FAIL at once, on record.

    The projection must not pick by list order, which would make the answer depend on whichever
    order the endpoint happened to return -- two calls on unchanged data disagreeing. FAIL wins over
    INCONCLUSIVE wins over PASS: a projection that can be wrong should be wrong in the direction
    that sends someone to look.
    """
    both = _item(1, READY_LABEL, VERDICT_LABELS['PASS'], VERDICT_LABELS['FAIL'])
    reversed_ = _item(2, READY_LABEL, VERDICT_LABELS['FAIL'], VERDICT_LABELS['PASS'])
    assert [c.number for c in project([both, reversed_], claimed=frozenset()).column('FAIL')] == [1, 2]


def test_an_item_without_the_handover_label_is_not_on_the_board_at_all():
    """An item without `swarm:ready` is NOT work, whoever created it -- the store says so and the
    board must agree. A human's bug report appearing in Pending would put it in a queue nothing
    services.
    """
    board_ = project([_item(1), _item(2, READY_LABEL)], claimed=frozenset())
    assert [c.number for c in board_.column('Pending')] == [2]
    assert [c.number for c in board_.cards] == [2]


@pytest.mark.parametrize('claimed', [frozenset(), frozenset({1})])
def test_an_UNKNOWN_label_does_not_make_a_card_vanish(claimed: frozenset[int]):
    """THE PROJECTION'S VERSION OF UNKNOWN-READ-AS-ZERO, pinned in the safe direction.

    `run:femm`, `priority:1`, a human's `wontfix` -- none is a verdict and none is the handover
    label. Written as a LOOKUP, `column_for` would answer `None` for these and the card would fall
    off the board entirely, which reads as "that job does not exist" and is indistinguishable from
    an empty queue. Written as a FALLTHROUGH, an unrecognised label is simply not evidence about the
    column, and the card lands on the strength of what IS known.

    Deliberately NOT symmetrical with the handover label: an item lacking `swarm:ready` IS dropped
    (see above). The distinction is between "not swarm work at all" and "swarm work carrying a label
    this module has no opinion about", and only the second may not cost a card.
    """
    board_ = project([_item(1, READY_LABEL, 'run:femm', 'priority:1', 'wontfix')], claimed=claimed)
    assert len(board_.cards) == 1
    assert board_.cards[0].column == ('In Progress' if claimed else 'Pending')


def test_column_for_is_TOTAL_and_never_declines_to_answer():
    """Asserted against the function rather than through the board, because it is a property of the
    return: there is no label set for which a column is not produced.
    """
    for labels in ([], ['whatever'], [READY_LABEL], ['a', 'b', 'c']):
        assert column_for(labels, claimed=False) in COLUMNS
        assert column_for(labels, claimed=True) in COLUMNS


def test_every_card_lands_in_a_declared_column():
    """No card may fall off the board. A projection that silently dropped an item would under-report
    the queue, which is the direction nobody notices.
    """
    items = [
        _item(1, READY_LABEL),
        _item(2, READY_LABEL),
        _item(3, READY_LABEL, VERDICT_LABELS['FAIL']),
        _item(4, READY_LABEL, 'run:femm'),
    ]
    board_ = project(items, claimed=frozenset({2}))
    assert {c.number for c in board_.cards} == {1, 2, 3, 4}
    assert all(c.column in COLUMNS for c in board_.cards)


def test_the_columns_are_ordered_as_work_flows():
    """The order IS the board. Pending before In Progress before the terminal columns, so a reader
    sees a pipeline rather than five buckets.
    """
    assert COLUMNS == ('Pending', 'In Progress', 'PASS', 'FAIL', 'INCONCLUSIVE')


def test_asking_for_a_column_that_does_not_exist_RAISES():
    """A typo returning an empty tuple reads as "nothing is pending", which is the same wrong answer
    an empty queue gives and is indistinguishable from it.
    """
    with pytest.raises(KeyError):
        project([], claimed=frozenset()).column('Done')


def test_claimed_is_required():
    """No default. An empty default would render every running job as Pending: unknown read as none,
    on the one column that distinguishes a busy fleet from an idle one.
    """
    with pytest.raises(TypeError):
        project([])  # type: ignore[call-arg]


def test_a_board_is_immutable():
    """A view that a caller can edit in place is a view a caller can be tempted to edit instead of
    the item -- the write-back path arriving as a mutable attribute rather than as a forge call.
    """
    board_ = project([_item(1, READY_LABEL)], claimed=frozenset())
    assert isinstance(board_, Board)
    with pytest.raises((AttributeError, TypeError)):
        board_.cards = ()  # type: ignore[misc]
