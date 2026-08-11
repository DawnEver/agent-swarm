"""Bind every descendant of this process to this process's own lifetime.

THE OPERATING MODEL THIS SERVES (user directive 2026-08-11): `ci_loop` is started by a HUMAN and
managed from a TERMINAL, and **closing the terminal must stop the service**. Autostart is allowed
and does not change that -- it starts a terminal-bound session, not a background daemon.

**IT WAS A DECLARATION WITH NOTHING BEHIND IT.** Measured 2026-08-11: stopping a gate wrapper left
SIXTEEN orphaned pytest-xdist workers running for twenty minutes, taking a workstation from 8.6 GiB
free to 2.06 GiB. `ci_loop`'s own docstring says a running gate survives the terminal and publishes
anyway. Cooperative shutdown cannot fix this -- the workers are someone else's processes, several
levels down, and a child that is wedged, blocked in a syscall or simply not listening ignores every
polite request. So the enforcement has to come from the kernel, which needs no cooperation.

    Windows   a Job Object with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE. Every process started by this
              one is in the job by inheritance; when the LAST handle to the job closes -- which
              happens when this process dies, however it dies -- the kernel terminates all of them.
    POSIX     the shell already sends SIGHUP to the terminal's foreground process group on close.
              Children inherit the group, so the mechanism exists; what is added is a handler that
              kills the group, covering children that ignore SIGHUP.

**THE POSIX HALF IS WEAKER AND THIS SAYS SO WHERE THE READER IS**, rather than presenting one word
("bound") for two different guarantees. A job object is unconditional: the kernel terminates the
tree and nothing gets a vote. SIGHUP is a request that a determined child can ignore, and it only
arrives if the process is still in the terminal's foreground group. That is why `Binding` carries
`mechanism` and why a caller logs it.

**THE NAIVE POSIX MOVE IS BACKWARDS.** `setsid()` looks like "own my process group" and is exactly
the wrong call: a session leader is detached from the controlling terminal and therefore stops
receiving the SIGHUP the shell sends. It would remove the only signal the feature depends on while
reading, in a diff, as though it implemented it.

**WHAT THIS DOES NOT DO, NAMED.** It does not survive `SIGKILL` of this process on POSIX (no
handler runs; children are reparented and live on) -- only the Windows job object closes that hole,
because there the kernel acts on handle closure rather than on a signal being delivered. It also
does nothing about processes started BEFORE the binding: `bind_children_to_this_process` is a line
drawn at the moment it is called, and anything already running is `procs.reap`'s problem.
"""

from __future__ import annotations

import ctypes
import os
import signal
import sys
from dataclasses import dataclass


class LifetimeUnavailable(RuntimeError):
    """The kernel refused to bind the tree, so this process CANNOT keep the promise.

    RAISED, NEVER RETURNED AS A DEGRADED BINDING. A `Binding(mechanism='none')` would be a success
    return carrying a failure -- the shape this project names as the dominant defect -- and every
    caller would pass it straight through to a log nobody reads. A service whose operator was told
    "closing this terminal stops it" and for which that is false is worse than one that refuses to
    start.
    """


@dataclass(frozen=True, slots=True)
class Binding:
    """What was established, and by which kernel mechanism.

    `mechanism` is not decoration: the two mechanisms make DIFFERENT promises (see the module
    docstring), so a caller that reports "bound" without it is reporting a guarantee it may not
    hold. `handle` is the Windows job handle and exists to be kept alive -- the job dies with its
    last handle, so dropping it is what fires the kill. It is 0 on POSIX.
    """

    mechanism: str
    handle: int = 0


_BINDING: Binding | None = None

# From <winnt.h>. Spelled here rather than fetched, because there is nothing to fetch them from:
# ctypes exposes no constants, and a wrong value would be accepted silently by
# SetInformationJobObject as some other limit.
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOBOBJECT_EXTENDED_LIMIT_INFORMATION = 9


def bind_children_to_this_process() -> Binding:
    """Make this process's death kill every process it starts. Idempotent.

    IDEMPOTENT BY IDENTITY, not by "call it twice and it is fine". A second job object would put
    this process in a NESTED job, and closing the outer one leaves the inner job's processes
    running -- a binding that reads as established and enforces nothing. So the first binding is
    cached and returned unchanged.

    Returns:
        The binding, naming its mechanism. Keep it: on Windows it owns the job handle, and the job
        terminates its members when the last handle to it closes.

    Raises:
        LifetimeUnavailable: the kernel refused. The caller must not start work it cannot stop.
    """
    global _BINDING
    if _BINDING is not None:
        return _BINDING
    _BINDING = _bind_windows() if sys.platform == 'win32' else _bind_posix()
    return _BINDING


def _bind_windows() -> Binding:
    from ctypes import wintypes  # noqa: PLC0415 -- Windows-only; importing it on POSIX raises.

    class _IoCounters(ctypes.Structure):
        _fields_ = [  # noqa: RUF012 -- ctypes requires a mutable class attribute here.
            ('ReadOperationCount', ctypes.c_ulonglong),
            ('WriteOperationCount', ctypes.c_ulonglong),
            ('OtherOperationCount', ctypes.c_ulonglong),
            ('ReadTransferCount', ctypes.c_ulonglong),
            ('WriteTransferCount', ctypes.c_ulonglong),
            ('OtherTransferCount', ctypes.c_ulonglong),
        ]

    class _BasicLimits(ctypes.Structure):
        _fields_ = [  # noqa: RUF012
            ('PerProcessUserTimeLimit', wintypes.LARGE_INTEGER),
            ('PerJobUserTimeLimit', wintypes.LARGE_INTEGER),
            ('LimitFlags', wintypes.DWORD),
            ('MinimumWorkingSetSize', ctypes.c_size_t),
            ('MaximumWorkingSetSize', ctypes.c_size_t),
            ('ActiveProcessLimit', wintypes.DWORD),
            ('Affinity', ctypes.POINTER(ctypes.c_ulong)),
            ('PriorityClass', wintypes.DWORD),
            ('SchedulingClass', wintypes.DWORD),
        ]

    class _ExtendedLimits(ctypes.Structure):
        _fields_ = [  # noqa: RUF012
            ('BasicLimitInformation', _BasicLimits),
            ('IoInfo', _IoCounters),
            ('ProcessMemoryLimit', ctypes.c_size_t),
            ('JobMemoryLimit', ctypes.c_size_t),
            ('PeakProcessMemoryUsed', ctypes.c_size_t),
            ('PeakJobMemoryUsed', ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    # EVERY SIGNATURE IS DECLARED, AND THIS IS NOT STYLE. ctypes defaults an undeclared `restype`
    # to `c_int`, which TRUNCATES a 64-bit HANDLE to 32 bits. It is invisible most of the time --
    # handle values are usually small -- so the failure is rare, load-dependent and looks like
    # anything but a missing declaration. It was caught here by a probe whose `DuplicateHandle`
    # silently returned 0 for exactly this reason, which is also why the test below checks the
    # RETURN of `GetHandleInformation` rather than trusting the flags it wrote.
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.GetCurrentProcess.argtypes = []
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    # AN ANONYMOUS, NON-INHERITABLE JOB, and both properties are load-bearing. `CreateJobObjectW`
    # with a NULL security descriptor yields a handle that children do NOT inherit -- which is what
    # makes this process's death close the LAST handle. An inheritable handle would leave every
    # child holding one, so the job would outlive the parent and kill nothing, while this function
    # returned success. Anonymous, because a NAMED job is reachable by any process that guesses the
    # name and would be shared between two concurrently running loops.
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        msg = f'CreateJobObject failed: {ctypes.WinError(ctypes.get_last_error())}'
        raise LifetimeUnavailable(msg)

    limits = _ExtendedLimits()
    limits.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel32.SetInformationJobObject(
        job,
        _JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
        ctypes.cast(ctypes.byref(limits), wintypes.LPVOID),
        ctypes.sizeof(limits),
    ):
        # RAISE RATHER THAN CARRY ON WITH THE JOB. A job without the flag is a container that
        # terminates nobody -- the assignment below would still succeed, so the failure would be
        # entirely invisible at the call site.
        msg = f'SetInformationJobObject failed: {ctypes.WinError(ctypes.get_last_error())}'
        raise LifetimeUnavailable(msg)

    if not kernel32.AssignProcessToJobObject(job, kernel32.GetCurrentProcess()):
        msg = (
            f'AssignProcessToJobObject failed: {ctypes.WinError(ctypes.get_last_error())}. '
            'This process is probably already in a job that forbids breakaway -- some CI agents '
            'and debuggers do that. Nested jobs need Windows 8 or later.'
        )
        raise LifetimeUnavailable(msg)
    return Binding(mechanism='job_object', handle=job)


def _bind_posix() -> Binding:
    # THE GROUP IS DELIBERATELY LEFT ALONE. See the module docstring: `setsid()` would detach this
    # process from the controlling terminal and stop the SIGHUP arriving at all.
    def _hangup(_signum: int, _frame: object) -> None:
        # KILL THE GROUP, NOT JUST SELF. The shell already SIGHUPs the group, so a child that
        # honours it is gone before this runs; this exists for the ones that do not, and SIGKILL is
        # correct here because the terminal is already gone and there is nothing left to flush.
        # `os.killpg` on our own group also hits this process, which is the intent -- the service
        # must not outlive its terminal either.
        os.killpg(os.getpgrp(), signal.SIGKILL)

    try:
        signal.signal(signal.SIGHUP, _hangup)
    except (OSError, ValueError) as exc:
        # ValueError: not the main thread. OSError: the platform refuses the handler. Either way
        # this process cannot promise the property, and saying so beats a binding that is decoration.
        msg = f'cannot install a SIGHUP handler, so the terminal cannot stop this service: {exc}'
        raise LifetimeUnavailable(msg) from exc
    return Binding(mechanism='process_group')
