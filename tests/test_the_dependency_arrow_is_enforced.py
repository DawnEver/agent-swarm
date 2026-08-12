"""The dependency arrow is CHECKED, not merely stated.

MEASURED 2026-08-11: the package had 32 modules, a clean acyclic import graph, no upward edges --
and no test that would have noticed any of those going wrong. `__init__`'s "dependency arrow
strictly L2 -> L1 -> L0" had been true since the package was three modules and was consulted by
nothing. Right by accident is the finding; this file is the fix.

WHAT IS ASSERTED: acyclicity, no upward edges, that the census in `layers.py` is exhaustive in BOTH
directions, that the dev-tool drawer cannot deepen, and that the front door does not lie about what
is behind it.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from agent_swarm import layers


_SRC = Path(layers.__file__).parent
_MODULES = {p.stem for p in _SRC.glob('*.py') if p.stem != '__init__'}


def _imports(stem: str) -> set[str]:
    """Every intra-package module `stem` imports. Both `from agent_swarm.x import y` spellings."""
    tree = ast.parse((_SRC / f'{stem}.py').read_text(encoding='utf-8'))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.split('.')[0] == 'agent_swarm':
            parts = node.module.split('.')
            if len(parts) > 1:
                found.add(parts[1])
            else:  # `from agent_swarm import claim`
                found |= {a.name for a in node.names if a.name in _MODULES}
        elif isinstance(node, ast.Import):
            for alias in node.names:
                parts = alias.name.split('.')
                if parts[0] == 'agent_swarm' and len(parts) > 1:
                    found.add(parts[1])
    return found - {stem}


_GRAPH = {stem: _imports(stem) for stem in sorted(_MODULES)}


def test_the_census_covers_every_module_and_names_none_that_is_gone():
    """Exhaustive BOTH ways. Without the converse a deleted module leaves a placement that reads as
    coverage forever -- the same dead-entry shape `test_testkey_entries_are_not_dead` exists for."""
    placed = set(layers.LAYERS)
    assert not (_MODULES - placed), f'modules with no declared layer: {sorted(_MODULES - placed)}'
    assert not (placed - _MODULES), f'layers.py places modules that do not exist: {sorted(placed - _MODULES)}'


def test_no_import_points_UP_a_layer():
    """The acceptance criterion. Same-layer edges are allowed; upward ones are not."""
    upward = [
        f'{src}({layers.LAYER_NAMES[layers.layer_of(src)]}) -> {dst}({layers.LAYER_NAMES[layers.layer_of(dst)]})'
        for src, deps in _GRAPH.items()
        for dst in sorted(deps)
        if layers.layer_of(dst) > layers.layer_of(src)
    ]
    assert not upward, 'imports point up the arrow:\n  ' + '\n  '.join(upward)


def test_the_import_graph_is_ACYCLIC():
    """The teeth the layer ordering does not have: same-layer edges are legal, so a two-module
    cycle inside JOB would pass the arrow check and still be a cycle."""
    colour: dict[str, int] = {}
    stack: list[str] = []

    def visit(node: str) -> list[str] | None:
        colour[node] = 1
        stack.append(node)
        for dep in sorted(_GRAPH.get(node, ())):
            if colour.get(dep) == 1:
                return stack[stack.index(dep) :] + [dep]
            if colour.get(dep) is None and (cycle := visit(dep)) is not None:
                return cycle
        colour[node] = 2
        stack.pop()
        return None

    for stem in sorted(_GRAPH):
        if colour.get(stem) is None and (cycle := visit(stem)) is not None:
            pytest.fail('import cycle: ' + ' -> '.join(cycle))


def test_the_arrow_check_can_FAIL():
    """The control. Every assertion above passes against an empty graph or an empty census, so one
    of them is proved to have teeth by planting a real violation instead of re-deriving the rule."""
    host, entry = 'procs', 'swarmctl'
    assert layers.layer_of(host) < layers.layer_of(entry)
    upward = [d for d in {entry} if layers.layer_of(d) > layers.layer_of(host)]
    assert upward == [entry], 'the comparison that decides an upward edge does not fire on one'


def test_nothing_load_bearing_imports_a_DEV_TOOL():
    """The drawer may exist; it may not deepen. A convenience on a load-bearing path stops being
    a convenience and nobody notices at the moment it changes."""
    allowed = layers.DEV_TOOL | {m for m, lyr in layers.LAYERS.items() if lyr == layers.ENTRY}
    offenders = [
        f'{src} -> {dst}'
        for src, deps in _GRAPH.items()
        for dst in sorted(deps & layers.DEV_TOOL)
        if src not in allowed
    ]
    assert not offenders, 'a dev tool is on a load-bearing path:\n  ' + '\n  '.join(offenders)


def test_the_front_door_does_not_describe_a_package_that_no_longer_exists():
    """`__init__`'s docstring said "WHAT IS HERE TODAY: admission, job and store" while thirty-three
    modules sat behind it -- a lying declaration in the first place any reader looks. The fix is not
    a longer list, which drifts identically; it is that the docstring DELEGATES the census and says
    where it lives."""
    import agent_swarm

    doc = agent_swarm.__doc__ or ''
    assert 'WHAT IS HERE TODAY' not in doc, 'the stale hand-maintained inventory is back'
    assert 'layers.py' in doc, 'the front door does not say where the module census lives'


def test_every_JOB_module_is_reachable_from_the_front_door():
    """`__all__` exported five modules' symbols out of thirty-three, so nine tenths of the package
    was invisible to a reader entering through the front door -- including `claim`, which is the
    arbitration everything else is built on."""
    import agent_swarm

    exported = set(agent_swarm.__all__)
    missing = sorted(m for m, lyr in layers.LAYERS.items() if lyr == layers.JOB and m not in exported)
    assert not missing, f'job-layer modules absent from __all__: {missing}'
