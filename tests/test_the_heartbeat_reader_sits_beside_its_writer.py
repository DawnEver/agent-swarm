"""`stamp_heartbeat` writes it and `read_heartbeat` decodes it, and until now only one of them was here.

THE DEFECT THIS CLOSES. `stamp_heartbeat` decides the file's shape -- the `at` key, the `pid` key,
JSON -- and the function that read it back lived in another repository, written against that shape
from memory. Nothing connected them: renaming `at` here would have left a consumer reporting DEAD
about a clock beating perfectly well, with the writer's tests and the reader's tests both green.
That is the declaration-that-lies shape at a repository boundary, where it is hardest to see.

THE ROUND TRIP IS THEREFORE THE POINT OF THIS FILE. Every test below stamps with the real writer and
reads with the real reader; none of them hand-writes the JSON, because a test that did would be
pinning the reader against a second copy of the format rather than against the writer.

WHAT `read_heartbeat` MUST NOT COLLAPSE. DEAD means no readable stamp -- nobody started the clock on
this box, so the action is to start it. STALE means it started and stopped, so the action is to find
out why. One word for both sends every reader down the wrong action half the time.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_swarm import clock

pytestmark = pytest.mark.unit


@pytest.fixture
def beat(tmp_path: Path) -> Path:
    return tmp_path / 'beat.json'


def test_a_fresh_stamp_reads_back_ALIVE(beat: Path) -> None:
    """The round trip, and the control for every assertion below."""
    clock.stamp_heartbeat(beat, now=1000.0, pid=7)
    state, detail = clock.read_heartbeat(beat, now=1010.0, poll_seconds=45.0)
    assert state == 'ALIVE'
    assert '7' in detail, f'the pid the writer stored did not survive the read: {detail}'


def test_no_stamp_at_all_is_DEAD(tmp_path: Path) -> None:
    """The state of any box where nobody started the clock -- which is the default one."""
    state, detail = clock.read_heartbeat(tmp_path / 'absent.json', now=1000.0, poll_seconds=45.0)
    assert state == 'DEAD'
    assert 'never' in detail.lower(), detail


def test_an_UNREADABLE_stamp_is_DEAD_and_not_a_traceback(beat: Path) -> None:
    """A truncated write must not raise over whatever the reader actually came to see."""
    beat.write_text('{not json', encoding='utf-8')
    assert clock.read_heartbeat(beat, now=1000.0, poll_seconds=45.0)[0] == 'DEAD'


def test_a_stamp_MISSING_ITS_TIME_is_DEAD_rather_than_infinitely_fresh(beat: Path) -> None:
    """Well-formed JSON without `at` is the shape a format change would actually produce, and the
    dangerous reading is not a crash -- it is treating an absent time as now.
    """
    beat.write_text('{"pid": 7}', encoding='utf-8')
    assert clock.read_heartbeat(beat, now=1000.0, poll_seconds=45.0)[0] == 'DEAD'


def test_an_old_stamp_is_STALE_and_STALE_is_not_DEAD(beat: Path) -> None:
    """A crashed clock leaves its last beat behind, so absence alone would never catch it."""
    clock.stamp_heartbeat(beat, now=1000.0, pid=7)
    state, detail = clock.read_heartbeat(beat, now=1000.0 + 45.0 * 100, poll_seconds=45.0)
    assert state == 'STALE'
    assert '45' in detail, detail


def test_the_threshold_FOLLOWS_the_poll_interval(beat: Path) -> None:
    """THE DISCRIMINATING ASSERTION. The SAME age is STALE at a fast poll and ALIVE at a slow one,
    which is only true if the limit is a multiple of the interval. A constant that merely happened
    to sit between the two would pass every other test in this file.
    """
    clock.stamp_heartbeat(beat, now=0.0, pid=7)
    age = 600.0
    assert clock.read_heartbeat(beat, now=age, poll_seconds=10.0)[0] == 'STALE'
    assert clock.read_heartbeat(beat, now=age, poll_seconds=3600.0)[0] == 'ALIVE'


def test_a_clock_inside_a_long_tick_is_not_called_dead_by_WIDENING_the_threshold(beat: Path) -> None:
    """`Clock.run_once_through` stamps AROUND the spawn, which is what keeps a 30-minute tick alive.

    Pinned from the other side on purpose: a 30-minute-old beat at a 45 s poll IS late, and must
    read that way. If this ever passes as ALIVE, someone repaired a long tick by loosening the
    reader instead of by stamping through the spawn -- which silences the real signal too.
    """
    clock.stamp_heartbeat(beat, now=0.0, pid=7)
    assert clock.read_heartbeat(beat, now=1800.0, poll_seconds=45.0)[0] != 'ALIVE'


def test_the_missed_beat_count_is_an_ARGUMENT_and_not_only_a_constant(beat: Path) -> None:
    """A consumer with a different tolerance must not have to re-implement the reader to get it."""
    clock.stamp_heartbeat(beat, now=0.0, pid=7)
    assert clock.read_heartbeat(beat, now=100.0, poll_seconds=10.0, missed_beats=2)[0] == 'STALE'
    assert clock.read_heartbeat(beat, now=100.0, poll_seconds=10.0, missed_beats=50)[0] == 'ALIVE'


def test_every_reading_is_one_the_module_DECLARES(beat: Path) -> None:
    """`HEARTBEAT_STATES` exists so a consumer comparing against a literal breaks HERE if it drifts."""
    clock.stamp_heartbeat(beat, now=0.0, pid=7)
    seen = {
        clock.read_heartbeat(beat, now=1.0, poll_seconds=45.0)[0],
        clock.read_heartbeat(beat, now=99999.0, poll_seconds=45.0)[0],
        clock.read_heartbeat(beat.with_name('gone.json'), now=1.0, poll_seconds=45.0)[0],
    }
    assert seen == set(clock.HEARTBEAT_STATES)
