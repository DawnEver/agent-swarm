"""A runner's identity must be UNIQUE and STABLE, and both halves were incidents.

PROVENANCE. Extracted from motronics' `scripts/ci/ci_tick.py`. The bare hostname was not unique on
that fleet, and the two things keyed on identity -- heartbeat pruning and claim release -- both fail
SILENTLY when it collides: mutual heartbeat erasure (each box reads as dead while both are alive)
and duplicate execution (each treats the other's live claim as its own abandoned one).

THESE TESTS RUN AGAINST THE REAL MACHINE, deliberately. A double for `machine_uuid` would be better
behaved than every platform is -- it would always answer -- and "this platform will not say" is
precisely the branch the fallback exists for and the one that was missing on macOS. So the
properties asserted are the ones that must hold on ANY box: determinism, sensitivity to the
argument, and the shape of the id.
"""

from __future__ import annotations

import platform
from pathlib import Path

import pytest

from agent_swarm import identity
from agent_swarm.identity import SALT_LENGTH, machine_uuid, runner_id, runner_salt

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------- stable, never random


def test_the_salt_is_the_same_twice():
    """THE PROPERTY A RANDOM SALT WOULD BREAK, and it breaks worse than the collision it fixes:
    every tick becomes a new runner, the liveness count grows without bound, and the "delete every
    stamp that is not mine" prune never collects any of them."""
    root = Path('/some/checkout')
    assert runner_salt(root) == runner_salt(root)


def test_the_id_is_the_same_twice():
    assert runner_id(Path('/some/checkout')) == runner_id(Path('/some/checkout'))


def test_the_salt_is_short_and_hex():
    """It goes in an operator-facing string, so its length is part of the design rather than an
    artefact of which hash was used."""
    salt = runner_salt(Path('/some/checkout'))
    assert len(salt) == SALT_LENGTH
    assert all(c in '0123456789abcdef' for c in salt)


# --------------------------------------------------------------------------- unique


def test_the_hostname_is_in_front():
    """Operator-facing. A bare hash would make every liveness line unreadable, which is how a
    correct identity scheme gets replaced by a convenient one."""
    assert runner_id(Path('/x')).startswith(platform.node() + '-')


def test_the_id_is_the_hostname_PLUS_the_salt():
    """The discriminating half of the test above: an implementation that returned the hostname
    alone -- the exact defect this module was written for -- would satisfy it."""
    ident = runner_id(Path('/x'))
    assert ident != platform.node()
    assert ident.endswith(runner_salt(Path('/x')))


def test_two_checkouts_are_two_runners_when_the_machine_will_not_say(monkeypatch):
    """THE FALLBACK'S WHOLE JOB. It is reached only when no platform names the machine, and it must
    still discriminate -- otherwise two fleets run off one box under one identity, which is the
    collision this module exists to prevent arriving through its own door."""
    monkeypatch.setattr(identity, 'machine_uuid', lambda: None)
    assert runner_salt(Path('/checkout/a')) != runner_salt(Path('/checkout/b'))


def test_a_machine_that_NAMES_itself_ignores_the_checkout(monkeypatch):
    """The other side of the same rule, and it is what makes the id a fact about the MACHINE: when
    the platform answers, the path is not part of the identity at all."""
    monkeypatch.setattr(identity, 'machine_uuid', lambda: 'a-stable-machine-id')
    assert runner_salt(Path('/checkout/a')) == runner_salt(Path('/checkout/b'))


def test_two_machines_are_two_salts(monkeypatch):
    """The property the whole module exists for, exercised at the only seam a test can reach."""
    monkeypatch.setattr(identity, 'machine_uuid', lambda: 'machine-one')
    one = runner_salt(Path('/x'))
    monkeypatch.setattr(identity, 'machine_uuid', lambda: 'machine-two')
    assert runner_salt(Path('/x')) != one


# --------------------------------------------------------------------------- the project fact


def test_the_salt_will_not_guess_a_root():
    """A default root would silently merge two checkouts on one unidentifiable box into one runner
    -- the failure this module exists to prevent, wearing a convenience."""
    with pytest.raises(TypeError):
        runner_salt()  # type: ignore[call-arg]


def test_the_id_will_not_guess_a_root():
    with pytest.raises(TypeError):
        runner_id()  # type: ignore[call-arg]


# --------------------------------------------------------------------------- the platform probe


def test_the_machine_uuid_is_a_string_or_None():
    """UNKNOWN MUST BE REPRESENTABLE. A probe that raised on an unsupported platform would take the
    whole tick with it, and the fallback below it would never be reached."""
    answer = machine_uuid()
    assert answer is None or (isinstance(answer, str) and answer)


def test_the_machine_uuid_does_not_change_between_calls():
    """It is READ, never generated. A generated one would be unique and would make every reboot --
    or here, every call -- a new fleet member."""
    assert machine_uuid() == machine_uuid()
