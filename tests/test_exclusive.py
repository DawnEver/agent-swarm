"""The machine lock: it must exclude, it must name the holder, and it must not evict the living.

WHY EACH TEST EXISTS. The lock replaces a convention that was bypassable, so the properties worth
testing are the ones whose absence would make it *look* like it works:

* excluding a second holder is the point;
* naming the holder is what makes people wait instead of deleting the lock file;
* reclaiming a DEAD holder is what stops a crash from wedging the machine forever;
* **not** reclaiming a LIVE holder is the one that cannot be verified by using the thing -- a
  timestamp-based reclaim passes every "does it lock?" test and silently recreates the concurrency
  the lock exists to prevent, 25 minutes into a heavy gate.

The last one is why `_pid_alive` is exercised through real processes rather than fabricated pids.

PROVENANCE. Every property here was extracted with the module from motronics' `scripts/gate/`,
where it had been guarding real verdicts; the dated incidents in the docstrings are measurements,
not invented examples. What is NEW is the namespace: the module arrived with one project's name
compiled into its lock directory, and the tests that pin its removal have no motronics ancestor.

THE LIBRARY MUST BE TESTABLE WITHOUT ITS CALLERS. The consumer's own suite cannot vouch for this
layer, because it only ever exercises the paths its scheduler happens to take -- a second namespace
on one box is a case that consumer will never run and this package promises to handle.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import pytest

from agent_swarm import (
    WHOLE_BOX,
    LockBusy,
    exclusive,
    exclusive_lock,
    lock_dir,
    lock_path_for_class,
    read_owner,
)


class TestExclusion:
    """The point: one holder at a time, and the refusal says who."""

    def test_a_second_acquisition_is_refused_while_the_first_is_held(self, tmp_path):
        lock = tmp_path / 'expensive.lock'
        with exclusive_lock(lock, 'gate:fast'), pytest.raises(LockBusy), exclusive_lock(lock, 'gate:heavy'):
            pytest.fail('the second holder must not get in')

    def test_the_refusal_NAMES_the_holder(self, tmp_path):
        """A refusal with no named owner is the message that gets worked around.

        It is also the one that gets LOST: refusals believed missing had in fact been issued, into a
        discarded stdout. Naming the holder, its pid and its start time is what makes the message
        worth routing somewhere a human reads.
        """
        lock = tmp_path / 'expensive.lock'
        with (
            exclusive_lock(lock, 'gate:heavy tree=9b9235204607'),
            pytest.raises(LockBusy) as excinfo,
            exclusive_lock(lock, 'gate:fast'),
        ):
            pass

        message = str(excinfo.value)
        assert 'gate:heavy tree=9b9235204607' in message, message
        assert str(os.getpid()) in message, message
        assert excinfo.value.owner is not None and excinfo.value.owner.pid == os.getpid()

    def test_two_labels_share_ONE_lock(self, tmp_path):
        """Every expensive label contends for the same machine.

        Pinned because a per-label lock is the plausible-looking design that fails exactly the way
        the original defect failed -- two expensive jobs running at once, each certain it holds
        "its" lock.
        """
        lock = tmp_path / 'expensive.lock'
        with exclusive_lock(lock, 'gate:femm'), pytest.raises(LockBusy), exclusive_lock(lock, 'gate:slow'):
            pass

    def test_the_lock_is_released_on_exit(self, tmp_path):
        lock = tmp_path / 'expensive.lock'
        with exclusive_lock(lock, 'gate:fast'):
            pass
        assert not lock.exists()
        with exclusive_lock(lock, 'gate:heavy'):
            pass  # acquiring again must simply work

    def test_the_lock_is_released_when_the_body_RAISES(self, tmp_path):
        """A crashing job must not wedge the machine -- the common case of the stale-lock problem."""
        lock = tmp_path / 'expensive.lock'
        with pytest.raises(ValueError, match='boom'), exclusive_lock(lock, 'gate:fast'):
            msg = 'boom'
            raise ValueError(msg)
        assert not lock.exists()

    def test_waiting_gives_up_after_its_deadline_rather_than_hanging(self, tmp_path):
        """`wait_seconds` is a bound, not a hope: a blocked queue is itself a resource leak.

        ONLY THE LOWER BOUND IS ASSERTED. An upper bound produced a FALSE RED on 2026-08-08 on a box
        running a 16-worker gate -- it measured the MACHINE, not the code -- and discriminated almost
        nothing, since `pytest.raises(LockBusy)` already proves the wait ended. A false red in the
        lock's own test is expensive out of proportion to its size: it is the check people consult
        when they suspect the lock, so it must not cry wolf.
        """
        lock = tmp_path / 'expensive.lock'
        with exclusive_lock(lock, 'gate:heavy'):
            started = time.monotonic()
            with pytest.raises(LockBusy), exclusive_lock(lock, 'gate:fast', wait_seconds=0.5):
                pass
            elapsed = time.monotonic() - started

        assert elapsed >= 0.4, f'returned after {elapsed:.2f}s -- a 0.5s deadline was not waited out'


class TestReclaim:
    """A dead holder frees the lock; a live one never does, however old the file claims to be."""

    def test_a_lock_from_a_DEAD_process_is_reclaimed(self, tmp_path):
        """A pid that has exited holds nothing. Uses a REAL exited process, not a made-up number.

        A fabricated pid would test the arithmetic and not the liveness probe: on Windows an
        arbitrary integer is usually not a process, so the test would pass even if `_pid_alive` were
        a stub returning False.
        """
        dead = subprocess.Popen([sys.executable, '-c', 'pass'])
        dead.wait(timeout=60)  # `python -c pass`; a minute means something is very wrong
        lock = tmp_path / 'expensive.lock'
        lock.write_text(
            json.dumps({'pid': dead.pid, 'label': 'gate:fast', 'host': 'h', 'since': 't'}), encoding='utf-8'
        )

        assert read_owner(lock) is None, 'a dead holder must read as free'
        with exclusive_lock(lock, 'gate:heavy') as owner:
            assert owner.pid == os.getpid()

    def test_a_LIVE_holder_is_NOT_reclaimed_however_old_the_lock_says_it_is(self, tmp_path):
        """THE ONE A TIMESTAMP RECLAIM WOULD FAIL.

        A heavy gate runs 25+ minutes and the unbounded groups run longer, so any timeout short
        enough to clear a crashed holder also evicts a healthy one -- and the eviction recreates
        exactly the concurrency this lock prevents, while still looking like the lock is working.
        The `since` here is deliberately ancient; liveness must win over age.
        """
        live = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
        try:
            lock = tmp_path / 'expensive.lock'
            lock.write_text(
                json.dumps({'pid': live.pid, 'label': 'gate:heavy', 'host': 'h', 'since': '1999-01-01T00:00:00+00:00'}),
                encoding='utf-8',
            )

            owner = read_owner(lock)
            assert owner is not None and owner.pid == live.pid, 'a live holder must never read as free'
            with pytest.raises(LockBusy), exclusive_lock(lock, 'gate:fast'):
                pytest.fail('a live 27-year-old lock is still held')
        finally:
            live.kill()
            live.wait(timeout=60)  # already killed above; the wait only reaps it

    def test_a_corrupt_lock_file_does_not_wedge_the_machine(self, tmp_path):
        """Unparseable means "nobody identifiable holds it" -- reclaim, do not crash the caller."""
        lock = tmp_path / 'expensive.lock'
        lock.write_text('not json at all', encoding='utf-8')

        assert read_owner(lock) is None
        with exclusive_lock(lock, 'gate:fast') as owner:
            assert owner.pid == os.getpid()


class TestTheBootId:
    """The hole PID liveness alone cannot close, and the two ways of closing it too hard."""

    def test_a_lock_from_a_PREVIOUS_BOOT_is_reclaimed_even_if_its_pid_is_alive(self, tmp_path):
        """THE REBOOT HOLE. The machine dies holding the lock, returns, and an unrelated process is
        handed the same PID. `_pid_alive` then answers True forever and the executor is alive,
        reporting "busy", running nothing.

        Planted with THIS process's own pid, which is unambiguously alive, so the only thing that can
        free the lock is the boot id differing. A test using a dead pid would pass with the boot
        check removed entirely.
        """
        lock = tmp_path / 'expensive.lock'
        lock.write_text(
            json.dumps(
                {'pid': os.getpid(), 'label': 'gate:heavy', 'host': 'h', 'since': 't', 'boot': 'a-previous-boot'}
            ),
            encoding='utf-8',
        )

        assert read_owner(lock) is None, 'a lock from a previous boot must read as free'
        with exclusive_lock(lock, 'gate:fast') as owner:
            assert owner.pid == os.getpid()

    def test_an_UNKNOWN_boot_id_does_not_reclaim_a_live_holder(self, tmp_path):
        """The control, and it decides the direction the check errs in.

        A machine where the boot id cannot be read reports it as empty, and an empty id is not
        evidence of anything. It must therefore keep exactly the old behaviour -- liveness alone --
        rather than become a new way to steal a lock from a running gate.
        """
        lock = tmp_path / 'expensive.lock'
        lock.write_text(
            json.dumps({'pid': os.getpid(), 'label': 'gate:heavy', 'host': 'h', 'since': 't', 'boot': ''}),
            encoding='utf-8',
        )

        owner = read_owner(lock)
        assert owner is not None and owner.pid == os.getpid(), 'an unknown boot id must not free a live lock'

    def test_a_lock_is_not_reclaimed_one_minute_after_it_was_taken(self, tmp_path, monkeypatch):
        """A held lock must stay held as the clock advances. It did not.

        MEASURED 2026-08-09: `_boot_id()` returned MINUTES SINCE BOOT and `read_owner` treats any
        stored value != the current one as "that machine rebooted, the holder cannot exist". So a
        lock acquired at minute N was reported FREE from minute N+1 onward, and a second gate would
        take it while the first was still running. Every gate runs ~28 minutes, so this defeated the
        lock for essentially every real job -- and it surfaced as a concurrency test going red
        inside a gate, which reads exactly like a flaky test and was a real one.
        """
        lock_path = tmp_path / 'expensive.lock'
        with exclusive_lock(lock_path, 'holder:still-running'):
            assert read_owner(lock_path) is not None, 'sanity: the lock must be seen while held'

            # One minute later, same boot, same process, still running.
            now = exclusive._boot_id()
            later = str(int(now) + 60) if now.isdigit() else now
            monkeypatch.setattr(exclusive, '_boot_id', lambda: later)

            owner = read_owner(lock_path)
            assert owner is not None, (
                'the lock reads as FREE a minute after it was taken, so a second gate would start '
                'while the first is still running -- the exact collision this module prevents'
            )
            assert owner.label == 'holder:still-running'

    def test_a_real_reboot_still_reclaims_the_lock(self, tmp_path, monkeypatch):
        """The anti-vacuity half: tolerating the clock must not tolerate a REBOOT.

        Without this, the fix above could be "never compare boot ids at all", which restores the
        wedge it was written for.
        """
        lock_path = tmp_path / 'expensive.lock'
        with exclusive_lock(lock_path, 'holder:before-the-reboot'):
            assert read_owner(lock_path) is not None

            now = exclusive._boot_id()
            rebooted = str(int(now) + 86400) if now.isdigit() else 'a-different-boot-uuid'
            monkeypatch.setattr(exclusive, '_boot_id', lambda: rebooted)

            assert read_owner(lock_path) is None, (
                'a lock from a PREVIOUS boot must be reclaimable -- its holder is gone whatever the pid table says'
            )


class TestTheNamespaceIsREQUIRED:
    """WHOSE locks these are is the CALLER's fact, and it has no default.

    The constant this replaced was `_PROJECT_KEY = 'motronics'` inside vendor-neutral infrastructure
    -- invisible because it WORKED for that one project. Same defect as `forge.DEFAULT_REPO`, and
    its history is the argument against the half-fix: removing the constant while leaving a DEFAULT
    fixes the grep and not the defect, because a default is exactly how two unrelated projects come
    to share one machine's lock.
    """

    def test_calling_WITHOUT_a_namespace_is_an_ERROR(self):
        """A `TypeError`, not a fallback. This is the discriminating test for the whole change: a
        defaulted namespace would pass every other test in this class.
        """
        with pytest.raises(TypeError):
            lock_dir()  # type: ignore[call-arg]
        with pytest.raises(TypeError):
            lock_path_for_class(WHOLE_BOX)  # type: ignore[call-arg]

    def test_two_namespaces_get_DIFFERENT_lock_directories(self):
        assert lock_dir('alpha') != lock_dir('beta')

    def test_two_namespaces_get_DIFFERENT_lock_paths_for_the_SAME_class(self, tmp_path, monkeypatch):
        """The property that matters at runtime. Directories differing is not enough on its own --
        the class-to-filename mapping is applied per namespace, and a shared parent would put both
        projects' `expensive` lock on one file: unrelated work serialising on a slot it has no
        reason to share, which is a HANG rather than an error.
        """
        monkeypatch.setenv('ALPHA_LOCK_DIR', str(tmp_path / 'a'))
        monkeypatch.setenv('BETA_LOCK_DIR', str(tmp_path / 'b'))
        assert lock_path_for_class('alpha', WHOLE_BOX) != lock_path_for_class('beta', WHOLE_BOX)

    def test_two_namespaces_DO_NOT_EXCLUDE_each_other(self, tmp_path, monkeypatch):
        """Stated as behaviour, not as paths, because that is the claim a reader will over-read.
        A lock only excludes processes that TAKE it; machine-global scope would be unenforceable
        reach. Unrelated load is `admission.capacity_blocker`'s job -- it reads actual free RAM.
        """
        monkeypatch.setenv('ALPHA_LOCK_DIR', str(tmp_path / 'a'))
        monkeypatch.setenv('BETA_LOCK_DIR', str(tmp_path / 'b'))
        with (
            exclusive_lock(lock_path_for_class('alpha', WHOLE_BOX), 'alpha:holder'),
            exclusive_lock(lock_path_for_class('beta', WHOLE_BOX), 'beta:holder') as beta,
        ):
            assert beta.pid == os.getpid()

    def test_the_SAME_namespace_DOES_exclude_across_checkouts(self, tmp_path, monkeypatch):
        """The converse, and the reason the namespace is a constant rather than derived from a path.
        A worktree, a workspace copy and a second clone are one machine's worth of memory; a lock
        keyed by checkout could not serialise them, which is the defect that moved these files out
        of the repo in the first place.
        """
        monkeypatch.setenv('ALPHA_LOCK_DIR', str(tmp_path / 'a'))
        first = lock_path_for_class('alpha', WHOLE_BOX)
        with exclusive_lock(first, 'checkout-one'):
            second = lock_path_for_class('alpha', WHOLE_BOX)  # a different checkout, same namespace
            with pytest.raises(LockBusy), exclusive_lock(second, 'checkout-two'):
                pytest.fail('two checkouts of one namespace share one box, and one lock')

    def test_an_EMPTY_namespace_is_REFUSED(self):
        """Accepted, it would collapse every caller into one lock directory -- the same failure
        `vendor:` has in `admission.is_known_class`, refused for the same reason.
        """
        for empty in ('', '   ', '...', '-'):
            with pytest.raises(ValueError, match='namespace'):
                lock_dir(empty)

    def test_the_directory_is_NOT_inside_any_checkout(self):
        """A lock file inside a checkout cannot serialise two checkouts. It lived at
        `<repo>/output/.expensive.lock`, computed from `__file__`, and could not see a sibling
        worktree of the same project at all.
        """
        assert 'agent-swarm' not in str(lock_dir('alpha'))


class TestTheClassMapping:
    """Conflicting classes must land on ONE file; non-conflicting ones must not."""

    def test_the_whole_box_class_gets_its_own_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv('ALPHA_LOCK_DIR', str(tmp_path))
        assert lock_path_for_class('alpha', WHOLE_BOX).exists() is False  # named, not created
        assert lock_path_for_class('alpha', WHOLE_BOX).parent == lock_dir('alpha')

    def test_two_different_vendors_get_DIFFERENT_files(self, tmp_path, monkeypatch):
        """The classes are only real if they map to different files. A redirect that named a whole
        FILE collapsed them all onto one -- classes that looked enforced while being disabled, and a
        test isolating itself that way was exercising a build in which they did not exist.
        """
        monkeypatch.setenv('ALPHA_LOCK_DIR', str(tmp_path))
        assert lock_path_for_class('alpha', 'vendor:femm') != lock_path_for_class('alpha', 'vendor:jmag')
        assert lock_path_for_class('alpha', 'vendor:femm') != lock_path_for_class('alpha', WHOLE_BOX)

    def test_a_class_name_with_a_COLON_is_a_legal_filename(self, tmp_path, monkeypatch):
        """`:` is legal in a POSIX filename and ILLEGAL on Windows, where these locks already run.
        Asserted by CREATING the file, not by inspecting the string: a check on the name would pass
        against a mapping that produced an unopenable path.
        """
        monkeypatch.setenv('ALPHA_LOCK_DIR', str(tmp_path))
        path = lock_path_for_class('alpha', 'vendor:femm')
        assert ':' not in path.name
        with exclusive_lock(path, 'vendor:femm holder') as owner:
            assert owner.pid == os.getpid()


class TestTheRedirect:
    """The escape hatch relocates the directory. It cannot bypass the class mapping."""

    def test_the_env_var_is_DERIVED_from_the_namespace(self, tmp_path, monkeypatch):
        """One shared `LOCK_DIR` would undo the namespace parameter through the back door:
        redirecting one project's locks would silently redirect everyone's.
        """
        monkeypatch.setenv('ALPHA_LOCK_DIR', str(tmp_path / 'a'))
        monkeypatch.delenv('BETA_LOCK_DIR', raising=False)
        assert tmp_path / 'a' == lock_dir('alpha').parent
        assert tmp_path not in lock_dir('beta').parents

    def test_the_consumers_EXISTING_spelling_still_resolves(self, tmp_path, monkeypatch):
        """`motronics` must yield `MOTRONICS_LOCK_DIR`, the variable already set on live boxes and in
        the consumer's own tests. A derivation that produced a new spelling would silently ignore
        every redirect in place today -- and the tests using it would fall back to the real machine
        lock, colliding with whatever gate was running.
        """
        monkeypatch.setenv('MOTRONICS_LOCK_DIR', str(tmp_path))
        assert tmp_path in lock_path_for_class('motronics', WHOLE_BOX).parents

    def test_the_redirect_does_not_collapse_the_classes(self, tmp_path, monkeypatch):
        """The retired file-level override let any caller point every class at one file. This one
        cannot express that: relocatable, not bypassable.
        """
        monkeypatch.setenv('ALPHA_LOCK_DIR', str(tmp_path))
        paths = {lock_path_for_class('alpha', c) for c in (WHOLE_BOX, 'cheap', 'vendor:femm', 'vendor:jmag')}
        assert len(paths) == 4, paths
