"""The five scheduling modules each say where they sit in the chain.

MEASURED 2026-08-11, during the architecture pass: the decomposition

    admission (may I?) -> allocator (which first?) -> loop (run one) -> tick (one pass) -> clock (pull)

is a good one -- each split has a reason that stands on its own, and only `clock`'s docstring gave
it. The defect is not any single file; it is that `loop`, `tick` and `clock` are three names a
reader WILL confuse, and no file admitted the other two existed. Each is locally clear and the set
is disorienting, which is the failure mode a per-file review cannot see.

WHY THIS IS A TEST AND NOT A PARAGRAPH SOMEWHERE. The same reason `layers.py` exists: an
orientation note nothing consults is true on the day it is written. Restating the whole chain in
five docstrings would be five copies that drift apart silently, each looking authoritative. So the
rule is the weakest one that cannot rot -- **every link names its neighbours** -- and it is checked
here. Rename a module or drop a stage and the files that pointed at it red.

WHAT THIS DOES NOT ASSERT, stated so the reader does not supply "everything": it does not check that
the prose is CORRECT, only that the cross-reference exists. A wrong sentence about `tick` inside
`loop` passes. This buys navigability, not accuracy.
"""

from __future__ import annotations

import pytest

from agent_swarm import admission, allocator, clock, loop, tick

pytestmark = pytest.mark.unit

#: module -> the neighbours its docstring must name. Not "all five in all five": a chain, so each
#: link points at the ones it actually touches. `loop` names both its caller and its inputs because
#: it is the one in the middle and the one people land in first.
NEIGHBOURS = {
    'admission': ('allocator',),
    'allocator': ('admission', 'loop'),
    'loop': ('allocator', 'tick'),
    'tick': ('loop', 'clock'),
    'clock': ('tick',),
}

_MODULES = {'admission': admission, 'allocator': allocator, 'loop': loop, 'tick': tick, 'clock': clock}


@pytest.mark.parametrize('name', sorted(NEIGHBOURS))
def test_each_link_names_its_neighbours(name):
    doc = _MODULES[name].__doc__ or ''
    missing = [neighbour for neighbour in NEIGHBOURS[name] if neighbour not in doc]
    assert not missing, f'{name}.py does not tell its reader about {missing}'


def test_the_table_covers_the_whole_chain_and_nothing_else():
    """A dead entry here would assert about a module that no longer exists, and a missing one would
    leave a stage silently unchecked -- the same both-directions rule the layer census follows."""
    assert set(NEIGHBOURS) == set(_MODULES)


def test_the_check_can_FAIL():
    """The control. Substring containment passes trivially against a docstring that happens to
    mention anything, so the discriminating case is asserted directly: a name that is NOT there."""
    assert 'a_stage_that_does_not_exist' not in (loop.__doc__ or '')
