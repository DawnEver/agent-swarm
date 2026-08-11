"""The terminal owns the service, and the KERNEL is what enforces it.

WHY THIS EXISTS, MEASURED. 2026-08-11 a stop on a gate wrapper left SIXTEEN orphaned pytest-xdist
workers running for twenty minutes and drove a workstation from 8.6 GiB free to 2.06 GiB. The
operating model is `ci_loop` started by a human and managed from a terminal, closing the terminal
stops the service -- and before this module that was a DECLARATION with nothing consulting it.

WHAT IS AND IS NOT TESTED HERE. Binding is asserted directly. Actually killing the tree when the
terminal closes is NOT asserted, and saying so is the point: reproducing it needs a real console
whose handle is closed out from under a real process tree, which no in-process test can stage
honestly. What the tests below pin is everything short of that -- that a binding is established or
RAISES, that it names its mechanism so a caller can tell which one it got, and that the Windows
handle is not inheritable, which is the one mistake that would make the whole mechanism a silent
no-op (an inherited handle keeps the job open forever, so nothing is ever killed and every test
that merely asserted "bind() returned" would still pass).
"""

from __future__ import annotations

import os
import sys

import pytest

from agent_swarm import lifetime


class TestBinding:
    def test_binding_names_the_mechanism_it_actually_got(self) -> None:
        """A caller must be able to TELL. Two kernels enforce this differently and one of them
        (POSIX) is weaker -- it relies on a signal a child may ignore -- so a caller that logged
        "bound" without saying HOW would be reporting a guarantee it may not have.
        """
        binding = lifetime.bind_children_to_this_process()
        expected = 'job_object' if sys.platform == 'win32' else 'process_group'
        assert binding.mechanism == expected

    def test_binding_is_idempotent_and_returns_the_SAME_binding(self) -> None:
        """`ci_loop` may call this from more than one entry point. A second job object would be the
        real hazard: the process would sit in a nested job, and closing only the outer one leaves
        the inner alive -- a binding that reads as established and enforces nothing.
        """
        first = lifetime.bind_children_to_this_process()
        second = lifetime.bind_children_to_this_process()
        assert first is second

    @pytest.mark.skipif(sys.platform != 'win32', reason='job objects are a Windows kernel object')
    def test_the_job_handle_is_NOT_inheritable(self) -> None:
        """THE DISCRIMINATING ASSERTION, and the only one that separates this mechanism from a
        decorative one. `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` fires when the LAST handle closes. If
        the handle is inheritable, every child holds one, so the job outlives the parent and kills
        nothing -- while `bind()` returned successfully and every other test here stayed green.
        """
        import ctypes
        from ctypes import wintypes

        handle_flag_inherit = 0x00000001
        k = ctypes.WinDLL('kernel32', use_last_error=True)
        k.GetCurrentProcess.restype = wintypes.HANDLE
        k.GetCurrentProcess.argtypes = []
        k.GetHandleInformation.restype = wintypes.BOOL
        k.GetHandleInformation.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        k.DuplicateHandle.restype = wintypes.BOOL
        k.DuplicateHandle.argtypes = [
            wintypes.HANDLE,
            wintypes.HANDLE,
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.HANDLE),
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        ]

        def inherit_flag_of(handle) -> bool:
            flags = wintypes.DWORD()
            # THE RETURN IS CHECKED. Without it this reads whatever was already in `flags` -- which
            # is how the first version of this probe reported "not inheritable" for a handle that
            # WAS, and would have certified a broken binding.
            assert k.GetHandleInformation(handle, ctypes.byref(flags)), ctypes.WinError(ctypes.get_last_error())
            return bool(flags.value & handle_flag_inherit)

        binding = lifetime.bind_children_to_this_process()

        # THE CONTROL, and it is the reason this test is worth anything. An assertion that some
        # handle is not inheritable passes just as happily when the probe cannot detect
        # inheritance at all. So a deliberately INHERITABLE clone of the same job is measured
        # alongside: if the probe cannot tell these two apart, it cannot tell anything apart.
        duplicate_same_access = 2
        inheritable = wintypes.HANDLE()
        me = k.GetCurrentProcess()
        assert k.DuplicateHandle(me, binding.handle, me, ctypes.byref(inheritable), 0, True, duplicate_same_access), (
            ctypes.WinError(ctypes.get_last_error())
        )
        assert inherit_flag_of(inheritable) is True, 'the probe cannot detect inheritance, so it proves nothing'

        assert inherit_flag_of(binding.handle) is False

    @pytest.mark.skipif(sys.platform == 'win32', reason='process groups are POSIX')
    def test_on_posix_the_process_group_is_NOT_changed(self) -> None:
        """The naive move -- `setsid()` -- is exactly BACKWARDS and would break the feature it looks
        like it implements: a session leader is detached from the controlling terminal and therefore
        stops receiving the SIGHUP the shell sends when that terminal closes. So the binding must
        leave the group alone and add a handler; this asserts the group is untouched.
        """
        before = os.getpgrp()
        lifetime.bind_children_to_this_process()
        assert os.getpgrp() == before


def test_the_module_refuses_rather_than_returning_an_unbound_binding() -> None:
    """There is no third outcome. A `Binding(mechanism='none')` would be the forbidden shape: a
    success return that carries a failure, which every caller would pass straight through.
    """
    assert not hasattr(lifetime, 'NO_BINDING')
    assert issubclass(lifetime.LifetimeUnavailable, RuntimeError)
