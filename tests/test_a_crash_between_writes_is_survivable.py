"""`record_verdict` writes the COMMENT before the CLOSE, and the order is the whole safety property.

THE HAZARD. `record_verdict` is two server writes -- a verdict comment, then a close that carries
the final label set -- and a 7x24 fleet dies between them regularly: a killed runner, a reboot, a
dropped connection. Whether that kills the JOB depends entirely on which write landed first, and
BOTH ORDERS PASS EVERY HAPPY-PATH TEST, because after both writes the two states are identical.

* comment first (what the code does): the crash leaves the item OPEN and still carrying its
  handover label, so the next runner claims it and re-runs. Cost: one duplicate comment.
* close first: the crash leaves the item CLOSED, unclaimable, and carrying a verdict label with no
  verdict comment behind it. Nobody re-runs it and nobody is told. The job is lost SILENTLY, which
  is the failure direction this repo refuses.

WHY AN ORDER ASSERTION AND NOT ONLY A STATE ONE. The state after a crash is downstream of the
order, so a state test does catch a reversal -- but it catches it as "the item was closed", which
reads as a bug in the crash simulation. Asserting the call ORDER names the property directly, so a
future reader reordering those two lines for tidiness is told what they broke.

MEASURED WHILE WRITING THIS (and the reason this file was written twice). A first attempt validated
the guard by REORDERING the two lines and confirming it went red. It did. After `git checkout`
restored the source, two tests STAYED red -- and the source was byte-identical to HEAD. Cause: a
line reorder preserves the file SIZE, and CPython validates a cached `.pyc` against (mtime, size),
so the mutated bytecode outlived the revert and the suite was still measuring the mutation. The
instrument, not the code, was stale. Any mutation that only moves lines needs `__pycache__` cleared
before the revert is believed.
"""

from __future__ import annotations

import pytest

from agent_swarm.forge_store import ForgeStore, Role
from agent_swarm.job import TEST_RUN, Job
from agent_swarm.testing import RecordingForge

JOB = Job(id='j1', kind=TEST_RUN)


class _Crashing(RecordingForge):
    """A forge that dies on the Nth call to one method -- a process kill, not a server error.

    Subclassing the SHIPPED double rather than writing a stub: everything before the crash must
    behave exactly as it does in every other test, or the surviving state is a property of the stub.
    """

    def __init__(self, *, die_on: str) -> None:
        super().__init__()
        self.calls: list[str] = []
        self._die_on = die_on

    def _note(self, name: str) -> None:
        self.calls.append(name)
        if name == self._die_on:
            msg = f'process died after {name}'
            raise KeyboardInterrupt(msg)

    def add_comment(self, number: int, body: str) -> int:
        out = super().add_comment(number, body)
        self._note('add_comment')  # AFTER the write: the write landed, then we died.
        return out

    def close_work_item(self, number: int) -> None:
        super().close_work_item(number)
        self._note('close_work_item')


def _store(forge: RecordingForge) -> ForgeStore:
    return ForgeStore('ns', forge, role=Role.SUBMITTER)


# --------------------------------------------------------------------------- the order itself


def test_the_comment_is_written_BEFORE_the_close():
    """The discriminating assertion. Reversing the two lines in `record_verdict` fails only this."""
    forge = _Crashing(die_on='never')
    _store(forge).record_verdict(JOB, verdict='PASS', detail='d')
    assert forge.calls.index('add_comment') < forge.calls.index('close_work_item')


# --------------------------------------------------------------------------- crash after the comment


def test_a_crash_after_the_comment_leaves_the_job_OPEN():
    """Open is what makes it re-claimable. Closed would strand it."""
    forge = _Crashing(die_on='add_comment')
    with pytest.raises(KeyboardInterrupt):
        _store(forge).record_verdict(JOB, verdict='PASS', detail='d')
    assert [item.state for item in forge.items.values()] == ['open']


def test_a_crash_after_the_comment_leaves_NO_verdict_readable():
    """A verdict readable from a run that never finished is the unearned green."""
    forge = _Crashing(die_on='add_comment')
    store = _store(forge)
    with pytest.raises(KeyboardInterrupt):
        store.record_verdict(JOB, verdict='PASS', detail='d')
    assert store.verdict(JOB) is None


def test_the_retry_converges_on_the_verdict():
    """Recovery is the point; surviving a crash into a wedged state would not be surviving it."""
    forge = _Crashing(die_on='add_comment')
    store = _store(forge)
    with pytest.raises(KeyboardInterrupt):
        store.record_verdict(JOB, verdict='PASS', detail='d')
    forge._die_on = 'never'
    store.record_verdict(JOB, verdict='PASS', detail='d')
    assert store.verdict(JOB) == 'PASS'
    assert [item.state for item in forge.items.values()] == ['closed']


def test_the_cost_of_a_crash_is_ONE_duplicate_comment():
    """Named so it stays a KNOWN, bounded price rather than a discovery. It does not compound: a
    second crash costs a second comment, never a second work item.
    """
    forge = _Crashing(die_on='add_comment')
    store = _store(forge)
    with pytest.raises(KeyboardInterrupt):
        store.record_verdict(JOB, verdict='PASS', detail='d')
    forge._die_on = 'never'
    store.record_verdict(JOB, verdict='PASS', detail='d')
    assert len(forge.items) == 1
    number = next(iter(forge.items))
    assert len(forge.comments(number)) == 2


# --------------------------------------------------------------------------- the bad order, stated


def test_a_crash_after_the_close_would_strand_the_job():
    """The counterfactual, expressed against the REAL close: this is the state the current order
    makes unreachable, and it is unreachable only because the comment goes first.
    """
    forge = _Crashing(die_on='close_work_item')
    store = _store(forge)
    with pytest.raises(KeyboardInterrupt):
        store.record_verdict(JOB, verdict='PASS', detail='d')
    # Closed AND answered -- survivable only because the comment behind the verdict already landed.
    assert [item.state for item in forge.items.values()] == ['closed']
    assert store.verdict(JOB) == 'PASS'
    number = next(iter(forge.items))
    assert len(forge.comments(number)) == 1
