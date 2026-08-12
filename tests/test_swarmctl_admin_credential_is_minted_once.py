"""ONE standing admin token, not one per run -- and the leftovers of the old design are reachable.

WHAT WAS MEASURED, on the live Gitea host on 2026-08-10. swarmctl minted a fresh
`swarmctl-ephemeral@<host>-<hex>` admin token per run and revoked it in a `finally`. Running
`provision`, `onboard` and `verify` left THREE standing `write:admin` tokens, each run printing a
warning nobody was obliged to read.

THE REVOKE COULD NEVER HAVE WORKED. It goes through `GET/DELETE /users/<u>/tokens`, and Gitea
answers 401 to TOKEN auth on those routes -- a token may not enumerate or delete tokens, which is a
deliberate circularity guard. So "ephemeral" was a property of the NAME and of the docstring, and
of nothing that ran. The declaration-that-lies shape, in a credential path.

AND NOTHING COULD CLEAN THEM UP. `cmd_revoke` walked `USERS.values()` -- the four ROLE users -- so
the one account swarmctl mints admin tokens under was the one account it never scanned; and
`--unmanaged` means "not named user@machine", which a leftover is. A tool that creates a credential
class no selector can reach leaves that class to accumulate.

THE FIX IS NOT A BETTER REVOKE. The choice was never "ephemeral vs standing" -- it was ONE standing
admin token or N of them. `token()` now mints one named for the machine, stores it where
`admin-emit`/`consume` already put credentials, and every later run finds it and mints nothing.

AND THE MANAGEMENT ROUTES NOW SEND BASIC AUTH, because a token demonstrably cannot use them: a
`revoke --ephemeral` run against the live host on the fixed code revoked ZERO and added a fourth
leftover, aborting on its first listing call. A selector that is correct on a channel that cannot
carry it is not a fix.

WHAT IS NOT CLAIMED: that basic auth SUCCEEDS there. The 401 under token auth is measured; that a
password works is an inference from Gitea's documented circularity guard, untested because no
password was available. It is written as an inference here and the code fails with an instruction
rather than a retry, so a wrong inference surfaces as one clear error instead of a loop.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from agent_swarm import swarmctl as _swarmctl

#: This module's own source, read by the two assertions below that are about what the code SAYS.
_SOURCE = Path(_swarmctl.__file__)


@pytest.fixture(scope='module')
def swarmctl():
    """The module under test, as an ORDINARY IMPORT.

    It used to be loaded by path with `importlib.util.spec_from_file_location`, because it lived in
    another project as a bare script that nothing could import. Here it is a module of this package,
    so the loader preamble is gone -- and with it a whole class of mistake, since a hand-rolled load
    can silently execute a DIFFERENT file from the one an import would resolve.
    """
    return _swarmctl


class _Server:
    """The token table. Shared between providers in a test, which is what makes a second mint
    visible as a second row rather than as nothing at all.
    """

    def __init__(self) -> None:
        self.tokens: list[dict] = []
        self._next = 1

    def issue(self, name: str) -> str:
        self.tokens.append({'id': self._next, 'name': name})
        self._next += 1
        return f'secret-{self._next}'


@pytest.fixture
def store(swarmctl, monkeypatch):
    """An in-memory stand-in for the OS credential store, shared across providers like the real one.

    A per-provider store would make every run mint again and the test would agree with the defect.
    """
    kept: dict[str, str] = {}
    monkeypatch.setattr(
        swarmctl, 'store_credential', lambda _s, host, user, tok: kept.__setitem__(f'{user}@{host}', tok)
    )
    monkeypatch.setattr(swarmctl, 'read_credential', lambda _s, host, user: kept.get(f'{user}@{host}'))
    return kept


def _provider(swarmctl, server: _Server, monkeypatch, *, admin: str | None = 'admin'):
    """A provider whose three server-touching methods are redirected at `_Server`.

    `monkeypatch.setattr` rather than plain assignment: assigning a lambda over a bound method needs
    a `# type: ignore[method-assign]` at each site, and this repo's suppression ratchet is a ceiling
    that must be met by restructuring rather than raised. monkeypatch also undoes them per test.
    """
    provider = swarmctl.GiteaProvider('http://host:9000', 'Org', None, admin)
    monkeypatch.setattr(provider, 'exe', 'pretend-gitea')
    monkeypatch.setattr(provider, 'issue_token', lambda _user, name, _scopes: server.issue(name))
    monkeypatch.setattr(provider, 'tokens_of', lambda _user: list(server.tokens))
    monkeypatch.setattr(
        provider,
        'revoke_token',
        lambda _user, tid: server.tokens.__setitem__(slice(None), [t for t in server.tokens if t['id'] != tid]),
    )
    return provider


# --------------------------------------------------------------------------- minted once


@pytest.mark.usefixtures('store')
def test_three_runs_leave_ONE_admin_token(swarmctl, monkeypatch):
    """THE DEFECT ITSELF, at the exact scale it was measured: provision, onboard, verify."""
    server = _Server()
    for _ in range(3):
        _provider(swarmctl, server, monkeypatch).token()
    assert len(server.tokens) == 1, f'{len(server.tokens)} standing admin tokens: {[t["name"] for t in server.tokens]}'


@pytest.mark.usefixtures('store')
def test_the_second_run_reuses_the_stored_credential(swarmctl, monkeypatch):
    """Not merely "one token exists" -- the later run must actually be USING it, or it is holding a
    credential the server does not know about and every call 401s.
    """
    server = _Server()
    first = _provider(swarmctl, server, monkeypatch).token()
    assert _provider(swarmctl, server, monkeypatch).token() == first


def test_the_credential_is_stored_before_the_verb_runs(swarmctl, store, monkeypatch):
    """A run that crashes mid-verb must still leave the credential findable. Storing on the way out
    loses it on exactly the runs that fail, and the next run then mints a SECOND token under the
    same name -- two rows, one name, and no way to tell which the store holds.
    """
    server = _Server()
    provider = _provider(swarmctl, server, monkeypatch)
    provider.token()
    assert store, 'the token was minted but not stored'


@pytest.mark.usefixtures('store')
def test_the_name_says_which_machine(swarmctl, monkeypatch):
    """The name is the only handle an operator has for revoking it by hand, and one token per
    machine is what makes revoking one machine not lock out the others.
    """
    server = _Server()
    _provider(swarmctl, server, monkeypatch).token()
    assert server.tokens[0]['name'].startswith(f'{swarmctl.GiteaProvider.ADMIN_CRED_USER}@')


@pytest.mark.usefixtures('store')
def test_no_ephemeral_token_is_ever_minted(swarmctl, monkeypatch):
    """The retired spelling must not return through a different path -- and this is checkable
    against BEHAVIOUR rather than against the source text.
    """
    server = _Server()
    _provider(swarmctl, server, monkeypatch).token()
    assert not any('ephemeral' in t['name'] for t in server.tokens)


@pytest.mark.usefixtures('store')
def test_a_machine_with_no_cli_and_no_credential_refuses(swarmctl, monkeypatch):
    """It must not fall back to running unauthenticated: the failure is an instruction (`admin-emit`),
    not a 401 three layers down.
    """
    provider = _provider(swarmctl, _Server(), monkeypatch)
    monkeypatch.setattr(provider, 'exe', None)
    with pytest.raises(swarmctl.Fail, match='admin-emit'):
        provider.token()


# --------------------------------------------------------------------------- the old leftovers


def test_the_admin_user_is_swept_too(swarmctl):
    """`revoke` walked the four ROLE users only, so the account the leftovers live under was never
    scanned. Appended, not substituted -- an off-host run has no admin and must still sweep roles.
    """
    assert 'MingyangBao' in swarmctl._revocable_users(
        swarmctl.GiteaProvider('http://host:9000', 'Org', None, 'MingyangBao')
    )


def test_an_off_host_run_still_sweeps_the_role_users(swarmctl):
    provider = swarmctl.GiteaProvider('http://host:9000', 'Org', None, None)
    assert set(swarmctl._revocable_users(provider)) == set(swarmctl.USERS.values())


@pytest.mark.parametrize(
    ('selector', 'reaches'),
    [('unmanaged', False), ('machine_given', False), ('ephemeral', True)],
)
def test_only_the_ephemeral_selector_reaches_a_leftover(swarmctl, monkeypatch, selector: str, reaches: bool):
    """WHY A NEW SELECTOR AND NOT A WIDENED OLD ONE, as the two near-misses:

    * `--unmanaged` means "not named user@machine", and a leftover IS so named. It was managed; it
      simply could not be cleaned up.
    * `--machine NAME` matches `*@NAME`, and a leftover's machine half carries a random suffix
      (`@DUIPEZZTZ-d56860`), so it never matches.

    Both would have to be made wrong to cover this, which is how a third rule earns its place.
    """
    args = argparse.Namespace(
        machine_given=selector == 'machine_given',
        machine='DUIPEZZTZ',
        token_name=None,
        unmanaged=selector == 'unmanaged',
        ephemeral=selector == 'ephemeral',
        all_tokens=False,
        confirm=None,
        erase_local=False,
    )
    server = _Server()
    server.issue('swarmctl-ephemeral@DUIPEZZTZ-d56860')
    swarmctl.cmd_revoke(_provider(swarmctl, server, monkeypatch), args)
    assert (server.tokens == []) is reaches


# --------------------------------------------------------------------------- the auth the route needs


class _Recorder:
    """Captures the Authorization header instead of reaching a server."""

    def __init__(self) -> None:
        self.headers: list[str] = []
        #: The status every response answers with. READ AT `getresponse` TIME, not at `install` time:
        #: a snapshot taken when the connection class was built would silently ignore any test that
        #: set it afterwards, and a 200 that never reaches the branch under test passes for the
        #: wrong reason.
        self.status = 200

    def install(self, swarmctl, monkeypatch, payload: bytes = b'[]') -> None:
        recorder = self

        class _Response:
            @property
            def status(self) -> int:
                return recorder.status

            @staticmethod
            def read() -> bytes:
                return payload

        class _Conn:
            def __init__(self, *_a, **_k) -> None:
                pass

            def request(self, _method, _path, body=None, headers=None) -> None:
                recorder.headers.append((headers or {}).get('Authorization', ''))

            def getresponse(self):
                return _Response()

            def close(self) -> None:
                pass

        monkeypatch.setattr(swarmctl.http.client, 'HTTPConnection', _Conn)


def test_listing_tokens_uses_BASIC_auth(swarmctl, monkeypatch):
    """THE MEASURED REFUTATION, pinned. Token auth on this route answered 401 on the live host even
    for a token that had just performed admin writes -- and the old code retried it forever.
    """
    recorder = _Recorder()
    recorder.install(swarmctl, monkeypatch)
    monkeypatch.setenv('SWARM_ADMIN_PASSWORD', 'pw')
    provider = swarmctl.GiteaProvider('http://host:9000', 'Org', None, 'admin')
    provider.tokens_of('swarm-agent')
    assert recorder.headers == ['Basic YWRtaW46cHc=']


def test_revoking_a_token_uses_BASIC_auth(swarmctl, monkeypatch):
    recorder = _Recorder()
    recorder.install(swarmctl, monkeypatch)
    monkeypatch.setenv('SWARM_ADMIN_PASSWORD', 'pw')
    provider = swarmctl.GiteaProvider('http://host:9000', 'Org', None, 'admin')
    provider.revoke_token('swarm-agent', 7)
    assert recorder.headers[0].startswith('Basic ')


def test_an_ordinary_call_still_uses_the_TOKEN(swarmctl, monkeypatch):
    """Basic auth must not leak onto the rest of the API: those routes work with a token, and the
    password is not available on a machine that only has a stored credential.
    """
    recorder = _Recorder()
    recorder.install(swarmctl, monkeypatch, payload=b'{}')
    monkeypatch.setattr(swarmctl, 'read_credential', lambda *_a, **_k: 'stored-token')
    provider = swarmctl.GiteaProvider('http://host:9000', 'Org', None, 'admin')
    provider.api_obj('GET', '/orgs/Org')
    assert recorder.headers == ['token stored-token']


def test_no_password_REFUSES_with_an_instruction(swarmctl, monkeypatch):
    """Not a 401 three layers down. The old code's failure named an endpoint, so three sessions read
    it as a server problem; the operator needs to be told what to supply.
    """
    monkeypatch.delenv('SWARM_ADMIN_PASSWORD', raising=False)
    monkeypatch.setattr(swarmctl.sys.stdin, 'isatty', lambda: False)
    provider = swarmctl.GiteaProvider('http://host:9000', 'Org', None, 'admin')
    with pytest.raises(swarmctl.Fail, match='SWARM_ADMIN_PASSWORD'):
        provider.tokens_of('swarm-agent')


def test_the_password_is_never_stored(swarmctl):
    """It is a human's login. Writing it to the credential store or the config file would make a
    password recoverable from disk on every machine that ever ran a revoke.
    """
    source = _SOURCE.read_text(encoding='utf-8')
    assert 'store_credential' not in source.split('def admin_password')[1].split('def _call')[0]
    assert 'admin_password' not in swarmctl.CONFIG_KEYS


def test_list_says_UNKNOWN_rather_than_none_when_it_cannot_read_tokens(swarmctl, monkeypatch, capsys):
    """'-' would read as "this user has no tokens", and an operator acts on that by re-issuing
    credentials that already exist. The overview must still run without a password; what it must
    not do is answer the token question wrongly.
    """
    monkeypatch.delenv('SWARM_ADMIN_PASSWORD', raising=False)
    monkeypatch.setattr(swarmctl.sys.stdin, 'isatty', lambda: False)
    provider = swarmctl.GiteaProvider('http://host:9000', 'Org', None, 'admin')
    monkeypatch.setattr(provider, 'teams', list)
    monkeypatch.setattr(provider, 'user_exists', lambda _u: True)
    swarmctl.cmd_list(provider, argparse.Namespace(repo=None))
    out = capsys.readouterr().out
    assert 'SWARM_ADMIN_PASSWORD' in out
    assert 'tokens: -' not in out


# --------------------------------------------------------------------------- revoke does not self-destruct


def _revoke_args(**over):
    base = {
        'machine_given': False,
        'machine': 'THISBOX',
        'token_name': None,
        'unmanaged': False,
        'ephemeral': False,
        'all_tokens': False,
        'confirm': None,
        'erase_local': False,
    }
    return argparse.Namespace(**{**base, **over})


@pytest.fixture
def thisbox(swarmctl, monkeypatch) -> str:
    monkeypatch.setattr(swarmctl.socket, 'gethostname', lambda: 'THISBOX')
    return f'{swarmctl.GiteaProvider.ADMIN_CRED_USER}@THISBOX'


def test_retiring_a_machine_spares_the_credential_the_run_is_using(swarmctl, monkeypatch, thisbox):
    """`--machine NAME` matches `*@NAME`, and this box's admin credential is `swarmctl-admin@NAME`.

    So the obvious way to retire a machine's stale role tokens also revokes the credential the
    revoke is authenticating with -- self-destructing part way through, leaving the rest of the
    sweep 401 and the operator locked out of the verb they were running.
    """
    server = _Server()
    server.issue('swarm-agent@THISBOX')
    server.issue(thisbox)
    swarmctl.cmd_revoke(_provider(swarmctl, server, monkeypatch), _revoke_args(machine_given=True))
    assert [t['name'] for t in server.tokens] == [thisbox]


def test_naming_it_exactly_DOES_revoke_it(swarmctl, monkeypatch, thisbox):
    """The discriminating half. Sparing it unconditionally would make retiring this machine
    impossible, and an operator who typed the exact name has said what they mean.
    """
    server = _Server()
    server.issue(thisbox)
    swarmctl.cmd_revoke(_provider(swarmctl, server, monkeypatch), _revoke_args(token_name=thisbox))
    assert server.tokens == []


@pytest.mark.usefixtures('thisbox')
def test_another_machines_admin_credential_is_NOT_spared(swarmctl, monkeypatch):
    """The guard is about THIS run's credential, not about admin credentials as a class -- sparing
    every machine's would make a lost laptop unrevokable from anywhere else.
    """
    server = _Server()
    server.issue('swarmctl-admin@OTHERBOX')
    swarmctl.cmd_revoke(_provider(swarmctl, server, monkeypatch), _revoke_args(machine_given=True, machine='OTHERBOX'))
    assert server.tokens == []


def test_the_sparing_is_SAID_not_silent(swarmctl, monkeypatch, thisbox, capsys):
    """An operator who asked to retire a machine and got a partial result must be told which token
    survived and how to remove it -- a silent skip reads as a completed retirement.
    """
    server = _Server()
    server.issue(thisbox)
    swarmctl.cmd_revoke(_provider(swarmctl, server, monkeypatch), _revoke_args(machine_given=True))
    out = capsys.readouterr().out
    assert 'SPARED' in out
    assert '--token-name' in out


# --------------------------------------------------------------------------- it fails, never hangs


def test_it_does_not_prompt_unless_asked(swarmctl, monkeypatch):
    """MEASURED 2026-08-10: `swarmctl list` and `swarmctl revoke` HUNG on the fleet host, twice,
    once with stdin forced to /dev/null.

    The old gate was `sys.stdin.isatty()`, and no gate on stdin can work: on Windows `getpass`
    reads the CONSOLE directly, so redirecting stdin does not reach it. A hang is the worst failure
    a 7x24 fleet can have -- no exit code, no log line, no timeout, and a scheduler cannot tell it
    from slow work. `read_credential` next door carries GIT_TERMINAL_PROMPT=0 for exactly this.
    """
    monkeypatch.delenv('SWARM_ADMIN_PASSWORD', raising=False)
    monkeypatch.setattr(swarmctl.sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr(swarmctl.getpass, 'getpass', lambda _p: pytest.fail('prompted without --ask-password'))
    provider = swarmctl.GiteaProvider('http://host:9000', 'Org', None, 'admin')
    with pytest.raises(swarmctl.Fail, match='--ask-password'):
        provider.admin_password()


def test_asking_for_it_DOES_prompt(swarmctl, monkeypatch):
    """The discriminating half: a flag that never prompts is just a slower refusal."""
    monkeypatch.delenv('SWARM_ADMIN_PASSWORD', raising=False)
    monkeypatch.setattr(swarmctl.getpass, 'getpass', lambda _p: 'typed')
    provider = swarmctl.GiteaProvider('http://host:9000', 'Org', None, 'admin', ask_password=True)
    assert provider.admin_password() == 'typed'


def test_the_environment_is_used_without_asking(swarmctl, monkeypatch):
    """The unattended path: a runner supplies it once and never reaches the prompt branch."""
    monkeypatch.setenv('SWARM_ADMIN_PASSWORD', 'from-env')
    monkeypatch.setattr(swarmctl.getpass, 'getpass', lambda _p: pytest.fail('prompted with the env var set'))
    provider = swarmctl.GiteaProvider('http://host:9000', 'Org', None, 'admin', ask_password=True)
    assert provider.admin_password() == 'from-env'


def test_the_refusal_names_both_ways_to_supply_it(swarmctl, monkeypatch):
    """The operator is blocked mid-cleanup; a message that only says "needs a password" leaves them
    guessing which of the two channels this tool accepts.
    """
    monkeypatch.delenv('SWARM_ADMIN_PASSWORD', raising=False)
    provider = swarmctl.GiteaProvider('http://host:9000', 'Org', None, 'admin')
    with pytest.raises(swarmctl.Fail) as caught:
        provider.admin_password()
    assert 'SWARM_ADMIN_PASSWORD' in str(caught.value)
    assert '--ask-password' in str(caught.value)


# --------------------------------------------------------------------------- a dead stored credential


def test_a_REFUSED_stored_credential_is_forgotten(swarmctl, monkeypatch):
    """MEASURED 2026-08-10, hours after tokens were revoked by hand. `token()` returns the stored
    credential first and NEVER validates it, so one that is dead server-side but still present
    locally is returned forever: every run 401s on the same routes, and the docstring's claim that
    a second run "finds it above" became the thing keeping the tool broken. Recovery took a
    hand-run `git credential reject`, which no message offered.
    """
    erased: list[str] = []
    monkeypatch.setattr(swarmctl, 'read_credential', lambda *_a, **_k: 'dead-token')
    monkeypatch.setattr(swarmctl, 'erase_credential', lambda _s, _h, user: erased.append(user))
    recorder = _Recorder()
    recorder.status = 401
    recorder.install(swarmctl, monkeypatch)
    provider = swarmctl.GiteaProvider('http://host:9000', 'Org', None, 'admin')
    with pytest.raises(swarmctl.Fail):
        provider.api_obj('GET', '/orgs/Org')
    assert erased == [swarmctl.GiteaProvider.ADMIN_CRED_USER]


def test_it_is_NOT_a_retry(swarmctl, monkeypatch):
    """A 401 retried with the SAME credential is still a 401, and that stays forbidden. The
    credential is discarded, the call still FAILS, and the NEXT run mints a fresh one because
    nothing stored shadows it.
    """
    monkeypatch.setattr(swarmctl, 'read_credential', lambda *_a, **_k: 'dead-token')
    monkeypatch.setattr(swarmctl, 'erase_credential', lambda *_a: None)
    recorder = _Recorder()
    recorder.status = 401
    recorder.install(swarmctl, monkeypatch)
    provider = swarmctl.GiteaProvider('http://host:9000', 'Org', None, 'admin')
    with pytest.raises(swarmctl.Fail):
        provider.api_obj('GET', '/orgs/Org')
    assert len(recorder.headers) == 1, 'a refusal must not be retried'


def test_a_token_minted_THIS_RUN_is_not_forgotten(swarmctl, monkeypatch):
    """It was just created, so a 401 against it means something else is wrong -- erasing it would
    hide that behind a message about a stale credential.
    """
    erased: list[str] = []
    monkeypatch.setattr(swarmctl, 'read_credential', lambda *_a, **_k: None)
    monkeypatch.setattr(swarmctl, 'store_credential', lambda *_a: None)
    monkeypatch.setattr(swarmctl, 'erase_credential', lambda _s, _h, user: erased.append(user))
    recorder = _Recorder()
    recorder.status = 401
    recorder.install(swarmctl, monkeypatch)
    provider = swarmctl.GiteaProvider('http://host:9000', 'Org', None, 'admin')
    monkeypatch.setattr(provider, 'exe', 'pretend-gitea')
    monkeypatch.setattr(provider, 'issue_token', lambda *_a: 'fresh-token')
    with pytest.raises(swarmctl.Fail):
        provider.api_obj('GET', '/orgs/Org')
    assert erased == []


def test_a_healthy_stored_credential_is_kept(swarmctl, monkeypatch):
    """The discriminating half: forgetting on any failure would throw away a working credential
    every time the server hiccuped.
    """
    erased: list[str] = []
    monkeypatch.setattr(swarmctl, 'read_credential', lambda *_a, **_k: 'good-token')
    monkeypatch.setattr(swarmctl, 'erase_credential', lambda _s, _h, user: erased.append(user))
    recorder = _Recorder()
    recorder.install(swarmctl, monkeypatch, payload=b'{}')
    provider = swarmctl.GiteaProvider('http://host:9000', 'Org', None, 'admin')
    provider.api_obj('GET', '/orgs/Org')
    assert erased == []
