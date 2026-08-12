"""A mark pytest does not know about is a filter that silently selects nothing.

WHAT WAS FOUND, 2026-08-12. Thirty-one test files in this tree carried `pytestmark =
pytest.mark.unit` and pytest emitted `PytestUnknownMarkWarning` for every one of them. The mark was
never registered, so `-m unit` was never a usable selector; and 31 of the suite's 95 files carried
it, so even registered it would have named an arbitrary third of the suite rather than a partition.

THE MARK WAS DELETED RATHER THAN REGISTERED, and the reason is the second sentence above rather than
the first. Registering it would have made `-m unit` *work* -- which is worse than the warning, since
it would then select 31 files while reading as "the unit tests" and silently omit the other 64. The
whole default suite is offline and fast already; `live_forge` and `live_fabric` are the real tiers
and they are registered, exhaustive over what they name, and deselected by default.

THIS FILE IS WHY IT CANNOT COME BACK BY ACCIDENT. Deleting 31 lines is an event; the property is
that every mark this suite uses is one pyproject declares. That is checked here, so a new unregistered
mark reds instead of warning -- and a warning in a wall of warnings is a check nobody runs.
"""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

_TESTS = Path(__file__).parent
_PYPROJECT = _TESTS.parent / 'pyproject.toml'

#: Marks pytest itself defines. They need no registration and are not this file's business.
_BUILTIN = frozenset({'skip', 'skipif', 'xfail', 'parametrize', 'usefixtures', 'filterwarnings'})


def _declared_marks() -> set[str]:
    """The marks pyproject registers, by name -- the entries are `name: description`."""
    config = tomllib.loads(_PYPROJECT.read_text(encoding='utf-8'))
    entries = config['tool']['pytest']['ini_options']['markers']
    return {entry.split(':', 1)[0].strip() for entry in entries}


def _used_marks() -> dict[str, set[str]]:
    """``{mark: {files using it}}``, read from the SOURCE rather than from a collected run.

    AST AND NOT A COLLECTION HOOK: a mark on a file that fails to import would be invisible to a
    run, and the file that will not import is exactly the one worth being loud about.
    """
    found: dict[str, set[str]] = {}
    for path in sorted(_TESTS.glob('*.py')):
        tree = ast.parse(path.read_text(encoding='utf-8'))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute):
                continue
            owner = node.value
            if isinstance(owner, ast.Attribute) and owner.attr == 'mark' and isinstance(owner.value, ast.Name):
                if owner.value.id == 'pytest':
                    found.setdefault(node.attr, set()).add(path.name)
    return found


def test_every_mark_used_in_this_suite_is_registered():
    """The acceptance criterion. An unregistered mark warns; pytest never fails on it, so nothing
    stops one accumulating until `-m` quietly answers about the wrong set of tests."""
    unregistered = {
        mark: sorted(files) for mark, files in _used_marks().items() if mark not in _declared_marks() | _BUILTIN
    }
    assert not unregistered, (
        'these marks are used but not registered in pyproject.toml -- either register them or '
        f'delete them, but do not leave a selector that selects nothing: {unregistered}'
    )


def test_every_registered_mark_is_actually_USED():
    """THE CONVERSE, without which a registration outlives the tests it named and reads as coverage
    forever -- the dead-entry shape this repo already refuses for testkeys and for the layer census.

    `live_forge` and `live_fabric` are deselected by default and still counted here: this reads the
    SOURCE, so a tier being switched off does not make its registration look dead.
    """
    used = _used_marks()
    unused = sorted(mark for mark in _declared_marks() if mark not in used)
    assert not unused, f'pyproject registers marks nothing uses: {unused}'


def test_the_unit_mark_did_not_come_BACK():
    """It named a third of the suite while reading as a partition of it. Named here explicitly so
    that a reader who reaches for it finds the reason rather than re-deriving it -- and so that
    restoring it is a deliberate act with this file's docstring in front of it."""
    assert 'unit' not in _used_marks(), (
        'pytest.mark.unit is back. It was deleted, not registered: it covered 31 of 95 files, so '
        '`-m unit` would name an arbitrary third of the suite while reading as "the unit tests". '
        'The real tiers are live_forge and live_fabric.'
    )
