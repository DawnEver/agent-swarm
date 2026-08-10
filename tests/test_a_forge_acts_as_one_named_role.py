"""A forge client authenticates as ONE named role, and the query says which.

THE DEFECT. `_credential` asked `git credential fill` with `protocol` and `host` only. That helper
keys on (protocol, host, USERNAME), and this system deliberately stores FOUR credentials for one
host -- `swarm-observer`, `swarm-agent`, `swarm-verifier`, `swarm-integrator`. A query with no
username therefore returns whichever entry the helper happens to hold. Measured on the live host
2026-08-10: the bare-host key belonged to `OAUTH_USER`, an entry nothing in this system issued.

WHY IT IS A BOUNDARY FAILURE AND NOT A CONFIGURATION ONE. Gitea has no scope for commit status --
writing one needs repository write, which `swarm-agent` must have to push branches. `swarmctl`'s
role table says so in as many words: "only swarm-verifier marks a commit green" is carried by WHICH
PROCESS HOLDS WHICH CREDENTIAL, not by the server. So role selection in the client is not a
convenience on top of a server-side rule; it is the entire rule. A client that cannot select does
not weaken the boundary, it deletes it -- and every call still succeeds, so nothing says so.

THE SAME DEFECT, TWICE, ON BOTH SIDES OF ONE SEAM. `swarmctl` prints the remote URLs with
"THE USERNAME IS REQUIRED, not decoration" and stores each credential under its role name; the
consumer then looked it up without one. The producer was right and documented; the consumer threw
the distinction away, which is why this file asserts the QUERY rather than the outcome -- the
outcome is identical on a machine that happens to hold only one credential, which is every
development box.

WHAT IS NOT ASSERTED: that the server enforces anything. It does not, and pretending otherwise is
the thing the role table already refuses to do.
"""

from __future__ import annotations

import pytest

from agent_swarm.forge import ROLE_ACCOUNTS, ForgeError, GiteaForge, default_forge


class _Recording(GiteaForge):
    """Captures the credential-helper query instead of running git."""

    def __init__(self, username: str) -> None:
        super().__init__('http://127.0.0.1:1', 'o/r', username=username)
        self.queries: list[str] = []

    def _run_credential_helper(self, scheme: str, netloc: str, username: str):
        self.queries.append(f'protocol={scheme} host={netloc} username={username}')

        class _Result:
            stdout = 'password=secret\n'

        return _Result


# --------------------------------------------------------------------------- the query names the role


@pytest.mark.parametrize('username', sorted(ROLE_ACCOUNTS.values()))
def test_the_credential_query_carries_the_username(username: str):
    """THE DEFECT ITSELF. Without this the query is (protocol, host) and the answer is arbitrary."""
    forge = _Recording(username)
    forge._credential()
    assert forge.queries == [f'protocol=http host=127.0.0.1:1 username={username}']


def test_two_roles_ask_DIFFERENT_questions():
    """The discriminating half. A client that always sent the same username would satisfy the test
    above for whichever role it was hard-coded to, and silently be that one role forever.
    """
    verifier, agent = _Recording('swarm-verifier'), _Recording('swarm-agent')
    verifier._credential()
    agent._credential()
    assert verifier.queries != agent.queries


def test_the_query_is_asked_once_and_then_cached():
    """The cache is a cost optimisation and it must not have become per-call with the extra
    argument -- a credential subprocess per API call is a real cost on a 7x24 fleet.
    """
    forge = _Recording('swarm-agent')
    forge._credential()
    forge._credential()
    assert len(forge.queries) == 1


# --------------------------------------------------------------------------- it cannot be omitted


def test_a_forge_without_a_username_is_REFUSED():
    """Not defaulted. A default here would be a silent choice of role, which is the defect wearing
    a different spelling -- and the caller who forgot is exactly the caller who would not notice.
    """
    with pytest.raises(ForgeError, match='username is required'):
        GiteaForge('http://127.0.0.1:1', 'o/r', username='')


def test_the_username_is_keyword_only():
    """Positionally it would sit next to `repo`, two strings in a row, and a transposition would
    produce a client authenticating as `owner/name` -- which fails at the server, far from here.
    """
    with pytest.raises(TypeError):
        GiteaForge('http://127.0.0.1:1', 'o/r', 'swarm-agent')  # type: ignore[misc]


# --------------------------------------------------------------------------- the role table


def test_every_role_maps_to_its_own_account():
    """A mapping with a duplicate would make two roles one identity, which is the boundary gone
    again by another route.
    """
    assert len(set(ROLE_ACCOUNTS.values())) == len(ROLE_ACCOUNTS)


def test_default_forge_takes_a_role():
    assert default_forge('verifier').username == ROLE_ACCOUNTS['verifier']


def test_default_forge_refuses_an_unknown_role():
    """A typo must not become a fifth identity that nothing enrolled and nothing grants."""
    with pytest.raises(ForgeError, match='role must be one of'):
        default_forge('verifiers')


def test_the_default_role_is_the_least_privileged_one_that_can_work():
    """Stated as a decision rather than left to whichever name sorted first: the common caller is a
    worker, and a default of `integrator` would hand merge rights to every unconfigured process.
    """
    assert default_forge().username == ROLE_ACCOUNTS['agent']


def test_the_missing_credential_message_names_the_role_and_the_fix():
    """`no stored credential for host` sent the last reader looking at the server. The role and
    `swarmctl enroll` are what actually resolve it.
    """
    forge = GiteaForge('http://127.0.0.1:1', 'o/r', username='swarm-verifier')
    forge._run_credential_helper = lambda *_a: type('_R', (), {'stdout': ''})  # type: ignore[method-assign]
    with pytest.raises(ForgeError) as caught:
        forge._credential()
    assert 'swarm-verifier' in str(caught.value)
    assert 'enroll' in str(caught.value)
