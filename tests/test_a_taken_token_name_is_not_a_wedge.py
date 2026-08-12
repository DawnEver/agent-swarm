"""A token name already taken on the SERVER is a NAMED, RECOVERABLE state -- not a wedge.

MEASURED 2026-08-12 on the Gitea host: `swarmctl list` failed with Gitea's raw
`access token name has been used already`. The box was in the one state `token()`'s self-heal does
NOT cover -- present on the SERVER, absent from the local store -- which a cleared credential store,
a re-imaged machine, or a mint whose store write was lost all produce.

WHY IT IS WORSE THAN A PLAIN ERROR: every subsequent run tries the same name and fails identically,
so the machine is WEDGED PERMANENTLY, and the token value is unrecoverable (Gitea shows it once).
The operator saw a vendor error naming a token they cannot see, with no route from there to a
working box. The mirror-image state (stored locally, revoked server-side) has had a documented
self-heal since 2026-08-10; this one had nothing.
"""

from __future__ import annotations

import argparse

import pytest

from agent_swarm import swarmctl

pytestmark = pytest.mark.unit


class _Provider(swarmctl.GiteaProvider):
    def __init__(self, stderr: str) -> None:
        self._stderr = stderr
        self.exe = 'gitea'
        self.scheme = 'http'
        self.netloc = 'forge.invalid:9000'

    def _cli(self, *args: str) -> str:
        msg = f'gitea {" ".join(args[:3])} failed:\n{self._stderr}'
        raise swarmctl.Fail(msg)


def test_a_taken_name_names_the_remedy_and_the_token():
    provider = _Provider('access token name has been used already')
    with pytest.raises(swarmctl.Fail) as caught:
        provider.issue_token('admin', 'swarmctl-admin@BOX', ['write:admin'])
    text = str(caught.value)
    assert 'swarmctl revoke --token-name swarmctl-admin@BOX' in text, f'no runnable remedy: {text}'
    assert 'NOT a login failure' in text, 'it does not rule out the wrong diagnosis'
    assert 'cannot be recovered' in text, 'it does not say the value is unrecoverable'


def test_ANY_OTHER_cli_failure_is_passed_through_unchanged():
    """THE CONTROL, and the direction that costs evidence. A blanket `except Fail` that relabelled
    every mint failure as a name collision would send an operator to revoke a token over what was
    actually a permissions error or a dead server."""
    provider = _Provider('user does not exist')
    with pytest.raises(swarmctl.Fail, match='user does not exist'):
        provider.issue_token('nobody', 'swarmctl-admin@BOX', ['write:admin'])


class TestVerifyCannotReportNoProblemsWithoutLooking:
    """MEASURED 2026-08-12 on the Gitea host: `verify` hit the no-admin-credential arm, printed
    `configuration problems: none`, and EXITED 0 -- having read no team, no membership and no
    branch protection. Three quarters of what it claims to verify were never looked at.

    The `not checked from here` line was already printed and changed nothing. **A LOG IS NOT A
    SIGNAL; the test is whether the CALLER can tell**, and a caller reads the exit code. An agent
    or script wiring `verify` into a launch check would have been told the fleet was configured.
    """

    def test_a_skipped_check_exits_INCOMPLETE_rather_than_zero(self, monkeypatch, capsys):
        provider = _Provider('no admin credential stored')
        monkeypatch.setattr(provider, 'teams', lambda: (_ for _ in ()).throw(swarmctl.Fail('no credential')))
        monkeypatch.setattr(swarmctl, 'read_credential', lambda *_a, **_k: None)

        code = swarmctl.cmd_verify(provider, argparse.Namespace(repo='o/r'))

        assert code == swarmctl.EXIT_INCOMPLETE, f'a run that read nothing exited {code}'
        out = capsys.readouterr().out
        assert 'INCOMPLETE' in out
        assert 'This is not a pass' in out
        assert 'NOT read' in out, 'it does not say WHAT went unchecked'

    def test_the_three_values_are_DISTINCT(self):
        """The control. INCOMPLETE collapsing onto either neighbour is the whole defect, so the
        constants must not collide -- a `2` that equalled `0` would restore the bug silently."""
        assert swarmctl.EXIT_INCOMPLETE not in (0, 1)
