"""The commit status: what turns "gate green before main moves" into a mechanism.

Today that rule is carried by whoever remembers it, and this session alone has the receipts on how
that goes. A required status check is the SERVER refusing the merge instead of a human recalling a
convention -- and it also deletes the whole shared-checkout accident class, because landing stops
being "push when you believe it is green".

ORDER MATTERS AND IS NOT NEGOTIABLE: the status must exist BEFORE branch protection is enabled. A
rule waiting on a context nobody publishes freezes the branch behind a check that can never appear.
That is why this lands now, while the credentials to enable protection do not yet exist.

WHAT IS NOT ENFORCEABLE, said plainly rather than implied: Gitea has no scope for commit status --
writing one needs repository write, which any identity that pushes branches must already have. So
"only the verifier marks a commit green" is carried by credential distribution, NOT by the server.
GitHub can enforce it (an App may hold statuses:write without contents:write). The strongest check
available on our side is that our own code reaches `set_status` through one role.
"""

from __future__ import annotations

import pytest

from agent_swarm.forge import STATUS_STATES, ForgeError, GiteaForge
from agent_swarm.testing import RecordingForge


@pytest.fixture
def gitea():
    forge = GiteaForge('http://forge.test:9000', 'owner/repo', username='swarm-agent')
    sent: list[tuple[str, str, dict | None]] = []
    forge._api = lambda method, path, body=None: sent.append((method, path, body))  # type: ignore[method-assign]
    forge.sent = sent  # type: ignore[attr-defined]
    return forge


def test_the_status_is_posted_against_the_SHA(gitea):
    gitea.set_status('a' * 40, state='success', context='motronics/gate', description='PASS')
    method, path, body = gitea.sent[-1]
    assert method == 'POST'
    assert path.endswith(f'/statuses/{"a" * 40}')
    assert body == {'state': 'success', 'context': 'motronics/gate', 'description': 'PASS'}


@pytest.mark.parametrize('state', sorted(STATUS_STATES))
def test_every_declared_state_is_accepted(gitea, state):
    gitea.set_status('b' * 40, state=state, context='c', description='d')
    assert gitea.sent[-1][2]['state'] == state


def test_an_undeclared_state_is_REFUSED_before_any_io(gitea):
    """A typo'd state would be accepted by the vendor as an error status or rejected far away; both
    read as "the gate is broken". Refused here, where the caller can be named -- and before the
    request, so a rejected value leaves nothing behind."""
    with pytest.raises(ForgeError, match='state must be one of'):
        gitea.set_status('c' * 40, state='green', context='c', description='d')
    assert gitea.sent == []


def test_a_long_description_is_TRUNCATED_not_refused(gitea):
    """A gate summary is long. A status that failed to publish because its prose was verbose would
    block a merge for a reason no reader would connect to the text."""
    gitea.set_status('d' * 40, state='success', context='c', description='x' * 5000)
    assert len(gitea.sent[-1][2]['description']) <= 255


def test_the_context_is_sent_verbatim(gitea):
    """It must match the branch rule EXACTLY. Any normalisation here would silently publish a check
    the rule is not waiting on, and the branch would stay frozen with a green status visible."""
    gitea.set_status('e' * 40, state='success', context='Motronics/Gate', description='d')
    assert gitea.sent[-1][2]['context'] == 'Motronics/Gate'


# --------------------------------------------------------------------------- the double


def test_the_double_keeps_the_CURRENT_answer_per_context():
    """Last write wins, as a real forge does. An appending double would let a test assert a commit
    is green while a later failure sat behind it in a list nobody reads."""
    forge = RecordingForge()
    forge.set_status('f' * 40, state='success', context='gate', description='ok')
    forge.set_status('f' * 40, state='failure', context='gate', description='no')
    assert forge.statuses['f' * 40, 'gate'] == ('failure', 'no')


def test_the_double_keeps_contexts_APART():
    """Two checks on one commit are two independent answers; collapsing them would let one check
    satisfy a rule waiting on another."""
    forge = RecordingForge()
    forge.set_status('g' * 40, state='success', context='gate', description='ok')
    forge.set_status('g' * 40, state='pending', context='lint', description='running')
    assert forge.statuses['g' * 40, 'gate'][0] == 'success'
    assert forge.statuses['g' * 40, 'lint'][0] == 'pending'


def test_the_double_refuses_what_the_client_refuses():
    """A double more permissive than reality is how an invalid state reaches production green."""
    forge = RecordingForge()
    with pytest.raises(ForgeError):
        forge.set_status('h' * 40, state='green', context='gate', description='d')
