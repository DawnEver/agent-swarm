"""A machine-level exclusive lock for expensive work, held by a NAMED owner.

WHY THIS EXISTS. Two concurrent gates on one box have been measured to starve each other: 2 x 16
workers x ~1.2 GB exceeds that machine's 31.71 GiB, and the result is not a clean failure but
``MemoryError`` / ``[gate:budget] EXCEEDED`` / ``node down`` -- non-verdicts that read like code
regressions. One night produced nine of them, only two of which involved the code.

The obvious place to serialise is a CI queue or a wrapper script. Both are BYPASSABLE: three agents
develop on those machines and any of them can invoke the gate directly. *Documentation is not a
control; the shortest correct path is* -- so the lock lives where every path already goes through,
and no discipline is required of anyone.

TWO PROPERTIES THAT ARE NOT NEGOTIABLE, both learned the expensive way:

* **A stale lock is reclaimed by PID LIVENESS, never by a timestamp.** A heavy gate legitimately
  runs 25+ minutes and an unbounded group runs longer, so any timeout short enough to clear a
  crashed holder is also short enough to evict a healthy one -- and evicting a healthy holder
  recreates exactly the concurrency this module exists to prevent, while making it look like the
  lock is working.
* **The holder is NAMED in the lock file.** "Resource busy" with no owner is the message that gets
  worked around; ``held by pid 49124 (gate:heavy tree=9b9235204607) since 15:02:11`` is the one that
  gets waited on. A caller that cannot say WHO is blocking it teaches people to delete lock files.

NOT A LOCK ACROSS MACHINES. It guards one box. Two runners on two hosts are independent and that is
intended -- the constraint being modelled is physical memory, which is per-machine.

STDLIB ONLY, like every module in this package. The consumer that needs this most is an instrument
that MEASURES a package, so it must stay runnable when that package is broken -- which is exactly
when the instrument is needed. A lock with a dependency is a lock that can be uninstalled.

THE NAMESPACE IS A REQUIRED ARGUMENT, and its absence is the one thing this file gained on the way
in. It arrived carrying ``_PROJECT_KEY = 'motronics'``, a constant naming one project inside
vendor-neutral infrastructure -- invisible for the usual reason, that it WORKED for that project.
The same defect as `forge.DEFAULT_REPO`, and its history is the argument against the tempting fix:
removing the CONSTANT while leaving a DEFAULT would have fixed the grep and not the defect, because
a default is how two unrelated projects silently share one machine's lock and serialise work that
has no reason to contend. A caller that omits it gets a `TypeError`, not a stranger's lock file.
"""

from __future__ import annotations

import contextlib
import ctypes
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Generator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from agent_swarm.admission import WHOLE_BOX

#: Resolved once (ruff S607 refuses a partial executable path). Absent off macOS, where the branch
#: that uses it is unreachable anyway.
_SYSCTL = shutil.which('sysctl') or 'sysctl'

#: `WHOLE_BOX` is NOT re-exported. It was, in the version this came from, for a mechanical reason
#: that does not survive the move: the consumer imported this file by path (`scripts/` is not a
#: package), so a sibling module was unreachable and re-exporting was the only way to keep one
#: definition. Here `agent_swarm.admission` is an ordinary sibling and already exports it from the
#: package root, so a second spelling would be a second entry point for one value -- the shape that
#: lets two callers drift about which name is authoritative. Import it from where it is defined.
__all__ = [
    'LockBusy',
    'LockOwner',
    'exclusive_lock',
    'lock_dir',
    'lock_path_for_class',
    'read_owner',
]


def _boot_id() -> str:
    """An identifier that CHANGES ACROSS A REBOOT. Empty string when it cannot be established.

    WHY A LOCK NEEDS THIS. Staleness is decided by PID liveness, never by a timestamp -- a heavy gate
    legitimately runs 25+ minutes, so any age-based reclaim would evict a healthy holder and recreate
    the very concurrency this module prevents. That reasoning is correct WITHIN one boot and breaks
    across a reboot: the machine dies holding the lock, comes back, and some unrelated process is
    handed the same PID. `_pid_alive` then answers True forever and the lock is never reclaimed --
    an executor that is alive, reports "busy", and runs nothing.

    Reported as unknown rather than guessed. An empty boot id never triggers a reclaim, so a machine
    where this cannot be read keeps exactly today's behaviour instead of a new failure mode.
    """
    if sys.platform == 'win32':
        try:
            # THE BOOT EPOCH, not the uptime. `now - uptime` is the same wall-clock instant for
            # every process in this boot, so two of them agree; uptime itself is different every
            # time it is read, and comparing THAT for equality is the bug below.
            #
            # `WinDLL('kernel32')` rather than `ctypes.windll.kernel32`, for the reason written at
            # `_pid_alive` below: `windll` is a lazy attribute container the type stubs do not
            # model, so reaching through it needs a `type: ignore` while binding the library
            # returns a plain object the checker is content with. Same fact, no suppression.
            uptime_ms = ctypes.WinDLL('kernel32').GetTickCount64()
            return str(int(time.time() - uptime_ms / 1000.0))
        except (OSError, AttributeError):
            return ''
    try:
        return Path('/proc/sys/kernel/random/boot_id').read_text(encoding='utf-8').strip()
    except OSError:
        pass
    # macOS HAS NO `/proc`, so the read above always failed there and `_boot_id` returned '' --
    # which `_same_boot` treats as "no evidence of a reboot" and short-circuits to True. The whole
    # reboot-with-recycled-PID hazard this mechanism exists for was therefore UNGUARDED on every
    # mac node, and the failure mode is the loud one: a crash while holding the lock, then a
    # reboot, wedges the box-wide lock permanently and the executor reports BUSY while running
    # nothing. Linux was fine; nobody would have noticed until a mac joined the fleet.
    #
    # `kern.boottime` is the darwin equivalent and yields the boot EPOCH, which compares like the
    # Windows value -- numeric, jitter-tolerant -- rather than like the Linux UUID. Parsed for the
    # `sec=` field rather than by position: the format is
    # `{ sec = 1723200000, usec = 123456 } Sat Aug  9 ...`.
    try:
        out = subprocess.run(
            [_SYSCTL, '-n', 'kern.boottime'],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return ''
    match = re.search(r'sec\s*=\s*(\d+)', out)
    return match.group(1) if match else ''


#: How far two readings of the Windows boot epoch may differ and still mean the same boot.
#: `now - uptime` is computed from two clocks sampled a moment apart, so it jitters by well under a
#: second between processes; a reboot moves it by minutes at least. 120 s sits far above the noise
#: and far below the signal. On POSIX the id is a kernel UUID and compares exactly, so this is
#: unused there -- stated because a reader will otherwise assume the tolerance applies everywhere.
_BOOT_JITTER_S = 120


def _same_boot(recorded: str, current: str) -> bool:
    """Whether two boot ids name the SAME boot. Tolerant of clock jitter, not of a reboot.

    THE DEFECT THIS REPLACES, measured 2026-08-09. `_boot_id` returned MINUTES SINCE BOOT and this
    comparison was `recorded != current`. Minutes-since-boot advances every 60 s, so a lock taken
    at minute N read as a DIFFERENT MACHINE from minute N+1 onward: `read_owner` returned None, the
    lock reported FREE, and a second gate could start while the first was still running. Every gate
    runs ~28 minutes, so the expensive-work lock was defeatable about a minute after it was taken,
    for essentially every real job.

    It hid well. The window is only wide when two gates start within the same minute, so the
    collision it permitted was rare rather than absent -- and the symptom, when it finally fired,
    was a "second process is refused while the lock is held" test going red inside a gate, which
    reads exactly like a flaky test.

    A tolerance rather than equality: the quantity is derived from two clock samples, and equality
    on such a value is a hash comparison on a measurement.
    """
    if not recorded or not current:
        return True  # an unknown id is not evidence of a reboot; keep the PID-liveness path
    if recorded == current:
        return True
    try:
        return abs(int(recorded) - int(current)) <= _BOOT_JITTER_S
    except ValueError:
        return False  # non-numeric ids (the POSIX UUID) compare exactly, and these differed


@dataclass(frozen=True)
class LockOwner:
    """Who holds the lock. Written as JSON so a human and a scheduler read the same bytes."""

    pid: int
    label: str
    host: str
    since: str
    boot: str = ''

    def describe(self) -> str:
        return f'pid {self.pid} ({self.label}) on {self.host} since {self.since}'


class LockBusy(RuntimeError):
    """Raised when the lock is held. Carries the OWNER, so the caller can say who is blocking it."""

    def __init__(self, owner: LockOwner | None) -> None:
        self.owner = owner
        who = owner.describe() if owner else 'an unidentified process (lock file unreadable)'
        super().__init__(f'expensive work is already running: {who}')


def _pid_alive(pid: int) -> bool:
    """Is this PID a live process? A FALSE here reclaims a lock, so it errs toward ALIVE.

    On any doubt -- a permission error, an unexpected OS error -- the answer is True. Treating an
    ambiguous signal as "dead" would reclaim the lock from a healthy holder, which is worse than
    refusing to run: the run that gets refused is retried a minute later, the run that gets
    double-started corrupts a verdict and may take the machine down.
    """
    if pid <= 0:
        return False
    if os.name == 'nt':
        process_query_limited_information = 0x1000
        still_active = 259
        # BOUND ONCE rather than reached through `ctypes.windll.kernel32.<fn>` at each call site.
        # `windll` is a lazy attribute container the type stubs do not model, so every access needed
        # a `type: ignore` -- four suppressions recording the same fact. `WinDLL` returns a plain
        # object, so the checker has nothing to report and the ignores are gone rather than deferred.
        kernel32 = ctypes.WinDLL('kernel32')
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            # ERROR_ACCESS_DENIED (5) means the process EXISTS but is not ours to inspect.
            return kernel32.GetLastError() == 5
        try:
            code = ctypes.c_ulong()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return code.value == still_active
            return True
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def read_owner(lock_path: Path) -> LockOwner | None:
    """The current holder, or None when the lock is free, unreadable, or held by a dead process.

    A dead holder reads as None -- that IS the reclaim, and it is a read-only operation so a status
    command can report "stale lock from pid N" without taking the lock.
    """
    try:
        raw = json.loads(lock_path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    try:
        owner = LockOwner(
            pid=int(raw['pid']),
            label=str(raw['label']),
            host=str(raw['host']),
            since=str(raw['since']),
            boot=str(raw.get('boot', '')),
        )
    except (KeyError, TypeError, ValueError):
        return None
    # A DIFFERENT BOOT means the holder cannot exist, whatever the PID table says -- the machine that
    # took this lock is gone. Checked BEFORE liveness because after a reboot a recycled PID makes
    # `_pid_alive` answer True about an unrelated process, which would wedge the lock permanently.
    # Both ids must be known: an unknown one is not evidence of anything, so it keeps the old path.
    current = _boot_id()
    if not _same_boot(owner.boot, current):
        return None
    return owner if _pid_alive(owner.pid) else None


@contextlib.contextmanager
def exclusive_lock(lock_path: Path, label: str, *, wait_seconds: float = 0.0) -> Generator[LockOwner]:
    """Hold the machine's expensive-work lock for the duration of the block.

    ``wait_seconds = 0`` (the default) fails FAST with :class:`LockBusy`. That is the right default
    for a scheduled one-shot runner: there is no value in a tick queueing behind a 40-minute job when
    another tick will start in a minute, and a queue of blocked processes is itself a resource leak.
    An interactive caller may pass a wait.

    The lock file is created with ``O_EXCL``, so acquisition is atomic against another process doing
    the same thing at the same instant -- checking then creating would leave a window exactly wide
    enough for the race this guards.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    owner = LockOwner(
        pid=os.getpid(),
        label=label,
        host=platform.node() or '?',
        since=datetime.now(UTC).isoformat(timespec='seconds'),
        boot=_boot_id(),
    )
    deadline = time.monotonic() + wait_seconds
    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            held = read_owner(lock_path)
            if held is None:
                # Dead or unreadable holder: reclaim. `missing_ok` because another process may be
                # reclaiming the same corpse concurrently, and losing that race is fine -- the
                # O_EXCL retry below is what actually decides who gets the lock.
                lock_path.unlink(missing_ok=True)
                continue
            if time.monotonic() >= deadline:
                raise LockBusy(held) from None
            time.sleep(min(1.0, max(0.05, wait_seconds / 20)))
            continue
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as handle:
                json.dump(owner.__dict__, handle)
            yield owner
        finally:
            # Only ever remove OUR OWN lock. After a reclaim the file may belong to someone else,
            # and deleting it unconditionally would hand a second process the lock while the first
            # still holds it -- the failure this module exists to make impossible.
            current = read_owner(lock_path)
            if current is None or current.pid == owner.pid:
                lock_path.unlink(missing_ok=True)
        return


# ---------------------------------------------------------------------------
# WHERE THE LOCK FILES LIVE
#
# One definition, consulted by every layer that needs to name a lock. It was computed in TWO places
# -- a gate deriving it from its own ``__file__`` and a scheduler from the repo root -- so the two
# could disagree about which file serialises a machine, and did.
# ---------------------------------------------------------------------------


def _namespace_env(namespace: str) -> str:
    """The environment variable that relocates ``namespace``'s lock directory.

    DERIVED FROM THE NAMESPACE RATHER THAN FIXED, because a single variable name would undo the
    parameter directly above it: one `LOCK_DIR` shared by every project on the box means redirecting
    one project's locks silently redirects everyone's, which is the collision the namespace exists
    to prevent, reintroduced through the escape hatch. `motronics` yields `MOTRONICS_LOCK_DIR`, the
    spelling already in use, so the consumer's existing redirect keeps working unchanged.

    IT REDIRECTS THE DIRECTORY, NEVER A FILE. The retired file-level override it replaces named a
    whole FILE, which let any caller collapse every exclusivity class onto one lock -- classes that
    looked enforced while being disabled, and a test isolating itself with it was exercising a
    configuration in which they did not exist. This form cannot express that: the class-to-filename
    mapping below is relocatable, not bypassable.
    """
    return f'{re.sub(r"[^A-Za-z0-9]+", "_", namespace).strip("_").upper()}_LOCK_DIR'


def lock_dir(namespace: str) -> Path:
    """Where ``namespace``'s locks live on THIS MACHINE. Never inside a checkout.

    THE RESOURCE IS THE BOX, so the lock's identity must be a property of the machine. The scheme
    this replaced put the file inside the checkout (`<repo>/output/.expensive.lock`, computed from
    ``__file__``), and **a lock file inside a checkout cannot serialise two checkouts** -- which is
    not a corner case but the prescribed workflow: a lane gates from a worktree while a scheduler
    gates from a workspace copy, and neither could see the other. Two whole-box gates on one machine,
    which is exactly what the lock claims to prevent.

    THE NAMESPACE IS THE IDENTITY, and it is a CONSTANT the caller declares once rather than
    something derived from a path -- deriving it from the checkout is the defect above, respelled.

    SCOPE, stated because a lock claim invites "everything":

    * **Every checkout under ONE namespace on THIS machine shares one lock.** Worktree, workspace
      copy, second clone -- a clone is the discriminating case, since it shares no git common dir,
      so any fix that walked up looking for `.git` would still have got it wrong.
    * **A DIFFERENT namespace does not, and that is a decision.** A lock only excludes processes
      that TAKE it, and an unrelated project never will -- so machine-global scope would be
      unenforceable reach, a claim the mechanism cannot back. Unrelated load is handled by
      MEASUREMENT instead: `admission.capacity_blocker` reads actual free RAM and sees another
      project's 12 GB whether or not that project has heard of this one. Exclusion is for
      co-operating processes; measurement is for the rest.
    * **Per USER, as an accepted consequence of the temp dir.** Two accounts on one box do not
      contend. Named rather than hidden; cross-user locking needs permissions this does not want.

    Raises:
        ValueError: if ``namespace`` is empty or has no usable characters. An empty namespace would
            make every caller's locks collapse into one directory -- the same failure `vendor:` has
            in `admission.is_known_class`, and the same reason it is refused: unrelated work
            serialising on a slot it has no reason to share is a HANG, which is the failure mode you
            cannot read off a log.
    """
    key = re.sub(r'[^A-Za-z0-9._-]+', '-', namespace).strip('.-')
    if not key:
        msg = f'lock namespace {namespace!r} is empty after sanitising; a lock must name whose it is'
        raise ValueError(msg)
    root = os.environ.get(_namespace_env(namespace)) or tempfile.gettempdir()
    return Path(root) / f'.{key}-locks'


def lock_path_for_class(namespace: str, cls: str) -> Path:
    """The lock file for one exclusivity class within ``namespace``.

    THE CLASS RELATION IS `admission.classes_conflict`'s; this only names the file. Two jobs that
    conflict must land on the SAME path, which is why the mapping is here and not at a call site --
    two layers spelling it themselves are two layers free to disagree about which file serialises
    the machine.

    The ``:`` in ``vendor:femm`` is replaced because it is legal in a POSIX filename and ILLEGAL on
    Windows, where these locks already run.
    """
    directory = lock_dir(namespace)
    directory.mkdir(parents=True, exist_ok=True)
    name = 'expensive' if cls == WHOLE_BOX else cls.replace(':', '-', 1)
    return directory / f'.{name}.lock'
