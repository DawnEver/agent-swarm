"""The clock's properties, independent of any project it might be driving.

PROVENANCE. Every mechanism here was extracted from motronics' `scripts/ci/ci_loop.py`, where it had
been running and measured for months. The numbers below are measurements, not invented examples.

WHAT THESE TESTS ARE FOR. Three of the four properties are about what happens WHILE a child process
is running, and none of them can be asserted from a snapshot: a heartbeat that only stamps between
spawns passes any test that checks the file afterwards. So the cadence tests spawn a real child that
outlives several beats, and count the beats by their SIDE EFFECT rather than by patching the thing
under test.
"""

from __future__ import annotations

import json
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from agent_swarm import clock
from agent_swarm.clock import (
    Clock,
    maintenance_due,
    maintenance_policy,
    poll_interval,
    run_maintenance,
    stamp_heartbeat,
    start_wake_listener,
    stop_wake_listener,
    wait_for_work,
)


def _free_port() -> int:
    """A port nothing is on, asked of the OS.

    NOT `port=0`. The API spends 0 on "poll only", deliberately -- a clock that cannot be woken is a
    supported configuration and needs a spelling -- so an ephemeral bind is unreachable through it,
    and a test wanting a real listener must name a real port. Racy in principle (the port is free at
    the moment it is released); the alternative is a hard-coded port, which is racy against every
    other suite on the box.
    """
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


def _sleeper(seconds: float) -> list[str]:
    """A child that outlives several beats and names nothing. A real process, not a double."""
    return [sys.executable, '-c', f'import time; time.sleep({seconds})']


def _appender(path: Path) -> list[str]:
    """A child whose only effect is one line in a file -- so beats can be COUNTED after the fact."""
    return [sys.executable, '-c', f'open({str(path)!r}, "a", encoding="utf-8").write("beat\\n")']


def _clock(tmp_path: Path, **overrides) -> Clock:
    kwargs = {
        'tick_command': _sleeper(0.05),
        'policy_path': tmp_path / 'policy.toml',
        'heartbeat_path': tmp_path / 'run' / 'heartbeat.json',
        'maintenance_stamp': tmp_path / 'run' / 'maintenance.json',
    }
    kwargs.update(overrides)
    return Clock(**kwargs)


class TestNothingIsDefaulted:
    """A DEFAULT THAT WORKS IS INVISIBLE, and the day it is wrong this clock drives someone else's
    project. This package already removed one such default (`default_forge`'s `DEFAULT_REPO`); a
    defaulted tick command is the same defect with a bigger blast radius.
    """

    @pytest.mark.parametrize('missing', ['tick_command', 'policy_path', 'heartbeat_path', 'maintenance_stamp'])
    def test_every_binding_is_REQUIRED(self, tmp_path, missing):
        kwargs = {
            'tick_command': ['x'],
            'policy_path': tmp_path / 'p.toml',
            'heartbeat_path': tmp_path / 'h.json',
            'maintenance_stamp': tmp_path / 'm.json',
        }
        del kwargs[missing]
        with pytest.raises(TypeError):
            Clock(**kwargs)  # type: ignore[arg-type]

    def test_the_module_holds_no_tick_to_fall_back_on(self):
        """A leftover module constant is the value the next caller reaches for -- which is why
        removing `DEFAULT_REPO` had to remove the DEFAULT and not just the name.
        """
        for gone in ('TICK', 'DEFAULT_TICK', 'POLICY_PATH', 'HEARTBEAT', 'MAINTENANCE_STAMP'):
            assert not hasattr(clock, gone), gone

    def test_an_EMPTY_tick_command_is_refused_at_CONSTRUCTION(self, tmp_path):
        """Where the caller still is. Inside the loop it would land in the spawn-error arm, which
        reports and KEEPS GOING by design -- so a misconfigured clock would poll forever, printing,
        and look exactly like one whose ticks are merely finding nothing to do.
        """
        with pytest.raises(ValueError, match='no default one-shot'):
            _clock(tmp_path, tick_command=[])


class TestTheHeartbeatIsStampedTHROUGHALongRun:
    """THE PROPERTY, and the reason it is not "the heartbeat file exists afterwards".

    A tick that claims a slow group owns the clock for up to 30 minutes. A clock that stamped only
    BETWEEN spawns would read as dead for that whole window, and a false "runner down" is how a
    reader learns to ignore the one line that means the fleet stopped.
    """

    def test_the_fleet_beat_fires_repeatedly_while_ONE_tick_is_still_running(self, tmp_path):
        """COUNTED BY SIDE EFFECT, with nothing patched. The beat child appends a line; the tick
        child sleeps through several cadences. More than one line can only mean the clock stamped
        while the tick was alive -- which is the claim.
        """
        beats = tmp_path / 'beats.txt'
        subject = _clock(
            tmp_path,
            tick_command=_sleeper(1.2),
            beat_command=_appender(beats),
            heartbeat_every_s=0.2,
        )
        subject.run(interval=60.0, once=True, wake_port=0, out=_Sink())
        assert beats.exists(), 'no beat ran at all while a 1.2 s tick was running'
        assert len(beats.read_text(encoding='utf-8').splitlines()) >= 3

    def test_the_local_stamp_advances_DURING_the_run_not_only_after_it(self, tmp_path):
        """The discriminating half of the same claim, on the LOCAL file. Read from another thread
        while the clock is mid-tick, so a final stamp cannot alibi a silent middle.
        """
        subject = _clock(tmp_path, tick_command=_sleeper(1.2), heartbeat_every_s=0.2)
        seen: list[float] = []
        watching = threading.Event()
        watching.set()

        def watch() -> None:
            while watching.is_set():
                try:
                    seen.append(json.loads(subject.heartbeat_path.read_text(encoding='utf-8'))['at'])
                except (OSError, ValueError, KeyError):
                    pass
                time.sleep(0.05)

        watcher = threading.Thread(target=watch, daemon=True)
        watcher.start()
        started = time.time()
        subject.run(interval=60.0, once=True, wake_port=0, out=_Sink())
        watching.clear()
        watcher.join(timeout=2.0)
        distinct = sorted(set(seen))
        assert len(distinct) >= 2, f'only {len(distinct)} distinct stamp(s) during a 1.2 s tick'
        assert distinct[0] - started < 1.0, 'the first stamp arrived only at the end of the run'

    def test_an_UNWRITABLE_heartbeat_does_not_kill_the_clock(self, tmp_path):
        """A missed beat reads as STALE, which is the safe direction. A clock that died because it
        could not write its own liveness file would be the joke version of this mechanism.
        """
        blocked = tmp_path / 'a-file'
        blocked.write_text('not a directory', encoding='utf-8')
        stamp_heartbeat(blocked / 'nested' / 'heartbeat.json')  # must not raise

    def test_a_stamp_records_the_time_and_the_pid(self, tmp_path):
        """The discriminating half: a swallow-everything write that wrote nothing would satisfy the
        test above forever.
        """
        path = tmp_path / 'h.json'
        stamp_heartbeat(path, now=1234.0, pid=99)
        assert json.loads(path.read_text(encoding='utf-8')) == {'at': 1234.0, 'pid': 99}


class TestAFreshProcessPerIteration:
    """The one-shot holds no state, and that is its value. An import would drag it into this one."""

    def test_the_tick_is_SPAWNED_and_actually_runs(self, tmp_path):
        marker = tmp_path / 'ran.txt'
        _clock(tmp_path, tick_command=_appender(marker)).run(interval=60.0, once=True, wake_port=0, out=_Sink())
        assert marker.read_text(encoding='utf-8').strip() == 'beat'

    def test_a_FAILING_tick_does_not_stop_the_clock(self, tmp_path):
        """A tick's non-zero exit is information for the NEXT iteration, never a reason to stop
        pulling. A clock that exited on a red tick would stop quietly -- the failure this design
        exists to make impossible.
        """
        failing = [sys.executable, '-c', 'raise SystemExit(3)']
        assert _clock(tmp_path, tick_command=failing).run(interval=60.0, once=True, wake_port=0, out=_Sink()) == 0

    def test_an_UNSPAWNABLE_tick_is_reported_and_survived(self, tmp_path):
        out = _Sink()
        assert (
            _clock(tmp_path, tick_command=['a-binary-that-is-not-installed']).run(
                interval=60.0, once=True, wake_port=0, out=out
            )
            == 0
        )
        assert 'could not start a tick' in out.text


class TestThePolicyIsREAD:
    def test_a_declared_cadence_is_used(self, tmp_path):
        policy = tmp_path / 'policy.toml'
        policy.write_text('[schedule]\npoll_seconds = 12\n', encoding='utf-8')
        assert poll_interval(policy) == 12.0

    def test_a_SILENT_policy_falls_back_to_the_documented_floor(self, tmp_path):
        policy = tmp_path / 'policy.toml'
        policy.write_text('[other]\nx = 1\n', encoding='utf-8')
        assert poll_interval(policy) == clock.DEFAULT_POLL_INTERVAL_S

    def test_a_MISSING_or_MALFORMED_policy_does_not_raise(self, tmp_path):
        """The cost of a wrong cadence is 45 s instead of 30. The cost of raising is a fleet that
        will not start because somebody left a trailing bracket in a config file.
        """
        broken = tmp_path / 'broken.toml'
        broken.write_text('[schedule\n', encoding='utf-8')
        assert poll_interval(tmp_path / 'absent.toml') == clock.DEFAULT_POLL_INTERVAL_S
        assert poll_interval(broken) == clock.DEFAULT_POLL_INTERVAL_S


class TestMaintenanceSilenceMeansDoNothing:
    """Deletion is irreversible, so the ABSENCE of a declaration must never be read as a default
    retention window. Unknown is not a policy.
    """

    def test_a_silent_policy_declares_nothing(self, tmp_path):
        policy = tmp_path / 'policy.toml'
        policy.write_text('[schedule]\npoll_seconds = 30\n', encoding='utf-8')
        assert maintenance_policy(policy) is None

    def test_a_zero_or_negative_window_declares_nothing(self, tmp_path):
        policy = tmp_path / 'policy.toml'
        policy.write_text('[maintenance]\nprune_closed_after_days = 0\n', encoding='utf-8')
        assert maintenance_policy(policy) is None

    def test_a_declared_window_carries_the_CONSUMERS_own_keys_through(self, tmp_path):
        """What a pass needs to be told is the consumer's vocabulary. A mapping filtered down to the
        keys this module happens to understand would silently drop the rest.
        """
        policy = tmp_path / 'policy.toml'
        policy.write_text(
            '[maintenance]\nprune_closed_after_days = 30\nevery_hours = 6\nrepo = "owner/name"\n', encoding='utf-8'
        )
        declared = maintenance_policy(policy)
        assert declared == {'prune_closed_after_days': 30, 'days': 30.0, 'every_hours': 6.0, 'repo': 'owner/name'}

    def test_an_UNREADABLE_stamp_means_a_pass_is_DUE(self, tmp_path):
        """The asymmetry: reading "never ran" when it did costs one idempotent sweep, while reading
        "recently ran" from a corrupt stamp costs an archive that grows forever with nothing saying
        so.
        """
        assert maintenance_due(tmp_path / 'absent.json', 24.0)

    def test_a_recent_stamp_means_it_is_not(self, tmp_path):
        stamp = tmp_path / 'm.json'
        stamp.write_text(json.dumps({'at': 1000.0}), encoding='utf-8')
        assert not maintenance_due(stamp, 24.0, now=1000.0 + 3600.0)
        assert maintenance_due(stamp, 24.0, now=1000.0 + 25 * 3600.0)


class TestTheMaintenanceCommandIsTheCALLERS:
    def test_a_REFUSAL_comes_back_as_the_reason_not_as_silence(self, tmp_path):
        """ "Nobody is pruning" is a fact about the fleet, and a caller that wants it cannot get it
        from a line of somebody's stdout. Deletion needs a credential only one box holds, so on every
        other box this refuses on every pass -- correctly, and it must stay visible.
        """
        why = run_maintenance(
            {'days': 30.0},
            build_command=lambda _p: 'this box holds no credential for deletion',
            stamp_path=tmp_path / 'm.json',
        )
        assert why == 'this box holds no credential for deletion'
        assert not (tmp_path / 'm.json').exists()

    def test_a_clock_with_no_declared_upkeep_REFUSES_rather_than_skipping(self, tmp_path):
        policy = tmp_path / 'policy.toml'
        policy.write_text('[maintenance]\nprune_closed_after_days = 30\n', encoding='utf-8')
        assert 'no maintenance command was declared' in (_clock(tmp_path, policy_path=policy).upkeep() or '')

    def test_a_SILENT_policy_produces_no_reason_at_all(self, tmp_path):
        """Silence is not a refusal to report: a clock whose consumer declared no retention runs no
        pass and says nothing, which is different from one that tried and could not.
        """
        policy = tmp_path / 'policy.toml'
        policy.write_text('[schedule]\npoll_seconds = 5\n', encoding='utf-8')
        assert _clock(tmp_path, policy_path=policy).upkeep() is None

    def test_a_successful_pass_is_STAMPED_and_a_failing_one_is_NOT(self, tmp_path):
        """Stamping a failed pass would silence the retry that fixes it."""
        stamp = tmp_path / 'm.json'
        assert run_maintenance({}, build_command=lambda _p: _sleeper(0.0), stamp_path=stamp) is None
        assert json.loads(stamp.read_text(encoding='utf-8'))['at'] > 0
        stamp.unlink()
        failing = [sys.executable, '-c', 'import sys; sys.stderr.write("upstream said no\\n"); raise SystemExit(2)']
        assert run_maintenance({}, build_command=lambda _p: failing, stamp_path=stamp) == 'upstream said no'
        assert not stamp.exists()

    def test_the_policy_reaches_the_BUILDER_unchanged(self, tmp_path):
        seen: list[object] = []
        run_maintenance(
            {'days': 30.0, 'repo': 'owner/name'},
            build_command=lambda p: seen.append(dict(p)) or 'stop here',
            stamp_path=tmp_path / 'm.json',
        )
        assert seen == [{'days': 30.0, 'repo': 'owner/name'}]


class TestTheWakeIsAnAcceleratorOverAFloor:
    """ "The webhook did not arrive" and "there is no work" are the SAME OBSERVATION from here --
    nothing happened. A design whose failure mode is indistinguishable from its idle state cannot be
    monitored, so the poll stays and the wake only shortens it.
    """

    def test_the_POLL_fires_when_nothing_wakes_it(self):
        started = time.time()
        assert wait_for_work(threading.Event(), 0.2) is False
        assert time.time() - started >= 0.15

    def test_a_wake_returns_EARLY_and_says_so(self):
        wake = threading.Event()
        wake.set()
        started = time.time()
        assert wait_for_work(wake, 30.0) is True
        assert time.time() - started < 1.0

    def test_the_event_is_CLEARED_on_the_way_out(self):
        """Or the first webhook of the day turns the loop into a busy spin, forever."""
        wake = threading.Event()
        wake.set()
        wait_for_work(wake, 30.0)
        assert not wake.is_set()

    def test_a_POST_wakes_the_clock_and_carries_NO_work(self):
        """The endpoint's only authority is "look sooner". A handler that believed the request about
        WHICH work to run would be a second, unauthenticated scheduler.
        """
        wake = threading.Event()
        server = start_wake_listener(wake, host='127.0.0.1', port=_free_port(), out=_Sink())
        assert server is not None
        try:
            port = server.server_address[1]
            request = urllib.request.Request(
                f'http://127.0.0.1:{port}{clock.WAKE_PATH}', data=b'{"run": "everything"}', method='POST'
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                assert response.status == 204
            assert wake.wait(timeout=5)
        finally:
            stop_wake_listener(server)

    def test_a_GET_is_REFUSED(self):
        """A browser probe, a health check and a link preview are not reasons to spawn a tick."""
        wake = threading.Event()
        server = start_wake_listener(wake, host='127.0.0.1', port=_free_port(), out=_Sink())
        assert server is not None
        try:
            port = server.server_address[1]
            with pytest.raises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(f'http://127.0.0.1:{port}{clock.WAKE_PATH}', timeout=5)
            assert raised.value.code == 405
            assert not wake.is_set()
        finally:
            stop_wake_listener(server)

    def test_the_listener_thread_is_a_DAEMON(self):
        """The property that keeps this from becoming the background runner that was deleted at the
        source: closing the terminal must take the listener with it.
        """
        wake = threading.Event()
        server = start_wake_listener(wake, host='127.0.0.1', port=_free_port(), out=_Sink())
        assert server is not None
        try:
            serving = [t for t in threading.enumerate() if t.name == clock.WAKE_THREAD_NAME]
            assert serving and all(t.daemon for t in serving)
            assert server.daemon_threads
        finally:
            stop_wake_listener(server)

    def test_a_port_of_zero_or_below_means_POLL_ONLY(self):
        assert start_wake_listener(threading.Event(), port=-1, out=_Sink()) is None

    def test_a_port_that_cannot_be_bound_DEGRADES_to_the_floor(self):
        """Dying over a convenience socket would trade 45 s of latency for a stopped fleet. It says
        so, and returns None.
        """
        wake = threading.Event()
        first = start_wake_listener(wake, host='127.0.0.1', port=_free_port(), out=_Sink())
        assert first is not None
        out = _Sink()
        try:
            second = start_wake_listener(threading.Event(), host='127.0.0.1', port=first.server_address[1], out=out)
            assert second is None
            assert 'polling only' in out.text
        finally:
            stop_wake_listener(first)

    def test_the_listener_is_NOT_reuse_addressable(self):
        """Found by a test, not reasoned out. `HTTPServer` sets `allow_reuse_address = 1`, and on
        Windows that lets a SECOND process bind a port a first is actively listening on: two clocks
        both "have" a listener while only one ever receives a wake, and the other falls back to its
        poll without saying so.
        """
        assert clock._WakeServer.allow_reuse_address is False

    def test_stopping_a_listener_that_never_started_is_a_no_op(self):
        stop_wake_listener(None)


class _Sink:
    """A stdout that a test can read back. The clock's output IS its status line, so it is asserted
    on rather than discarded -- a guard reporting into a discarded stdout is indistinguishable from
    no guard.
    """

    def __init__(self) -> None:
        self.text = ''

    def write(self, chunk: str) -> None:
        self.text += chunk

    def flush(self) -> None:
        pass
