"""The pre-flight must separate "this identity may not see it" from "the forge is down" -- or say so.

MOVED HERE WITH THE CODE, 2026-08-12. The measurement that produced it stays in the sibling project;
what moved is the mechanism. A manifest declared an unpinned git dependency on a private repository;
the credential stored across the fleet authenticated perfectly but held no read grant, and the forge
answers a private-and-invisible repository with 404 rather than 403. Every install died in dependency
RESOLUTION with a 404 buried in it -- a message blaming resolution for an access-control fact.

THE CONTROL IS THE MECHANISM. One probe cannot tell the two explanations apart; two probes on the
SAME HOST can. So the outcome is three-valued and the tests below drive all three, including the
direction a careless pre-flight gets wrong: with no same-host control it must conclude NOTHING.

WHAT THESE CANNOT SEE: nothing here contacts a real forge, so they say nothing about how any
particular server renders an access failure -- only about how this module reasons from the answers.
"""

from __future__ import annotations

import subprocess

import pytest

from agent_swarm.installer import git_url, host_of, preflight, split_passthrough

#: The host both the target and the control live on. `HOST` carries no userinfo; `CONTROL` does, so
#: the same-host comparison is exercised on the shape the fleet actually uses.
_HOST = 'forge.example:9000'
_TARGET = f'widget-lab[web] @ git+http://{_HOST}/org/widget-lab.git'
_CONTROL = f'http://role-account@{_HOST}/org/control-repo.git'
_OTHER_HOST_CONTROL = 'http://other.example/org/control-repo.git'


class _Probe:
    """A scripted `subprocess.run`, keyed by what the command is ASKING, recording every call.

    Keyed by intent rather than call order because the whole design is that the TARGET and the
    CONTROL are two probes whose answers are compared; a double that could not tell them apart would
    pass for a pre-flight that ran only one -- the version that cannot discriminate at all.
    """

    def __init__(self, *, answers: dict[str, int], username: str | None = 'role-account') -> None:
        self.answers = answers
        self.username = username
        self.calls: list[list[str]] = []

    def __call__(self, cmd, **_kwargs) -> subprocess.CompletedProcess:
        self.calls.append(list(cmd))
        if 'credential' in cmd:
            # A credential helper emits the password on stdout beside the username. The double MUST
            # too -- one that only ever returned a username would be better-behaved than reality and
            # would certify a parser that leaks.
            out = '' if self.username is None else f'username={self.username}\npassword=hunter2-SECRET\n'
            return subprocess.CompletedProcess(cmd, 0 if self.username else 1, out, '')
        url = cmd[-2] if cmd[-1] == 'HEAD' else cmd[-1]
        for fragment, code in self.answers.items():
            if fragment in url:
                if code == -1:
                    raise subprocess.TimeoutExpired(cmd, 1.0)
                return subprocess.CompletedProcess(cmd, code, '', 'remote: Repository not found\n')
        return subprocess.CompletedProcess(cmd, 0, '', '')


def test_a_reachable_git_dependency_does_not_refuse() -> None:
    """THE HAPPY PATH IS THE ONE THAT MUST NOT REGRESS. A pre-flight that blocks a working install
    is strictly worse than the opaque error it replaces.
    """
    probe = _Probe(answers={})
    assert preflight([_TARGET], control_url=_CONTROL, run=probe) is None
    assert probe.calls, 'the reachable case must still have been probed, or the None means nothing'


def test_no_git_dependencies_probes_NOTHING() -> None:
    """Cheapness is a property under test, not an intention."""
    probe = _Probe(answers={})
    assert preflight(['numpy>=2', 'pytest'], control_url=_CONTROL, run=probe) is None
    assert probe.calls == []


def test_the_SAME_refusal_depends_on_whether_the_control_shares_the_host() -> None:
    """THE DISCRIMINATING TEST FOR THE EXTRACTION, and it is the mechanism itself.

    Identical requirements, identical probe answers, TWO control URLs, TWO outcomes. With a
    same-host control that resolves, the host is up and the credential authenticates, so the only
    explanation left is the grant -- that is a refusal. With a control on a DIFFERENT host, the two
    explanations do not separate and there is no refusal to make. A `control_url` that was ignored,
    or defaulted to some checkout this module was never told about, would give one answer to both.
    """
    same_host = preflight([_TARGET], control_url=_CONTROL, run=_Probe(answers={'widget-lab': 128}))
    other_host = preflight([_TARGET], control_url=_OTHER_HOST_CONTROL, run=_Probe(answers={'widget-lab': 128}))

    assert same_host is not None and 'widget-lab' in same_host
    assert other_host is None, 'a control on another host answered a different question'


def test_the_refusal_names_the_url_the_identity_and_who_can_fix_it() -> None:
    message = preflight([_TARGET], control_url=_CONTROL, run=_Probe(answers={'widget-lab': 128}))
    assert message is not None
    assert f'{_HOST}/org/widget-lab.git' in message
    assert _TARGET in message
    assert 'role-account' in message, 'the identity IS the diagnosis; a refusal without it names no cause'
    assert 'authenticat' in message.lower(), 'it must say this is NOT a login failure, or the reader retries the login'
    assert 'read' in message.lower() and 'grant' in message.lower()


def test_the_refusal_NEVER_carries_the_secret() -> None:
    """`git credential fill` emits the password beside the username. The parser takes one line and
    the rest must reach nothing -- not the message, not a log, not an exception.
    """
    message = preflight([_TARGET], control_url=_CONTROL, run=_Probe(answers={'widget-lab': 128}))
    assert message is not None
    assert 'hunter2' not in message and 'password' not in message.lower()


def test_an_unreadable_identity_still_refuses_and_says_so() -> None:
    """No stored credential is a DIFFERENT sentence, not a missing one. Suppressing the refusal
    because the username could not be read would drop the finding for the machine least able to
    diagnose it.
    """
    probe = _Probe(answers={'widget-lab': 128}, username=None)
    message = preflight([_TARGET], control_url=_CONTROL, run=probe)
    assert message is not None and 'widget-lab' in message
    assert 'username unreadable' in message


def test_a_DOWN_host_falls_through_and_is_never_rendered_as_access() -> None:
    """THE OTHER DIRECTION, and the one a careless pre-flight gets wrong. When the control fails too,
    "you lack access" and "the forge is down" are indistinguishable, and an operator whose install
    would have resolved FROM CACHE must not be stranded by a guess.
    """
    probe = _Probe(answers={'forge.example': 128})
    assert preflight([_TARGET], control_url=_CONTROL, run=probe) is None


def test_no_control_at_all_declines_to_conclude() -> None:
    """`control_url` may be None, and None means "no same-host control exists" -- which yields no
    verdict rather than a verdict made without one.
    """
    probe = _Probe(answers={'widget-lab': 128})
    assert preflight([_TARGET], control_url=None, run=probe) is None


def test_a_probe_that_HANGS_falls_through_rather_than_becoming_a_second_failure_mode() -> None:
    """A credential helper can raise a GUI prompt that no environment variable suppresses. The
    pre-flight is bounded, and a timeout is INDETERMINATE, never a refusal.
    """
    probe = _Probe(answers={'widget-lab': -1})
    assert preflight([_TARGET], control_url=_CONTROL, run=probe) is None


def test_control_url_is_REQUIRED_and_has_no_default() -> None:
    """Making it explicit is what stops this module reaching for a checkout it was never told about;
    a default would be the mechanism by which that coupling became invisible.
    """
    with pytest.raises(TypeError):
        preflight([_TARGET])  # type: ignore[call-arg]


class TestTheRequirementParsingIsMeasured:
    def test_the_url_is_parsed_out_and_stripped_of_its_REV(self) -> None:
        assert git_url('widget-lab[web] @ git+http://h/o/r.git') == 'http://h/o/r.git'
        assert git_url('some-pkg @ git+https://h/o/r.git@deadbeef') == 'https://h/o/r.git'
        assert git_url('numpy>=2') is None

    def test_a_userinfo_AT_is_not_mistaken_for_a_rev(self) -> None:
        """The `@` in `user@host` precedes a `/`, so it is userinfo and not a revision. Truncating
        there would hand `ls-remote` a URL with no path and turn every probe into a false failure.
        """
        assert git_url('some-pkg @ git+http://role-account@h/o/r.git') == 'http://role-account@h/o/r.git'

    def test_a_userinfo_prefix_does_not_defeat_the_same_host_comparison(self) -> None:
        """Remote URLs in this fleet carry a baked-in account. A naive netloc compare would read
        that as a DIFFERENT host and silently decline to conclude -- a guard that quietly stops
        guarding on exactly the fleet it was written for.
        """
        assert host_of(f'http://role-account@{_HOST}/o/r.git') == _HOST
        assert host_of(f'http://{_HOST}/o/r.git') == _HOST

    def test_a_fragment_is_not_part_of_the_url(self) -> None:
        assert git_url('some-pkg @ git+https://h/o/r.git#subdirectory=x') == 'https://h/o/r.git'


class TestTheArgvSplitCanExpressTheCanonicalInstall:
    """A locked path that cannot express the one command everybody needs guarantees everybody types
    the unlocked one -- the interlock bypassed BY CONSTRUCTION while appearing to exist.
    """

    def test_the_separator_is_EATEN_and_not_handed_to_the_installer(self) -> None:
        """Forwarding `--` verbatim made the installer report `Expected package name starting with
        an alphanumeric character` -- a message blaming the payload for a defect in the wrapper.
        """
        assert split_passthrough(['--wait', '--', '-e', '.']) == (['--wait'], ['-e', '.'])

    def test_a_leading_dash_payload_SURVIVES(self) -> None:
        """`nargs=REMAINDER` only starts capturing at the first unrecognised token, so a payload
        beginning with `-e` was an argparse error rather than a payload.
        """
        assert split_passthrough(['--wait', '--', '-e', '.', '--upgrade']) == (['--wait'], ['-e', '.', '--upgrade'])

    def test_without_a_separator_everything_is_ours(self) -> None:
        assert split_passthrough(['--wait']) == (['--wait'], [])
        assert split_passthrough([]) == ([], [])

    def test_an_empty_payload_after_the_separator_is_still_a_split(self) -> None:
        assert split_passthrough(['--wait', '--']) == (['--wait'], [])
