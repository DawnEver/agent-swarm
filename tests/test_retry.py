"""Every Gitea call retries three times -- and the interesting half is what it must NOT retry.

USER DIRECTIVE 2026-08-10: 「所有 gitea 操作都要重试 3 次」. Taken literally a retry loop is four
lines. Taken correctly it has three separate decisions in it, and two of them are about NOT
retrying:

* **5xx and connection errors retry.** These are the transient failures a retry exists for, and the
  one that matters most is a request that SUCCEEDED server-side whose response was lost -- from the
  client there is nothing to distinguish it from a request that never arrived.

* **4xx does NOT retry.** A 401 retried three times is still a 401. Retrying turns one clear
  credential error into the same error after three backoffs, which is strictly worse: it costs time
  and it makes a permanent condition look like a flaky one. Measured 2026-08-10: the stored Gitea
  credential stopped being accepted mid-session and every call returned 401 -- exactly the shape a
  blanket retry would have obscured.

* **A retried CREATE can duplicate, and that is accepted rather than prevented.** POST is retried,
  because a lost response is the case retry exists for. The cost is that a create which succeeded
  and lost its response is issued twice. That is not hypothetical -- it is the duplicate-work-item
  hazard this package already answers, and it is answered by ARBITRATION rather than by refusing to
  retry: work items resolve by lowest number, claims by lowest comment id, and
  `ForgeStore.reconcile_duplicates` retires the losers off the hot path. So the honest design is a
  retry that CAN duplicate over a create path that survives duplicates -- not a retry that pretends
  it cannot.

WHAT THESE TESTS PIN, and why each direction is here: the retry happens (a suite that only asserts
the failure path passes against a client that never retries), the retry STOPS (a bound nobody
asserts is a loop), the 4xx refusal (the half a blanket implementation gets wrong), and that no
credential reaches a message on any path -- the failure path is the one that renders only when
something has already gone wrong, so it is never seen in a green run.
"""

from __future__ import annotations

import email.message
import urllib.error

import pytest

from agent_swarm.forge import API_ATTEMPTS, ForgeError, GiteaForge

#: A stand-in with no resemblance to a credential, asserted absent from every failure message.
_TOKEN = 'tok-' + 'x' * 80


def _forge(monkeypatch) -> GiteaForge:
    forge = GiteaForge(base_url='http://forge.invalid', repo='o/r')
    monkeypatch.setattr(forge, '_credential', lambda: _TOKEN)
    return forge


def _no_sleep(monkeypatch) -> list[float]:
    """Record backoffs instead of taking them, so a bound is testable in milliseconds."""
    slept: list[float] = []
    monkeypatch.setattr('agent_swarm.forge._sleep', slept.append)
    return slept


def _responder(monkeypatch, outcomes: list):
    """Serve `outcomes` in order; each is an exception to raise or a bytes body to return."""
    calls: list[tuple[str, str]] = []

    class _Response:
        def __init__(self, raw: bytes) -> None:
            self._raw = raw

        def read(self) -> bytes:
            return self._raw

        def __enter__(self):
            return self

        def __exit__(self, *_exc) -> bool:
            return False

    def fake_urlopen(request, timeout=None):
        calls.append((request.get_method(), request.full_url))
        outcome = outcomes[len(calls) - 1]
        if isinstance(outcome, Exception):
            raise outcome
        return _Response(outcome)

    monkeypatch.setattr('agent_swarm.forge.urllib.request.urlopen', fake_urlopen)
    return calls


def _http_error(code: int) -> urllib.error.HTTPError:
    """A real `HTTPError`, not a stand-in: `_api` calls `.read()` on it, so a double that only
    carried a code would pass these tests and fail against the vendor.
    """
    return urllib.error.HTTPError('http://forge.invalid', code, 'boom', email.message.Message(), None)


class TestItRetries:
    def test_a_5xx_is_retried_and_the_call_succeeds(self, monkeypatch):
        """THE REGRESSION IF RETRY IS ABSENT. Two transient failures then a body."""
        _no_sleep(monkeypatch)
        calls = _responder(monkeypatch, [_http_error(502), _http_error(503), b'{"ok": true}'])

        result = _forge(monkeypatch)._api('GET', '/x')

        assert result == {'ok': True}
        assert len(calls) == 3, 'the call did not retry'

    def test_a_connection_error_is_retried(self, monkeypatch):
        """A dropped connection is indistinguishable from a lost response, which is the case
        retry exists for -- so it must be retried on the same terms as a 5xx.
        """
        _no_sleep(monkeypatch)
        calls = _responder(monkeypatch, [urllib.error.URLError('refused'), b'{"ok": 1}'])

        assert _forge(monkeypatch)._api('GET', '/x') == {'ok': 1}
        assert len(calls) == 2

    def test_a_POST_is_retried_TOO(self, monkeypatch):
        """DELIBERATE, and the docstring above is the argument. A create whose response was lost
        is the case retry exists for; the duplicate it can produce is answered by arbitration.
        Excluding POST would make the retry useless for exactly the calls that matter.
        """
        _no_sleep(monkeypatch)
        calls = _responder(monkeypatch, [_http_error(500), b'{"number": 7}'])

        assert _forge(monkeypatch)._api('POST', '/x', {'title': 't'}) == {'number': 7}
        assert [m for m, _ in calls] == ['POST', 'POST']


class TestItStops:
    def test_it_gives_up_after_exactly_API_ATTEMPTS(self, monkeypatch):
        """A RETRY WITHOUT A BOUND IS A LOOP. Asserting the count, not just the raise: a suite
        that only checks `pytest.raises` passes against an implementation that never stops.
        """
        _no_sleep(monkeypatch)
        calls = _responder(monkeypatch, [_http_error(500)] * (API_ATTEMPTS + 3))

        with pytest.raises(ForgeError) as caught:
            _forge(monkeypatch)._api('GET', '/x')

        assert len(calls) == API_ATTEMPTS
        assert str(API_ATTEMPTS) in str(caught.value), 'the failure does not say how many it tried'

    def test_it_backs_off_between_attempts(self, monkeypatch):
        """Immediate retries against a struggling server are three requests, not one retry."""
        slept = _no_sleep(monkeypatch)
        _responder(monkeypatch, [_http_error(500)] * API_ATTEMPTS)

        with pytest.raises(ForgeError):
            _forge(monkeypatch)._api('GET', '/x')

        assert len(slept) == API_ATTEMPTS - 1, 'backoff count must be attempts minus one'
        assert slept == sorted(slept), 'the backoff must not shrink'


class TestItRefusesToRetryA4xx:
    @pytest.mark.parametrize('code', [400, 401, 403, 404, 422])
    def test_a_4xx_is_raised_on_the_FIRST_attempt(self, monkeypatch, code):
        """THE HALF A BLANKET IMPLEMENTATION GETS WRONG.

        Measured 2026-08-10: the stored Gitea credential stopped being accepted mid-session and
        every call returned 401. Retrying that three times yields the same 401 after three
        backoffs -- it costs time and it makes a permanent condition look transient, which is the
        reading that sends someone hunting for a flake instead of reissuing a token.
        """
        _no_sleep(monkeypatch)
        calls = _responder(monkeypatch, [_http_error(code)] * 5)

        with pytest.raises(ForgeError):
            _forge(monkeypatch)._api('GET', '/x')

        assert len(calls) == 1, f'{code} was retried; a 4xx does not become a 2xx by repetition'


class TestNoCredentialReachesAMessage:
    """The failure path renders ONLY when something has already gone wrong, so it is never seen in
    a green run -- which is why it is asserted rather than reviewed. Hard project invariant: never
    log license keys, tokens or fingerprints.
    """

    def test_the_exhaustion_message_carries_no_token(self, monkeypatch):
        _no_sleep(monkeypatch)
        _responder(monkeypatch, [_http_error(500)] * API_ATTEMPTS)

        with pytest.raises(ForgeError) as caught:
            _forge(monkeypatch)._api('GET', '/x')

        assert _TOKEN not in str(caught.value)

    def test_the_4xx_message_carries_no_token(self, monkeypatch):
        _no_sleep(monkeypatch)
        _responder(monkeypatch, [_http_error(401)] * 2)

        with pytest.raises(ForgeError) as caught:
            _forge(monkeypatch)._api('GET', '/x')

        assert _TOKEN not in str(caught.value)


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__]))
