"""The clock is TERMINAL-BOUND, and that is a user directive rather than a nicety.

USER DIRECTIVE, restated 2026-08-12: **CI and every loop is started by a human, in a terminal.
Always. That is the design, not a gap.** 7x24 means a long-lived loop somebody STARTED and can SEE
-- never an unattended one.

WHAT WAS ACTUALLY WRONG, measured 2026-08-12: `Clock.run`'s docstring said "Ctrl-C, or closing the
terminal, is the stop" and **nothing made that true**. `grep -c lifetime src/agent_swarm/clock.py`
returned 0. motronics' `ci_loop` bound itself; the PACKAGE's own loop -- the one that spawns a fresh
child process every tick -- did not. So closing the window left the clock's ticks running, and a
closed window that stopped nothing is indistinguishable from a clock that was never started.

WHY THE LIE MATTERED MORE HERE THAN ELSEWHERE. The operator has exactly one stop gesture, and it is
"close the window". A loop whose docstring promises that gesture works, while the gesture does
nothing, does not merely fail to stop -- it teaches its operator that they HAVE stopped it. The
previously measured version of this failure was a Scheduled Task reporting itself present and
correct while its `LastRunTime` read 1932.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_swarm import clock, lifetime

pytestmark = pytest.mark.unit


@pytest.fixture
def a_clock(tmp_path: Path) -> clock.Clock:
    """A clock whose tick command exits immediately. `once=True` keeps the loop to one pass."""
    (tmp_path / 'policy.toml').write_text('', encoding='utf-8')
    return clock.Clock(
        tick_command=['python', '-c', 'pass'],
        policy_path=tmp_path / 'policy.toml',
        heartbeat_path=tmp_path / 'beat',
        maintenance_stamp=tmp_path / 'stamp',
    )


class _Out:
    def __init__(self) -> None:
        self.text = ''

    def write(self, s: str) -> None:
        self.text += s

    def flush(self) -> None:
        pass


def test_it_binds_BEFORE_the_first_tick(monkeypatch, a_clock):
    """Order is the assertion. Binding after the first spawn leaves that tick unbound forever --
    and the first tick is the one most likely to be running when somebody closes the window,
    because that is the moment they are still watching."""
    events: list[str] = []
    monkeypatch.setattr(
        lifetime, 'bind_children_to_this_process', lambda: events.append('bound') or lifetime.Binding('fake', 0)
    )
    monkeypatch.setattr(clock.Clock, 'run_once_through', lambda _s, _i: events.append('tick') or None)
    monkeypatch.setattr(clock.Clock, 'upkeep', lambda _s: None)

    assert a_clock.run(once=True, interval=0.01, out=_Out()) == 0
    assert events[:2] == ['bound', 'tick'], f'binding did not precede the first tick: {events}'


def test_it_SAYS_which_mechanism_bound_it(monkeypatch, a_clock):
    """`job_object` and `sighup` fail differently, and an operator reading a hang needs to know
    which one they have. A binding nobody can see is worth as much as one that did not happen."""
    monkeypatch.setattr(lifetime, 'bind_children_to_this_process', lambda: lifetime.Binding('job_object', 368))
    monkeypatch.setattr(clock.Clock, 'run_once_through', lambda _s, _i: None)
    monkeypatch.setattr(clock.Clock, 'upkeep', lambda _s: None)
    out = _Out()

    a_clock.run(once=True, interval=0.01, out=out)

    assert 'terminal-bound via job_object' in out.text
    assert 'closing this window' in out.text


def test_a_FAILURE_to_bind_stops_the_clock_rather_than_warning(monkeypatch, a_clock):
    """THE LOAD-BEARING ONE. A warning on an otherwise-normal start is the forbidden shape: the
    caller cannot tell it from a healthy clock, and what it would be hiding is a fleet nobody can
    stop by the only stop its operator has.

    So an unbindable box must not run ticks at all -- refusing is recoverable, an unstoppable
    fleet of spawned children is not.
    """

    def _cannot_bind():
        msg = 'no job object available'
        raise OSError(msg)

    monkeypatch.setattr(lifetime, 'bind_children_to_this_process', _cannot_bind)
    ticks: list[int] = []
    monkeypatch.setattr(clock.Clock, 'run_once_through', lambda _s, _i: ticks.append(1))

    with pytest.raises(OSError, match='job object'):
        a_clock.run(once=True, interval=0.01, out=_Out())
    assert ticks == [], 'a tick was spawned by a clock that could not bind its children'


def test_the_real_binding_works_outside_a_double(a_clock):
    """The control. Every test above passes against a `bind_children_to_this_process` that returns
    a plausible object and binds nothing -- which is exactly the failure being fixed. This one
    calls the REAL one and asserts it names a mechanism this platform actually has."""
    binding = lifetime.bind_children_to_this_process()
    assert binding.mechanism in {'job_object', 'sighup'}, f'unexpected mechanism {binding.mechanism!r}'
