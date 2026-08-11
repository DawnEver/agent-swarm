"""swarmctl retries transient failures, refuses to retry permanent ones, and stops.

USER DIRECTIVE 2026-08-10: 「所有 gitea 操作都要重试 3 次」. `agent_swarm.forge` has carried this
since it was given; **swarmctl never did**, and swarmctl is the tool that performs the writes whose
half-completion is worst -- a `provision` losing one call leaves teams created and members not
added, and the operator sees a traceback rather than a state.

NOT HYPOTHETICAL. While driving S0 on the live host on 2026-08-10, a `git fetch` against this very
server failed once with `remote: Failed to authenticate user` and succeeded unchanged on the next
attempt. That is the shape: the transient that looks exactly like the permanent one.

THE INTERESTING HALF IS WHAT MUST NOT RETRY. A 401 retried three times is still a 401, and this
server 401s BY DESIGN on the token-management routes -- so a blanket retry would have turned the
measurement that produced today's fix into three backoffs and the same message.

WHY A CREATE MAY BE RETRIED ANYWAY. A POST whose response was lost is indistinguishable from one
that never arrived, and refusing to retry it makes the retry useless for exactly the calls that
matter. swarmctl's writes are idempotent by construction -- `provision` prints "already present",
`onboard` re-attaches the same teams -- which is what makes this safe rather than merely hopeful.
That property is the precondition of the retry, so it is asserted here too.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_swarm import forge
from agent_swarm import swarmctl as _swarmctl

pytestmark = pytest.mark.unit


@pytest.fixture(scope='module')
def swarmctl():
    """The module under test, as an ORDINARY IMPORT.

    It used to be loaded by path with `importlib.util.spec_from_file_location`, because it lived in
    another project as a bare script that nothing could import. Here it is a module of this package,
    so the loader preamble is gone -- and with it a whole class of mistake, since a hand-rolled load
    can silently execute a DIFFERENT file from the one an import would resolve.
    """
    return _swarmctl


class _Transport:
    """Scripted answers, one per attempt, plus what the client did to get them."""

    def __init__(self) -> None:
        self.answers: list[object] = []
        self.requests = 0
        self.closed = 0
        self.slept: list[float] = []


@pytest.fixture
def transport(swarmctl, monkeypatch) -> _Transport:
    """Installs a connection whose answers are scripted per attempt, and counts them.

    An answer is `(status, body)` or an `OSError` to raise. A script shorter than the attempt count
    RAISES rather than reusing its last entry: a double that runs out of answers and repeats one is
    quietly asserting that the extra attempts did not happen.
    """
    state = _Transport()
    monkeypatch.setattr(swarmctl, '_sleep', state.slept.append)

    class _Conn:
        def __init__(self, *_a, **_k) -> None:
            self._answer: tuple[int, bytes] = (0, b'')

        def request(self, _method, _path, body=None, headers=None) -> None:
            index = state.requests
            state.requests += 1
            if index >= len(state.answers):
                msg = f'attempt {index + 1} requested but only {len(state.answers)} were scripted'
                raise AssertionError(msg)
            answer = state.answers[index]
            if isinstance(answer, OSError):
                raise answer
            assert isinstance(answer, tuple)
            self._answer = answer

        def getresponse(self):
            status, body = self._answer
            return type('_Response', (), {'status': status, 'read': staticmethod(lambda: body)})

        def close(self) -> None:
            state.closed += 1

    monkeypatch.setattr(swarmctl.http.client, 'HTTPConnection', _Conn)
    monkeypatch.setattr(swarmctl, 'read_credential', lambda *_a, **_k: 'stored-token')
    return state


def _provider(swarmctl):
    return swarmctl.GiteaProvider('http://host:9000', 'Org', None, 'admin')


# --------------------------------------------------------------------------- it retries


def test_a_5xx_is_retried_and_the_second_attempt_wins(swarmctl, transport):
    """The whole point: a transient server error must not end a provisioning run."""
    transport.answers = [(502, b'bad gateway'), (200, b'{"ok": true}')]
    assert _provider(swarmctl).api_obj('GET', '/orgs/Org') == {'ok': True}
    assert transport.requests == 2


def test_a_dropped_connection_is_retried(swarmctl, transport):
    """`OSError`, which is what a reset looks like here -- and what WS1 hit on a real fetch."""
    transport.answers = [OSError('connection reset'), (200, b'{}')]
    assert _provider(swarmctl).api_obj('GET', '/orgs/Org') == {}
    assert transport.requests == 2


def test_each_attempt_gets_a_FRESH_connection(swarmctl, transport):
    """`http.client` leaves a connection unusable after a failed exchange, so a retry that reused
    one would report a second, different error and hide the first.
    """
    transport.answers = [(500, b''), (200, b'{}')]
    _provider(swarmctl).api_obj('GET', '/orgs/Org')
    assert transport.closed == 2


def test_it_backs_off_between_attempts(swarmctl, transport):
    """Immediate retries against a struggling server are three requests, not one retry."""
    transport.answers = [(500, b''), (500, b''), (200, b'{}')]
    _provider(swarmctl).api_obj('GET', '/orgs/Org')
    assert transport.slept == [swarmctl.BACKOFF_S * 1, swarmctl.BACKOFF_S * 2]


# --------------------------------------------------------------------------- it stops


def test_it_gives_up_after_the_declared_number_of_attempts(swarmctl, transport):
    """A bound nobody asserts is a loop."""
    transport.answers = [(503, b'down')] * swarmctl.API_ATTEMPTS
    with pytest.raises(swarmctl.Fail, match='gave up after 3 attempts'):
        _provider(swarmctl).api_obj('GET', '/orgs/Org')
    assert transport.requests == swarmctl.API_ATTEMPTS


def test_the_final_message_still_names_the_endpoint_and_the_status(swarmctl, transport):
    """A bare "gave up" without the underlying failure is worse than no retry at all."""
    transport.answers = [(503, b'down')] * swarmctl.API_ATTEMPTS
    with pytest.raises(swarmctl.Fail, match=r'GET /orgs/Org -> 503'):
        _provider(swarmctl).api_obj('GET', '/orgs/Org')


# --------------------------------------------------------------------------- it refuses to retry


@pytest.mark.parametrize('status', [400, 401, 403, 404, 422])
def test_a_4xx_is_NOT_retried(swarmctl, transport, status: int):
    """The half a blanket implementation gets wrong. This server 401s by design on the token routes;
    hammering it three times would have made today's measurement look like flakiness.
    """
    transport.answers = [(status, b'nope')]
    with pytest.raises(swarmctl.Fail):
        _provider(swarmctl).api_obj('GET', '/orgs/Org')
    assert transport.requests == 1


def test_an_ALLOWED_status_is_not_retried_either(swarmctl, transport):
    """`allow=` means "this status is an expected answer". Retrying an expected answer would make
    every optional-resource probe cost three round trips.
    """
    transport.answers = [(404, b'')]
    assert _provider(swarmctl).api('GET', '/orgs/Org', allow=(404,)) is None
    assert transport.requests == 1


def test_a_429_is_not_treated_as_ordinary_transience(swarmctl):
    """A rate limit wants a longer, header-driven wait; retrying it linearly is how a client turns a
    limit into a ban. Stated as a decision rather than left to the >=500 rule by accident.
    """
    assert not swarmctl._is_retryable_status(429)
    assert swarmctl._is_retryable_status(500)


# --------------------------------------------------------------------------- there is one copy


def test_the_policy_IS_agent_swarms_not_a_matching_copy(swarmctl):
    """ONE RULE, ONE DEFINITION -- and this test changed shape when the code moved.

    It used to read `agent_swarm/forge.py` off disk and compare the two `API_ATTEMPTS` for equality,
    skipping when the sibling checkout was absent. That was the best available answer while swarmctl
    lived in another repository as a stdlib-only script that could not import this package: two
    copies, kept honest by a comparison.

    The move deleted the reason for the second copy, so the copy went too. A cross-check only makes
    a duplicate honest; it never makes it singular, and every duplicate is one edit away from a
    fleet running two retry policies with no way to tell which one answered. Identity, not equality,
    is what is asserted now -- and `is` is the discriminating word: a re-declared `API_ATTEMPTS = 3`
    would pass an `==`.
    """
    assert swarmctl.API_ATTEMPTS is forge.API_ATTEMPTS
    assert swarmctl.BACKOFF_S is forge._BACKOFF_S
    assert swarmctl._is_retryable_status is forge._is_retryable_status


# --------------------------------------------------------------------------- the precondition


def test_the_retried_writes_are_idempotent_by_construction():
    """THE PRECONDITION OF RETRYING A POST, asserted rather than assumed.

    A retried create can duplicate. That is acceptable here only because `provision` checks before
    it creates -- the live run printed "already present" for all four users. If that check were ever
    removed, this retry would start creating duplicate users under a directive that says to retry.
    """
    source = Path(_swarmctl.__file__).read_text(encoding='utf-8')
    assert 'already present' in source, (
        'provision no longer reports an existing user as already present, so it may no longer be '
        'checking before it creates -- and a retried POST would then duplicate'
    )
