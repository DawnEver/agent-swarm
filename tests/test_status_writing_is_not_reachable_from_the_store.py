"""Our own code must not let an agent identity mark a commit green.

THE PROPERTY, AND WHY IT CANNOT BE ENFORCED WHERE IT SHOULD BE. Branch protection rests entirely on
"the required status is green". If the identity that PRODUCED a commit can also mark it green, the
required-status-check is decoration. Gitea cannot stop that: it has no scope for commit status, so
writing one needs repository write, which any identity that pushes branches must already have.
GitHub can (an App may hold statuses:write without contents:write).

USER DECISION: ACCEPT IT AND LABEL IT HONESTLY. So this file is not a claim that the boundary is
enforced. It is the strongest form still available: the server cannot stop another identity, but a
test can stop OUR CODE from becoming one.

WHY `ForgeStore` IS THE THING GUARDED. It is what a runner holds -- the agent-side object, carrying
the agent credential. Every claim, comment, label and verdict goes through it. If `set_status` were
reachable from there, an agent would be publishing the check that authorises its own merge, using
our code and our credential, and nothing outside would look wrong: the status would be green, the
protection rule satisfied, the merge legitimate.

WHAT WOULD DEFEAT THIS TEST and is therefore stated rather than left to be discovered: a helper on
the store that forwards to `self.forge.set_status`, or a caller reaching `store.forge` and using it
directly. The first is caught here by scanning for the call; the second is caught by asserting the
store exposes no path a caller would naturally take. Neither can stop someone constructing their
own forge -- that is the part credential distribution carries, and it is labelled as such.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from agent_swarm import forge_store

pytestmark = pytest.mark.unit

_SOURCE = Path(inspect.getfile(forge_store))


def _calls_in(source: str) -> set[str]:
    """Every attribute called anywhere in the module, by attribute name."""
    tree = ast.parse(source)
    return {
        node.func.attr for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }


def test_the_store_never_writes_a_commit_status():
    """The whole file, by AST rather than by grep: a string search would be satisfied by a comment
    and defeated by `getattr(self.forge, 'set_' + 'status')`."""
    assert 'set_status' not in _calls_in(_SOURCE.read_text(encoding='utf-8')), (
        'ForgeStore calls set_status. That object carries the AGENT credential, so an agent would '
        'be publishing the check that authorises its own merge -- and nothing outside would look '
        'wrong. Route verdict publication through the verifier-role forge instead.'
    )


def test_the_store_exposes_no_status_method():
    """The forwarding-helper shape, caught by name as well as by call: a method called
    `publish_status` that happens to be unused today is a loaded gun for the next reader."""
    offenders = [name for name in dir(forge_store.ForgeStore) if 'status' in name.lower()]
    assert not offenders, f'ForgeStore exposes {offenders}; status writing belongs to the verifier'


def test_the_scan_would_actually_catch_it():
    """A guard that cannot fire is this repo's most-recorded defect. Planting the call proves the
    detector sees it, rather than proving the file happens to be clean."""
    planted = 'class X:\n    def go(self):\n        self.forge.set_status("sha", state="success")\n'
    assert 'set_status' in _calls_in(planted)


def test_the_scan_reads_the_real_module():
    """An empty or mis-resolved source would make the assertion above pass by finding nothing."""
    text = _SOURCE.read_text(encoding='utf-8')
    assert 'class ForgeStore' in text
    assert len(_calls_in(text)) > 20, 'the AST scan found almost no calls; it is not reading the store'


def test_the_forge_protocol_DOES_have_it():
    """The discriminating half. If `set_status` did not exist at all, every assertion above would
    pass for the wrong reason -- and this is exactly the state the file was written in, one commit
    before the method landed."""
    from agent_swarm.forge import Forge

    assert hasattr(Forge, 'set_status')
