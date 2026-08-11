"""A credential is not stored because the store said nothing, and a swarm is not healthy because
its configuration is.

TWO MEASUREMENTS ON THE LIVE HOST, 2026-08-10, and the same shape twice.

**`enroll` reported four credentials stored. Two were not.** `git credential approve` exits 0
whether or not the helper kept anything, and Git Credential Manager silently declines to persist
credentials for a plain-HTTP remote (it warns `use of unencrypted HTTP remote URLs is not
recommended`). `swarm-observer` and `swarm-integrator` landed; `swarm-agent` and `swarm-verifier`
did not, and all four printed `stored  sha256=...`.

The loss was UNRECOVERABLE and that is what makes it worth a guard rather than a note. Gitea keeps
only a hash; the plaintext exists exactly once, in the pipe. And the names could not be reused --
the next `enroll` died with `access token name has been used already`. So a silent write failure
cost two credentials permanently and blocked the obvious recovery.

**`verify` printed `configuration problems: none` over a swarm that could not authenticate at all.**
The four role accounts had been created by hand before this tool existed and carried
`must_change_password`, so Gitea answered `403 You must change your password` to every token they
minted. Everything `verify` read -- teams, membership, repo attachment, units -- was genuinely
correct. It was checking the configuration layer for a fault that lives below it.

WHY THE PROBE IS "CAN THIS TOKEN AUTHENTICATE" AND NOT "IS THE FLAG CLEAR". A flag read would pin
this to one server-side cause, to Gitea's schema, and to the version of it we happened to measure.
Asking the identity to authenticate is strictly stronger: the same check catches a revoked token, a
disabled account, and whatever the next refusal turns out to be. The flag is the instance; the
credential not working is the class.

AND UNKNOWN IS NOT OK. A machine holding no credential for a role cannot answer for the server, so
it reports "not stored here" rather than a tick -- reporting absence as health is how the 403 stayed
invisible for a whole provisioning run.

THE FIRST VERSION OF THAT CHECK WAS ITSELF A LIE, and the sequence is the point. It asked
`GET /user`, which needs `read:user` -- a scope no role carries -- so every role 403'd for a reason
unrelated to authentication, and the code read a 403 as "reachable". `verify` then printed four `ok`
lines on a server where the probe could never have succeeded: a check that passes for all inputs,
replacing a silence, which is louder and worse. Caught by the agent driving the live host, not by
this file, because everything here below the API boundary went through a stub that answered what it
was told to. So the endpoint moved to `/repos/{owner}/{repo}` -- covered by `read:repository`, which
every role has -- and the last section tests the REAL method against a scripted transport, with the
scope-vs-authentication distinction asserted against `ROLES` rather than written out.
"""

from __future__ import annotations

import argparse

import pytest

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


# --------------------------------------------------------------------------- the write is proven


@pytest.fixture
def helper(swarmctl, monkeypatch):
    """A credential helper whose keep/drop behaviour is scriptable, driven through the REAL
    `store_credential` so the read-back under test is the one that ships.
    """
    kept: dict[str, str] = {}
    drop: set[str] = set()

    def approve(_argv, **kwargs):
        # `**kwargs` rather than naming the parameter `input`: subprocess's keyword shadows the
        # builtin, which needs a suppression, and a suppression is a defect deferred. Reading it out
        # of the dict costs one line and removes the waiver.
        fields = dict(line.split('=', 1) for line in kwargs['input'].strip().splitlines())
        if fields['username'] not in drop:
            kept[fields['username']] = fields['password']
        return argparse.Namespace(returncode=0, stdout='', stderr='')

    monkeypatch.setattr(swarmctl.subprocess, 'run', approve)
    monkeypatch.setattr(swarmctl, 'read_credential', lambda _s, _h, user: kept.get(user))
    return {'kept': kept, 'drop': drop}


def test_a_helper_that_silently_drops_the_credential_RAISES(swarmctl, helper):
    """THE DEFECT ITSELF. Exit 0, nothing kept, and the old code called that success."""
    helper['drop'].add('swarm-agent')
    with pytest.raises(swarmctl.Fail, match='did not keep it'):
        swarmctl.store_credential('http', 'host:9000', 'swarm-agent', 'secret')


def test_a_helper_that_keeps_it_is_silent(swarmctl, helper):
    """The discriminating half: a read-back that always failed would refuse every enrollment."""
    swarmctl.store_credential('http', 'host:9000', 'swarm-observer', 'secret')
    assert helper['kept']['swarm-observer'] == 'secret'


def test_the_refusal_names_the_recovery_and_the_permanence(swarmctl, helper):
    """The operator has just lost a secret that exists nowhere else, and the token NAME is now
    burnt. A message that only said "failed" would send them to re-run the command that cannot work.
    """
    helper['drop'].add('swarm-agent')
    with pytest.raises(swarmctl.Fail) as caught:
        swarmctl.store_credential('http', 'host:9000', 'swarm-agent', 'secret')
    message = str(caught.value)
    assert '--machine' in message, 'the refusal must name the way forward'
    assert 'hash' in message, 'the refusal must say the secret is unrecoverable'
    assert 'secret' not in message.replace('secret is unrecoverable', ''), 'the token leaked into the error'


def test_the_refusal_carries_no_token(swarmctl, helper):
    """Project invariant: never log tokens. The failure path is the one that only renders when
    something has already gone wrong, so it is never seen in a green run.
    """
    helper['drop'].add('swarm-agent')
    with pytest.raises(swarmctl.Fail) as caught:
        swarmctl.store_credential('http', 'host:9000', 'swarm-agent', 'tok-abc123-do-not-print')
    assert 'tok-abc123-do-not-print' not in str(caught.value)


# --------------------------------------------------------------------------- verify leaves the config layer


class _Provider:
    """Only what `cmd_verify` touches. A real `GiteaProvider` would need a server.

    A FULLY CORRECT CONFIGURATION, which is the whole point: the measured host was exactly this --
    teams present, members right, repo attached, units matching -- and no role could authenticate.
    A stub with a config fault would let this file pass for the wrong reason.

    Team and member data is DERIVED FROM `ROLES` rather than written out, so adding a fifth role
    cannot leave the stub behind agreeing with a four-role expectation.
    """

    name = 'gitea'

    def __init__(self, swarmctl, works: dict[str, bool | None]) -> None:
        self._works = works
        self.base_url, self.org = 'http://host:9000', 'Org'
        self._teams = [
            {'name': team, 'id': index, 'units_map': dict(units)}
            for index, (team, units, _scopes) in enumerate(swarmctl.ROLES.values(), start=1)
        ]
        self._member_of = {index: swarmctl.USERS[role] for index, role in enumerate(swarmctl.ROLES, start=1)}

    def teams(self):
        return list(self._teams)

    def team_members(self, team_id: int):
        return [self._member_of[team_id]]

    def team_repos(self, _team_id: int):
        return ['Org/repo']

    def credential_works(self, username: str, _owner: str, _repo: str) -> bool | None:
        return self._works.get(username)

    def protections(self, _owner, _repo):
        return []


def _verify(swarmctl, works: dict[str, bool | None], capsys):
    provider = _Provider(swarmctl, works)
    args = argparse.Namespace(repo='Org/repo', branch='main', status_context='someproject/gate')
    swarmctl.cmd_verify(provider, args)
    return capsys.readouterr().out


def test_a_role_whose_token_is_refused_is_a_PROBLEM(swarmctl, capsys):
    """The measured case: everything configured, nothing able to authenticate."""
    out = _verify(swarmctl, {name: name != 'swarm-agent' for name in swarmctl.USERS.values()}, capsys)
    assert 'FAIL swarm-agent' in out
    assert 'configuration problems: none' not in out


def test_all_four_working_is_not_reported_as_a_problem(swarmctl, capsys):
    """The discriminating half -- a check that flagged everything would just be noise."""
    out = _verify(swarmctl, dict.fromkeys(swarmctl.USERS.values(), True), capsys)
    assert 'FAIL' not in out


def test_a_credential_this_machine_does_not_hold_is_UNKNOWN_not_ok(swarmctl, capsys):
    """`None` must not render as a tick. A machine that never enrolled cannot answer for the
    server, and calling that healthy is exactly how the 403 stayed invisible.
    """
    out = _verify(swarmctl, dict.fromkeys(swarmctl.USERS.values(), None), capsys)
    assert 'cannot be checked from this machine' in out
    assert 'ok   swarm-agent' not in out


def test_the_unknown_case_is_not_counted_as_a_failure_either(swarmctl, capsys):
    """Off-host machines are the normal case for `verify`, and turning "I cannot see" into "broken"
    would train the operator to ignore the count.
    """
    out = _verify(swarmctl, dict.fromkeys(swarmctl.USERS.values(), None), capsys)
    assert 'configuration problems: none' in out


# ------------------------------------------------------- the probe itself, which was wrong once


class _Api:
    """Records the request line and answers with a scripted status."""

    def __init__(self) -> None:
        self.paths: list[str] = []
        self.auth: list[str] = []
        self.status = 200
        self.body = b'{"full_name": "Org/repo"}'

    def install(self, swarmctl, monkeypatch) -> None:
        api = self

        class _Conn:
            def __init__(self, *_a, **_k) -> None:
                pass

            def request(self, _method, path, body=None, headers=None) -> None:
                api.paths.append(path)
                api.auth.append((headers or {}).get('Authorization', ''))

            def getresponse(self):
                return type('_R', (), {'status': api.status, 'read': staticmethod(lambda: api.body)})

            def close(self) -> None:
                pass

        monkeypatch.setattr(swarmctl.http.client, 'HTTPConnection', _Conn)
        monkeypatch.setattr(swarmctl, 'read_credential', lambda _s, _h, _u: 'role-token')


@pytest.fixture
def api(swarmctl, monkeypatch) -> _Api:
    recorder = _Api()
    recorder.install(swarmctl, monkeypatch)
    return recorder


def _probe(swarmctl):
    return swarmctl.GiteaProvider('http://host:9000', 'Org', None, 'admin')


def test_a_403_is_a_FAILURE_not_a_success(swarmctl, api):
    """THE INVERSION, pinned. The first version read a refusal as "reachable" -- `payload is None`
    -- so `verify` printed `ok` for every role precisely when the server said no.
    """
    api.status = 403
    assert _probe(swarmctl).credential_works('swarm-agent', 'Org', 'repo') is False


@pytest.mark.usefixtures('api')
def test_a_200_is_a_success(swarmctl):
    assert _probe(swarmctl).credential_works('swarm-agent', 'Org', 'repo') is True


@pytest.mark.parametrize('status', [401, 404])
def test_every_refusal_shape_is_a_failure(swarmctl, api, status: int):
    """404 counts: Gitea hides a repo the caller may not see behind one, so it is a refusal wearing
    a different number.
    """
    api.status = status
    assert _probe(swarmctl).credential_works('swarm-agent', 'Org', 'repo') is False


def test_the_probe_asks_for_the_REPO_and_not_the_user(swarmctl, api):
    """WHY THE ENDPOINT IS THE WHOLE BUG. `GET /user` needs `read:user`, a scope NO role in `ROLES`
    carries, so on the live host every role 403'd for a reason unrelated to authentication -- and a
    probe that can never succeed is not a check, whichever way its comparison points.
    """
    _probe(swarmctl).credential_works('swarm-agent', 'Org', 'repo')
    assert api.paths == ['/api/v1/repos/Org/repo']


def test_the_endpoint_is_within_every_roles_scopes(swarmctl):
    """The property that makes a refusal mean something. Asserted against `ROLES` rather than
    written out, so a role added without repository read turns this red instead of silently
    reintroducing the scope-vs-auth confusion.
    """
    for role, (_team, _units, scopes) in swarmctl.ROLES.items():
        assert any(scope.endswith(':repository') for scope in scopes), (
            f'{role} cannot read the repo, so the credential probe cannot distinguish a scope '
            f'refusal from an authentication refusal for it'
        )


def test_the_probe_uses_THAT_ROLES_token_and_not_the_runs_own(swarmctl, api):
    """Answering "can swarm-agent authenticate" with the admin credential would always say yes."""
    _probe(swarmctl).credential_works('swarm-agent', 'Org', 'repo')
    assert api.auth == ['token role-token']


@pytest.mark.usefixtures('api')
def test_no_stored_credential_is_UNKNOWN(swarmctl, monkeypatch):
    monkeypatch.setattr(swarmctl, 'read_credential', lambda _s, _h, _u: None)
    assert _probe(swarmctl).credential_works('swarm-agent', 'Org', 'repo') is None


def test_an_unreachable_server_is_UNKNOWN_not_a_failure(swarmctl, api):
    """A bare "could not ask" must not be reported as "this role is broken" -- that would send an
    operator re-issuing credentials over a network blip.
    """
    api.status = 503
    assert _probe(swarmctl).credential_works('swarm-agent', 'Org', 'repo') is None


# ------------------------------------------------- a non-host machine can still ask its own question


class _NoAdmin(_Provider):
    """A machine with role credentials and NO admin credential -- every machine but the host.

    It raises the REAL `Fail` the provider raises, not a stand-in: `cmd_verify` catches that type
    specifically, and a test raising something else would pass while the shipping code aborted.
    """

    def __init__(self, swarmctl, works) -> None:
        super().__init__(swarmctl, works)
        self._fail = swarmctl.Fail

    def teams(self):
        msg = 'no admin credential stored for swarmctl-admin@host:9000, and no Gitea CLI'
        raise self._fail(msg)


def test_a_machine_without_an_admin_credential_still_gets_its_CREDENTIALS_section(swarmctl, capsys):
    """MEASURED on the fleet's SECOND machine, minutes after enrolment: `verify` aborted with
    "no admin credential stored", so the one question a freshly enrolled machine actually has --
    do MY four credentials work? -- was the one question it could not ask.

    The team reads need an admin credential; the credential check does not. Ordering decides whether
    this verb is usable on the machines the fleet is made of.
    """
    provider = _NoAdmin(swarmctl, dict.fromkeys(swarmctl.USERS.values(), True))
    args = argparse.Namespace(repo='Org/repo', branch='main', status_context='someproject/gate')
    swarmctl.cmd_verify(provider, args)
    out = capsys.readouterr().out
    assert out.count('ok   swarm-') == len(swarmctl.USERS)
    assert 'not checked from here' in out


def test_the_missing_admin_credential_is_not_counted_as_a_deployment_problem(swarmctl, capsys):
    """It is the NORMAL state everywhere except the host. Counting it would make every fleet
    machine report a problem it cannot fix and should not care about.
    """
    provider = _NoAdmin(swarmctl, dict.fromkeys(swarmctl.USERS.values(), True))
    args = argparse.Namespace(repo='Org/repo', branch='main', status_context='someproject/gate')
    assert swarmctl.cmd_verify(provider, args) == 0
    assert 'configuration problems: none' in capsys.readouterr().out


def test_a_REFUSED_credential_still_fails_on_such_a_machine(swarmctl, capsys):
    """The discriminating half: degrading the org checks must not have degraded the one check the
    machine came for.
    """
    works = dict.fromkeys(swarmctl.USERS.values(), True)
    works['swarm-agent'] = False
    provider = _NoAdmin(swarmctl, works)
    args = argparse.Namespace(repo='Org/repo', branch='main', status_context='someproject/gate')
    assert swarmctl.cmd_verify(provider, args) == 1
    assert 'FAIL swarm-agent' in capsys.readouterr().out
