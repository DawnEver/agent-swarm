"""Issuing a token must not require running code on the Gitea host.

THE CONSTRAINT THAT FORCED THIS, user directive 2026-08-13: the forge moved to a dedicated Linux
server that runs Gitea and NOTHING ELSE. `issue_token` had exactly one leg -- `gitea admin user
generate-access-token` -- so `emit` refused with "this verb needs the Gitea CLI, which only exists on
the Gitea host [...] Creating users and minting tokens has no API path -- run it there."

THAT LAST CLAUSE WAS THE DEFECT: a declaration asserting a property of the world that was never
re-measured. `cmd_emit`'s own docstring already knew better -- "issuing a token needs either this
host's CLI **or the user's password**" -- and the password half was documented and never implemented.

MEASURED against the live forge, Gitea 1.27.1, before any of this was written:

    admin token -> POST /users/<other>/tokens              401 auth required
    admin token -> POST /users/<other>/tokens?sudo=<other> 401 auth required
    admin token -> GET  /admin/users                       200
    admin token -> PATCH /admin/users/<u> {password}       200
    basic <u>   -> POST /users/<u>/tokens                  201  MINTED
    basic <u>   -> DELETE /users/<u>/tokens/<name>         204

So the token-management surface refuses TOKEN auth outright -- 401, not 403, and not a scope
message. It is a circularity guard, not a permission. But an admin token may SET a password, and
that password may then mint. The route exists; only the implementation was missing.

THE PASSWORD IS EPHEMERAL BY DESIGN, and that is the part worth defending. It is generated per
issue, used within the same call, and never returned, stored or logged -- so nobody ends up holding
a long-lived password for a service account, and the automation needs no human secret at all. The
account is left with a strong unknown password, reachable again only by the same admin route, which
is the route this takes anyway.
"""

from __future__ import annotations

import pytest

from agent_swarm.swarmctl import GiteaProvider


class _Recorder:
    """A provider whose HTTP layer is a list. The point is WHICH calls happen, with WHICH identity."""

    def __init__(self, minted: str = 'the-new-token') -> None:
        self.calls: list[tuple] = []
        self.minted = minted

    def __call__(self, method, path, body=None, *, allow=(), auth='token', raw_token=None, raw_basic=None):
        self.calls.append((method, path, auth, raw_basic[0] if raw_basic else None, dict(body or {})))
        if method == 'POST' and path.endswith('/tokens'):
            return {'sha1': self.minted}
        return None


def _provider() -> GiteaProvider:
    return GiteaProvider(base_url='https://forge.example', org='Org', admin_user='admin')


def test_WITHOUT_the_cli_a_token_is_still_ISSUED(monkeypatch) -> None:
    """The direction the constraint demands. Absent the binary this used to RAISE with a message
    telling the operator to go and run it on a machine that no longer runs anything.
    """
    provider = _provider()
    recorder = _Recorder()
    monkeypatch.setattr(provider, '_call', recorder)

    assert provider.issue_token('swarm-observer', 'swarm-observer@WS9', ['read:repository']) == 'the-new-token'


def test_the_MINT_uses_the_TARGET_USER_and_not_the_admin(monkeypatch) -> None:
    """THE DISCRIMINATING ASSERTION, and the one the live probe settled. `auth='basic'` in this
    class means the ADMIN's credentials, which is right for revoking and wrong here: Gitea grants
    the token-creation route only to the account that will own the token. Minting as the admin would
    401 in production while every unit test that stubbed HTTP kept passing.
    """
    provider = _provider()
    recorder = _Recorder()
    monkeypatch.setattr(provider, '_call', recorder)

    provider.issue_token('swarm-observer', 'swarm-observer@WS9', ['read:repository'])
    mints = [c for c in recorder.calls if c[0] == 'POST' and c[1].endswith('/tokens')]
    assert len(mints) == 1, recorder.calls
    _method, _path, auth, identity, _body = mints[0]
    assert auth == 'raw-basic', 'the mint must carry a caller-supplied identity'
    assert identity == 'swarm-observer', 'and it must be the TARGET user, never the admin'


def test_the_PASSWORD_IS_SET_FIRST_and_never_returned(monkeypatch) -> None:
    """The order is the mechanism: a mint before the password is set authenticates as nobody.

    And nothing hands the password back. A caller that could read it would eventually store it, and
    a stored password for a service account is a strictly worse credential than the token it exists
    to produce -- it does not expire and it cannot be scoped.
    """
    provider = _provider()
    recorder = _Recorder()
    monkeypatch.setattr(provider, '_call', recorder)

    token = provider.issue_token('swarm-observer', 'swarm-observer@WS9', ['read:repository'])
    kinds = [(c[0], c[1].split('?')[0]) for c in recorder.calls]
    assert kinds[0] == ('PATCH', '/admin/users/swarm-observer'), kinds
    assert token == 'the-new-token'
    assert 'password' not in str(token)


def test_TWO_ISSUES_DO_NOT_REUSE_A_PASSWORD(monkeypatch) -> None:
    """Ephemeral means per issue. A password reused across mints is a long-lived secret with extra
    steps, and it would be the one an attacker who saw a single call could keep using.
    """
    provider = _provider()
    recorder = _Recorder()
    monkeypatch.setattr(provider, '_call', recorder)

    provider.issue_token('swarm-observer', 'a', ['read:repository'])
    provider.issue_token('swarm-observer', 'b', ['read:repository'])
    passwords = [c[4]['password'] for c in recorder.calls if c[0] == 'PATCH']
    assert len(passwords) == 2
    assert passwords[0] != passwords[1]
    assert all(len(p) >= 32 for p in passwords), 'and each must be long enough to be worth generating'


def test_THERE_IS_NO_CLI_ROUTE_LEFT_TO_PREFER() -> None:
    """THIS TEST USED TO ASSERT THE OPPOSITE, and the reversal is the record.

    It was `test_THE_CLI_IS_STILL_PREFERRED_WHERE_IT_EXISTS`, defending the host binary as the
    default and calling this file's route "the fallback". RETIRED 2026-08-15 (user directive:
    "彻底撤销 gitea exe 和类似逻辑"). The preference was never exercised: the forge is a VPS reached
    over SSH that runs Gitea and nothing else, so there is no machine where our code and that binary
    coexist -- `exe` resolved to None everywhere, and the branch it guarded was dead in production
    while every test that set it kept passing.

    Keeping a second route for a machine that does not exist is not caution. It was the thing
    `token()` refused into ("run this on the Gitea host with --gitea-exe") and what `issue_token`
    could not heal a taken name from, because the CLI route never held the account's own credential.
    """
    assert not hasattr(GiteaProvider, '_cli'), 'the CLI shell-out is back'
    assert not hasattr(_provider(), 'exe'), 'the provider is carrying a binary path again'


def test_a_REFUSED_PASSWORD_WRITE_does_not_look_like_a_mint_failure(monkeypatch) -> None:
    """Two different errands behind one exception would send the operator to the wrong one: a
    refused password write is an ADMIN credential problem, a refused mint is about the target
    account. The message must name which half failed.
    """
    provider = _provider()

    def refuse(method, path, body=None, **_kw):
        if method == 'PATCH':
            msg = 'admin route refused'
            raise RuntimeError(msg)
        return {'sha1': 'x'}

    monkeypatch.setattr(provider, '_call', refuse)
    with pytest.raises(Exception, match='(?i)password|admin'):
        provider.issue_token('swarm-observer', 'swarm-observer@WS9', ['read:repository'])


def test_a_NAME_ALREADY_TAKEN_is_revoked_and_reminted(monkeypatch) -> None:
    """THE STATE A MIGRATION LEAVES EVERYWHERE, met on the first real run: the old forge's tokens
    came across with the repositories, so every machine's token NAME was already taken and every
    mint answered `400 access token name has been used already`.

    THE CLI PATH CANNOT DO THIS. Its own remedy message says revoking "needs the operator PASSWORD",
    because Gitea refuses token auth on that route -- so on the host the answer is a human. Here the
    ephemeral password for exactly that user is still in frame, so the remote path can revoke and
    remint by itself. That is a capability the older route does not have, not a workaround for a
    limitation of this one.

    ONE RETRY, NOT A LOOP. If the delete succeeded and the mint still says the name is taken, the
    server is telling us something we do not understand, and retrying would turn that into a spin.
    """
    provider = _provider()
    seen: list[tuple[str, str]] = []
    refused_once = {'done': False}

    def call(method, path, body=None, *, allow=(), auth='token', raw_token=None, raw_basic=None):
        seen.append((method, path))
        if method == 'POST' and path.endswith('/tokens') and not refused_once['done']:
            refused_once['done'] = True
            msg = 'POST -> 400: {"message":"access token name has been used already"}'
            raise RuntimeError(msg)
        if method == 'POST' and path.endswith('/tokens'):
            return {'sha1': 'reminted'}
        return None

    monkeypatch.setattr(provider, '_call', call)
    assert provider.issue_token('swarm-observer', 'swarm-observer@G', ['read:repository']) == 'reminted'

    methods = [m for m, _p in seen]
    assert 'DELETE' in methods, 'the taken name must be revoked before the second attempt'
    assert methods.count('POST') == 2, 'exactly one retry'


def test_a_MINT_that_fails_for_ANOTHER_REASON_is_not_retried(monkeypatch) -> None:
    """The discriminating half. A 403, a 404 or a network error is not a name collision, and
    deleting a token in response to one would destroy a working credential to fix nothing.
    """
    provider = _provider()
    attempts = {'n': 0}

    def call(method, path, body=None, *, allow=(), auth='token', raw_token=None, raw_basic=None):
        if method == 'POST' and path.endswith('/tokens'):
            attempts['n'] += 1
            msg = 'POST -> 403: {"message":"forbidden"}'
            raise RuntimeError(msg)
        return None

    monkeypatch.setattr(provider, '_call', call)
    with pytest.raises(Exception, match='(?i)refused to mint'):
        provider.issue_token('swarm-observer', 'swarm-observer@G', ['read:repository'])
    assert attempts['n'] == 1, 'a non-collision failure must not be retried'
