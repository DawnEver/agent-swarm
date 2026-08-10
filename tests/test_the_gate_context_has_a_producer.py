"""The `motronics/gate` status a protected branch waits on is actually produced by something.

WHAT WAS MISSING. `set_status` shipped, was measured against a real Gitea, and was called by
NOTHING. Verdicts went into work items and stopped there, so the context a protected branch would
require had no producer. Turning protection on in that state freezes `main`: every merge waits for a
check nobody publishes, and the symptom -- merges hanging -- looks like a broken gate rather than an
absent one.

That is this project's own named shape, "a flag existing is not a runner running", landing on the
merge path. So the tests here are about the PRODUCER existing and being reachable only from the
right identity, not about the HTTP call, which `test_set_status.py` already covers.

THE MAPPING IS THE INTERESTING PART. Three verdicts, three states, and the one worth arguing about
is INCONCLUSIVE -- see `VERDICT_STATES`. `failure` would claim the change is bad when nobody found
out; `pending` would leave the merge waiting on a run that already finished, which is
indistinguishable from a dead runner. All three block, deliberately: a merge must not proceed on no
information, and that direction costs a re-run while the other costs a bad main.
"""

from __future__ import annotations

import pytest

from agent_swarm.forge import ROLE_ACCOUNTS, ForgeError, GiteaForge
from agent_swarm.status import STATUS_CONTEXT, VERDICT_STATES, StatusPublisher
from agent_swarm.store import VERDICTS


class _Recording:
    """A forge that records statuses. `username` is what the publisher checks."""

    def __init__(self, username: str = ROLE_ACCOUNTS['verifier']) -> None:
        self.username = username
        self.published: list[tuple[str, str, str, str]] = []

    def set_status(self, sha: str, *, state: str, context: str, description: str) -> None:
        self.published.append((sha, state, context, description))


# --------------------------------------------------------------------------- there IS a producer


def test_a_verdict_becomes_a_commit_status():
    """THE GAP ITSELF: before this, nothing turned a verdict into the check a merge waits for."""
    forge = _Recording()
    StatusPublisher(forge).publish('abc123', verdict='PASS', detail='gate green')
    assert forge.published == [('abc123', 'success', STATUS_CONTEXT, 'gate green')]


@pytest.mark.parametrize('verdict', sorted(VERDICTS))
def test_every_verdict_has_a_state(verdict: str):
    """A verdict with no mapping would raise a KeyError at publish time -- on the merge path, after
    the gate has already run, which is the most expensive place to discover it.
    """
    forge = _Recording()
    StatusPublisher(forge).publish('abc123', verdict=verdict, detail='d')
    assert forge.published[0][1] == VERDICT_STATES[verdict]


def test_inconclusive_is_error_and_not_failure():
    """`failure` would claim the change is bad. It is not -- nobody found out. A human reading a red
    branch would go hunting a defect that does not exist.
    """
    assert VERDICT_STATES['INCONCLUSIVE'] == 'error'


def test_no_verdict_maps_to_a_state_that_lets_a_merge_through():
    """The safety property, asserted over the whole mapping rather than per word: only `success`
    admits a merge, and only PASS may map to it.
    """
    assert [word for word, state in VERDICT_STATES.items() if state == 'success'] == ['PASS']


def test_the_context_matches_the_one_branch_protection_requires():
    """Two spellings of one name is two definitions of one fact, and the failure -- a branch
    protected against a check nobody publishes -- is exactly what this file exists to prevent.
    `swarmctl`'s STATUS_CONTEXT is the other copy; they must not drift.
    """
    assert STATUS_CONTEXT == 'motronics/gate'


# --------------------------------------------------------------------------- only the verifier


def test_a_non_verifier_forge_is_REFUSED_at_construction():
    """MEASURED 2026-08-10: publishing a status as `swarm-agent` SUCCEEDS on the real server --
    Gitea has no scope for commit status. So the server will not refuse this, and this seam is the
    only place the role can be checked at all.
    """
    with pytest.raises(ForgeError, match='verifier'):
        StatusPublisher(_Recording(username=ROLE_ACCOUNTS['agent']))


def test_the_verifier_forge_is_accepted():
    """The discriminating half: a check that refused everything would leave the context with no
    producer again, which is the defect this file is about.
    """
    assert StatusPublisher(_Recording()).forge.username == ROLE_ACCOUNTS['verifier']


def test_a_forge_with_no_username_at_all_is_refused():
    """A `Forge` implementation that never carried an identity would otherwise slip through the
    `getattr` and publish as whoever the credential helper happened to hand back.
    """
    with pytest.raises(ForgeError):
        StatusPublisher(object())  # type: ignore[arg-type]


def test_the_refusal_happens_before_any_publish():
    """Constructed-then-refused is the point: the mistake surfaces where the forge was built, not at
    the one call per job that publishes -- by which time a wrong-identity status is already on a
    commit, and Gitea cannot delete one.
    """
    forge = _Recording(username=ROLE_ACCOUNTS['integrator'])
    with pytest.raises(ForgeError):
        StatusPublisher(forge)
    assert forge.published == []


# --------------------------------------------------------------------------- it refuses bad input


def test_an_unknown_verdict_raises_BEFORE_any_io():
    """A commit status cannot be deleted on Gitea, so a publisher that validated afterwards would
    have already written one for a word it then rejected.
    """
    forge = _Recording()
    with pytest.raises(ValueError, match='verdict must be one of'):
        StatusPublisher(forge).publish('abc123', verdict='GREEN', detail='d')
    assert forge.published == []


def test_an_empty_sha_raises():
    """A status needs a COMMIT. An empty sha would 404 far from here, and the caller who forgot to
    thread one through is the one who needs to hear about it.
    """
    forge = _Recording()
    with pytest.raises(ValueError, match='commit sha'):
        StatusPublisher(forge).publish('', verdict='PASS', detail='d')
    assert forge.published == []


def test_a_real_gitea_forge_satisfies_the_publisher():
    """The seam is checked against the shipping client, not only the double -- a publisher that only
    accepted the test stand-in would be untested against the thing it actually runs on.
    """
    forge = GiteaForge('http://127.0.0.1:1', 'o/r', username=ROLE_ACCOUNTS['verifier'])
    assert StatusPublisher(forge).context == STATUS_CONTEXT
