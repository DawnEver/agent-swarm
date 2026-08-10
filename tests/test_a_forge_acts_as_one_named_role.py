r"""A forge client authenticates as ONE named role, and the query git receives says which.

THE DEFECT. `_credential` asked `git credential fill` with `protocol` and `host` only. That helper
keys on (protocol, host, USERNAME), and this system deliberately stores FOUR credentials for one
host -- `swarm-observer`, `swarm-agent`, `swarm-verifier`, `swarm-integrator`. A query with no
username returns whichever entry the helper happens to hold.

WHY IT IS A BOUNDARY FAILURE AND NOT A CONFIGURATION ONE. Gitea has no scope for commit status --
writing one needs repository write, which `swarm-agent` must have to push branches. `swarmctl`'s
role table says so in as many words: "only swarm-verifier marks a commit green" is carried by WHICH
PROCESS HOLDS WHICH CREDENTIAL, not by the server. Role selection in the client is not a convenience
on top of a server-side rule; it IS the rule. A client that cannot select does not weaken the
boundary, it deletes it -- and every call still succeeds, so nothing says so.

HOW THE FIRST ATTEMPT AT THIS FILE FAILED, which is why it now reaches `subprocess`. It subclassed
`GiteaForge`, OVERRODE `_run_credential_helper` with a recorder that built a query string containing
the username, and asserted the username was in it. It was asserting the recorder. The real helper
still sent protocol and host only; the fill stayed host-only and on the measured machine returned
`swarm-verifier`, so the live contract test 403'd with `token scope=write:repository` -- the
verifier's scope set, named in its own error -- while 556 unit tests passed.

That is the "test monkeypatches away the code under test" shape, and the seam made it easy:
`_run_credential_helper` exists so a COST test can COUNT calls, which an override serves correctly.
Asserting the CONTENT of the query is not that -- the content is precisely what an override
replaces. So these tests intercept `subprocess.run`, one layer below the seam, where the bytes going
to git's stdin are the real ones.

WHAT IS NOT ASSERTED: that the server enforces anything. It does not, and pretending otherwise is
what the role table already refuses to do.
"""

from __future__ import annotations

import subprocess

import pytest

from agent_swarm.forge import ROLE_ACCOUNTS, ForgeError, GiteaForge, default_forge

#: Any repo. The point of these tests is the ROLE, and the package no longer supplies a project.
REPO = 'owner/name'


@pytest.fixture
def git_stdin(monkeypatch) -> list[str]:
    """Every string the real code hands to `git credential fill`, captured BELOW the seam."""
    seen: list[str] = []

    def run(argv, *, input, **_kwargs):  # noqa: A002 - subprocess's own keyword
        assert argv[:3] == ['git', 'credential', 'fill'], argv
        seen.append(input)
        return subprocess.CompletedProcess(argv, 0, stdout='password=secret\n', stderr='')

    monkeypatch.setattr(subprocess, 'run', run)
    return seen


def _forge(username: str) -> GiteaForge:
    return GiteaForge('http://127.0.0.1:1', 'o/r', username=username)


# --------------------------------------------------------------------------- git is asked for a role


@pytest.mark.parametrize('username', sorted(ROLE_ACCOUNTS.values()))
def test_git_is_actually_asked_for_that_username(username: str, git_stdin: list[str]):
    """THE DEFECT ITSELF, asserted on the bytes rather than on a stand-in."""
    _forge(username)._credential()
    assert git_stdin == [f'protocol=http\nhost=127.0.0.1:1\nusername={username}\n\n']


def test_two_roles_send_DIFFERENT_bytes(git_stdin: list[str]):
    """The discriminating half. A client hard-coded to one role would satisfy the test above for
    that role and silently be it forever.
    """
    _forge('swarm-verifier')._credential()
    _forge('swarm-agent')._credential()
    assert git_stdin[0] != git_stdin[1]


def test_the_query_is_asked_once_and_then_cached(git_stdin: list[str]):
    """The cache is a cost optimisation and must not have become per-call with the extra argument --
    a credential subprocess per API call is a real cost on a 7x24 fleet.
    """
    forge = _forge('swarm-agent')
    forge._credential()
    forge._credential()
    assert len(git_stdin) == 1


def test_the_terminal_prompt_stays_disabled(monkeypatch):
    """A fill that cannot match must FAIL, not open a prompt on an unattended runner. This matters
    more after the fix than before it: a username makes a MISS possible where host-only would have
    matched something.
    """
    captured: dict = {}

    def run(argv, *, input, **kwargs):  # noqa: A002
        captured.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, stdout='password=x\n', stderr='')

    monkeypatch.setattr(subprocess, 'run', run)
    _forge('swarm-agent')._credential()
    assert captured['env']['GIT_TERMINAL_PROMPT'] == '0'


# --------------------------------------------------------------------------- it cannot be omitted


def test_a_forge_without_a_username_is_REFUSED():
    """Not defaulted. A default here would be a silent choice of role -- the defect wearing a
    different spelling -- and the caller who forgot is the one who would not notice.
    """
    with pytest.raises(ForgeError, match='username is required'):
        GiteaForge('http://127.0.0.1:1', 'o/r', username='')


def test_the_username_is_keyword_only():
    """Positionally it would sit beside `repo`, two strings in a row, and a transposition would
    authenticate as `owner/name` -- failing at the server, far from here.
    """
    with pytest.raises(TypeError):
        GiteaForge('http://127.0.0.1:1', 'o/r', 'swarm-agent')  # type: ignore[misc]


# --------------------------------------------------------------------------- the role table


def test_every_role_maps_to_its_own_account():
    """A duplicate would make two roles one identity -- the boundary gone again by another route."""
    assert len(set(ROLE_ACCOUNTS.values())) == len(ROLE_ACCOUNTS)


def test_default_forge_takes_a_role():
    assert default_forge('verifier', repo=REPO).username == ROLE_ACCOUNTS['verifier']


def test_default_forge_refuses_an_unknown_role():
    """A typo must not become a fifth identity that nothing enrolled and nothing grants."""
    with pytest.raises(ForgeError, match='role must be one of'):
        default_forge('verifiers', repo=REPO)


def test_the_default_role_is_the_least_privileged_one_that_can_work():
    """A decision, not whichever name sorted first: the common caller is a worker, and a default of
    `integrator` would hand merge rights to every unconfigured process.
    """
    assert default_forge(repo=REPO).username == ROLE_ACCOUNTS['agent']


def test_the_missing_credential_message_names_the_role_and_the_fix(monkeypatch):
    """`no stored credential for <host>` sent the last reader looking at the server. The ROLE and
    `swarmctl enroll` are what actually resolve it.
    """
    monkeypatch.setattr(
        subprocess, 'run', lambda argv, **_k: subprocess.CompletedProcess(argv, 1, stdout='', stderr='')
    )
    with pytest.raises(ForgeError) as caught:
        _forge('swarm-verifier')._credential()
    assert 'swarm-verifier' in str(caught.value)
    assert 'enroll' in str(caught.value)
