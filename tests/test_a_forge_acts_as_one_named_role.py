r"""A forge client authenticates as ONE named role, and the token it gets back proves which.

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

HOW THE FIRST ATTEMPT AT THIS FILE FAILED, and the lesson outlived the implementation. It subclassed
`GiteaForge`, OVERRODE the credential seam with a recorder that built a query string containing the
username, and asserted the username was in it. It was asserting the recorder. The real code still
sent protocol and host only, so on the measured machine it resolved `swarm-verifier` and the live
contract test 403'd with `token scope=write:repository` -- the verifier's scope set, named in its own
error -- while 556 unit tests passed.

That is the "test monkeypatches away the code under test" shape. The seam exists so a COST test can
COUNT calls, which an override serves correctly; asserting the CONTENT of a lookup is not that,
because the content is precisely what an override replaces. So these tests stay BELOW the seam.

WHAT "BELOW THE SEAM" MEANS NOW, and it is a stronger position than before. The old tests intercepted
`subprocess.run` and asserted the bytes going to git's stdin -- the best available proxy, since what
git did with those bytes was somebody else's store. The role tokens now live in the SWARM's own
store, so these tests write four real tokens into a real store and assert WHICH ONE COMES BACK. That
is the actual property -- the identity selected -- rather than a query that ought to select it.

WHAT IS NOT ASSERTED: that the server enforces anything. It does not, and pretending otherwise is
what the role table already refuses to do.
"""

from __future__ import annotations

import subprocess

import pytest

from agent_swarm import credentials
from agent_swarm.forge import ROLE_ACCOUNTS, ForgeError, GiteaForge, default_forge

#: Any repo. The point of these tests is the ROLE, and the package no longer supplies a project.
REPO = 'owner/name'

_HOST = '127.0.0.1:1'


@pytest.fixture
def enrolled(tmp_path, monkeypatch):
    """A REAL swarm credential store holding a DISTINCT token per role.

    Distinct on purpose: a store where every role held the same secret would return the right string
    for the wrong reason, and every assertion below would pass for a client hard-coded to one role.
    """
    store = tmp_path / 'credentials.json'
    monkeypatch.setattr(credentials, 'store_path', lambda: store)
    # The environment must not answer instead of the store, or these test the override path.
    for username in ROLE_ACCOUNTS.values():
        monkeypatch.delenv(credentials.env_var_for(username), raising=False)
        credentials.store_token('http', _HOST, username, f'token-for-{username}', path=store)
    return store


def _forge(username: str) -> GiteaForge:
    return GiteaForge(f'http://{_HOST}', 'o/r', username=username)


# --------------------------------------------------------------------------- one role, one identity


@pytest.mark.parametrize('username', sorted(ROLE_ACCOUNTS.values()))
def test_the_token_returned_is_THAT_ROLES(username: str, enrolled):
    """THE DEFECT ITSELF, asserted on the identity selected rather than on a query that ought to."""
    assert _forge(username)._credential() == f'token-for-{username}'


def test_two_roles_get_DIFFERENT_tokens(enrolled):
    """The discriminating half. A client hard-coded to one role would satisfy the test above for
    that role and silently be it forever.
    """
    assert _forge('swarm-verifier')._credential() != _forge('swarm-agent')._credential()


def test_the_lookup_happens_once_and_is_then_cached(enrolled, monkeypatch):
    """The cache is a cost optimisation and must not have become per-call -- a credential lookup per
    API call is a real cost on a 7x24 fleet.
    """
    calls = []
    real = credentials.resolve_token
    monkeypatch.setattr(credentials, 'resolve_token', lambda *a, **k: calls.append(a) or real(*a, **k))
    forge = _forge('swarm-agent')
    forge._credential()
    forge._credential()
    assert len(calls) == 1


def test_NO_SUBPROCESS_IS_SPAWNED_AT_ALL(enrolled, monkeypatch):
    """This replaces `the terminal prompt stays disabled`, and the property got STRONGER.

    The old code ran `git credential fill`, which falls back to an interactive prompt when nothing
    matches -- on Windows a GUI dialog that hangs an unattended runner and invites somebody to type a
    human credential into a fleet process. That was suppressed with `GIT_TERMINAL_PROMPT=0`, i.e.
    guarded. Reading the swarm's own store executes no git at all, so the hazard is ABSENT rather
    than suppressed, and this asserts the absence.
    """
    monkeypatch.setattr(subprocess, 'run', lambda *a, **k: pytest.fail('the credential path spawned a subprocess'))
    assert _forge('swarm-agent')._credential() == 'token-for-swarm-agent'


def test_the_operators_credential_store_is_NOT_consulted(tmp_path, monkeypatch):
    """THE READ DIRECTION OF THE 2026-08-11 REPAIR, and the more dangerous of the two.

    A write to the ambient store clobbers an identity -- bad, and at least a change. A READ from it
    SUCCEEDS as whoever the vault happens to hold: measured on the live host, the bare-host key
    belonged to `OAUTH_USER`, an entry nothing in this system issued, and every call worked.

    So an un-enrolled machine must RAISE rather than fall back. A fallback would preserve exactly
    that hazard and announce it in a log nobody reads on a run that returned 200.
    """
    monkeypatch.setattr(credentials, 'store_path', lambda: tmp_path / 'absent.json')
    monkeypatch.delenv(credentials.env_var_for('swarm-agent'), raising=False)
    monkeypatch.setattr(subprocess, 'run', lambda *a, **k: pytest.fail('it fell back to the ambient credential store'))
    with pytest.raises(ForgeError, match='NOT consulted'):
        _forge('swarm-agent')._credential()


def test_the_environment_can_supply_the_token_without_any_store(tmp_path, monkeypatch):
    """The explicit, per-invocation path -- how a fleet operation carries its role without changing
    what any other process on the box authenticates as.
    """
    monkeypatch.setattr(credentials, 'store_path', lambda: tmp_path / 'absent.json')
    monkeypatch.setenv(credentials.env_var_for('swarm-agent'), 'from-the-environment')
    assert _forge('swarm-agent')._credential() == 'from-the-environment'


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


def test_the_missing_credential_message_names_the_role_and_BOTH_fixes(tmp_path, monkeypatch):
    """`no stored credential for <host>` sent the last reader looking at the server. The ROLE and
    `swarmctl enroll` are what actually resolve it -- and now so does the environment variable, which
    is the only remedy available to someone who cannot re-enrol the machine they are on.
    """
    monkeypatch.setattr(credentials, 'store_path', lambda: tmp_path / 'absent.json')
    monkeypatch.delenv(credentials.env_var_for('swarm-verifier'), raising=False)
    with pytest.raises(ForgeError) as caught:
        _forge('swarm-verifier')._credential()
    message = str(caught.value)
    assert 'swarm-verifier' in message
    assert 'enroll' in message
    assert 'SWARM_TOKEN_VERIFIER' in message
