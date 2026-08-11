"""The fleet's tools must not touch the credential store a human is also using.

THE ROOT CAUSE, MEASURED 2026-08-11 and costing four days across two machines. `swarmctl enroll`
piped four role tokens into `git credential approve` -- the OPERATOR's store. A credential store holds
ONE entry per (protocol, host); four role accounts share one host; so onboarding did not merely RISK
clobbering the human's identity, it NECESSARILY did.

What that produced: the ambient credential for the forge became `swarm-verifier`, a role account with
read on one repository and no grant on another. The symptom appeared MONTHS LATER on a DIFFERENT
machine, as `uv pip install -e .[all,dev]` failing in dependency RESOLUTION with a 404 buried in it,
because a `git+http://` dependency authenticated as whoever the vault held. Diagnosing it needed a
control probe; nothing about the symptom pointed at onboarding.

THE PROPERTY IS ASSERTED, NOT THE ABSENCE OF A CALL. Grepping the source for `credential approve`
would pass the day somebody reaches the vault by another route -- `cmdkey`, a helper config write, a
clone with userinfo that a helper then caches. So these tests run the real code paths against a real
`git` with a real, ISOLATED credential store, and read the store back afterwards.

HOW THE STORE IS ISOLATED, and why this is honest rather than a simulation. `HOME`/`USERPROFILE` and
`GIT_CONFIG_GLOBAL` are pointed at a tmp dir carrying a `store` helper backed by a file there. That
is a REAL git credential helper doing REAL persistence -- the same protocol GCM implements -- so a
write reaches it exactly as it would reach the operator's. What is NOT covered: a call that bypasses
git entirely and drives the Windows vault through `cmdkey` or the DPAPI. Named here rather than left
implied; `test_no_route_to_the_ambient_store_survives_in_the_source` is the (weaker, source-level)
backstop for that direction, and it says so.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import urllib.parse
from pathlib import Path

import pytest

from agent_swarm import credentials, swarmctl

#: RESOLVED HERE, not imported from `swarmctl`. That module no longer runs git at all -- which is
#: the repair -- so borrowing its executable path would have been this test depending on a leftover.
_GIT = shutil.which('git') or 'git'

_HOST = 'forge.example:9000'
_SCHEME = 'http'


def _git(env: dict[str, str], *args: str, stdin: str = '') -> subprocess.CompletedProcess[str]:
    return subprocess.run([_GIT, *args], input=stdin, text=True, capture_output=True, check=False, timeout=60, env=env)


@pytest.fixture
def ambient(tmp_path, monkeypatch):
    """A REAL git credential store, isolated to this test, pre-loaded with a human's credential.

    The point is that it is real: `git credential approve` genuinely persists into it and
    `git credential fill` genuinely reads it back, so "unchanged" is measured against the same
    machinery an operator has, not against a stub that could be better-behaved than reality.
    """
    home = tmp_path / 'home'
    home.mkdir()
    store = home / '.git-credentials'
    config = home / '.gitconfig'
    config.write_text('[credential]\n\thelper = store\n', encoding='utf-8')
    env = {
        **os.environ,
        'HOME': str(home),
        'USERPROFILE': str(home),
        'GIT_CONFIG_GLOBAL': str(config),
        'GIT_CONFIG_SYSTEM': str(home / 'nonexistent-system-config'),
        'GIT_TERMINAL_PROMPT': '0',
    }
    # The human's own identity, stored the way a human's `git push` stores it.
    _git(
        env,
        'credential',
        'approve',
        stdin=f'protocol={_SCHEME}\nhost={_HOST}\nusername=MingyangBao\npassword=the-humans-own-secret\n\n',
    )
    assert 'MingyangBao' in store.read_text(encoding='utf-8'), 'the fixture did not establish an ambient credential'

    # The swarm's own store goes to tmp too, so nothing here touches the developer's real config.
    monkeypatch.setattr(credentials, 'store_path', lambda: tmp_path / 'swarm' / 'credentials.json')
    # And the module under test inherits the isolated git environment for any git it runs.
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return store


def _ambient_username(store: Path) -> str | None:
    """Who the ambient store would authenticate as. Username only -- never the secret.

    Parsed with `urlsplit` rather than by hand: git PERCENT-ENCODES the host, so a stored line reads
    `http://user:secret@forge.example%3a9000` and a substring match on `forge.example:9000` finds
    nothing. That is a fixture that would have reported "the ambient store is empty" -- i.e. the
    property holding -- for every possible implementation.
    """
    for line in store.read_text(encoding='utf-8').splitlines():
        parts = urllib.parse.urlsplit(line.strip())
        if urllib.parse.unquote(parts.netloc).endswith(_HOST) and parts.username:
            return parts.username
    return None


class TestTheAmbientStoreIsUnchanged:
    """THE DISCRIMINATING ASSERTION of item 1, and the whole reason this file exists."""

    def test_storing_four_role_credentials_leaves_the_humans_identity_intact(self, ambient) -> None:
        """The exact operation that broke the fleet. Four roles, one host, real store, real git.

        Before the repair this could not have passed for ANY implementation that used the ambient
        store: four `approve` calls against one host leave one entry, and it is not the human's.
        """
        for role in ('agent', 'verifier', 'observer', 'integrator'):
            swarmctl.store_credential(_SCHEME, _HOST, f'swarm-{role}', f'token-for-{role}')

        assert _ambient_username(ambient) == 'MingyangBao', (
            "onboarding overwrote the operator's credential for this host -- the defect measured "
            '2026-08-11, which surfaced four days later on another machine as an install failure'
        )
        assert 'token-for-' not in ambient.read_text(encoding='utf-8'), 'a role token reached the ambient store'

    def test_the_four_roles_all_survive_rather_than_overwriting_each_other(self, ambient) -> None:
        """The other half, and it is what a per-host store CANNOT do.

        A store keyed by (protocol, host) holds one credential. That is not a GCM quirk -- it is the
        credential protocol -- so the four roles could never have coexisted there. Keying by username
        is the fix, and this asserts all four are retrievable, not merely that the last one is.
        """
        for role in ('agent', 'verifier', 'observer', 'integrator'):
            swarmctl.store_credential(_SCHEME, _HOST, f'swarm-{role}', f'token-for-{role}')
        for role in ('agent', 'verifier', 'observer', 'integrator'):
            assert swarmctl.read_credential(_SCHEME, _HOST, f'swarm-{role}') == f'token-for-{role}'

    def test_erasing_a_role_credential_does_not_erase_the_humans(self, ambient) -> None:
        """`revoke --erase-local` and `destroy` both erase. Against the ambient store they erased
        the entry for the HOST -- i.e. whoever held it, which after enrolment was the human's
        replacement and before it was the human.
        """
        swarmctl.store_credential(_SCHEME, _HOST, 'swarm-agent', 'token-for-agent')
        swarmctl.erase_credential(_SCHEME, _HOST, 'swarm-agent')
        assert swarmctl.read_credential(_SCHEME, _HOST, 'swarm-agent') is None
        assert _ambient_username(ambient) == 'MingyangBao'

    def test_reading_a_role_credential_does_not_pick_up_the_humans(self, ambient) -> None:
        """THE READ DIRECTION, and it is not symmetric with the write.

        A read that fell through to the ambient store would silently authenticate as whatever the
        human happens to hold -- succeeding, plausibly, as the wrong identity. That is worse than
        failing: it is the shape that made `_credential` return `swarm-verifier` to a client that
        believed it was `swarm-agent`.
        """
        assert swarmctl.read_credential(_SCHEME, _HOST, 'MingyangBao') is None, (
            'the ambient store was consulted; a fleet process can now authenticate as the operator'
        )


class TestTheTokenIsExplicitPerInvocation:
    def test_the_environment_supplies_a_role_token_without_any_store_at_all(self, tmp_path, monkeypatch) -> None:
        """The mechanism the directive asks for: carried explicitly, persisted nowhere."""
        monkeypatch.setattr(credentials, 'store_path', lambda: tmp_path / 'absent.json')
        monkeypatch.setenv('SWARM_TOKEN_AGENT', 'from-the-environment')
        assert credentials.resolve_token(_SCHEME, _HOST, 'swarm-agent') == 'from-the-environment'

    def test_the_environment_BEATS_the_store(self, tmp_path) -> None:
        """Precedence stated as a test, because a per-invocation override that loses to a persisted
        value is not an override -- it is a suggestion, and the caller cannot tell.
        """
        store = tmp_path / 'credentials.json'
        credentials.store_token(_SCHEME, _HOST, 'swarm-agent', 'from-the-store', path=store)
        assert (
            credentials.resolve_token(
                _SCHEME, _HOST, 'swarm-agent', env={'SWARM_TOKEN_AGENT': 'from-the-environment'}, path=store
            )
            == 'from-the-environment'
        )

    def test_the_env_var_name_is_derived_and_not_spelled_by_hand(self) -> None:
        assert credentials.env_var_for('swarm-agent') == 'SWARM_TOKEN_AGENT'
        assert credentials.env_var_for('swarmctl-admin') == 'SWARM_TOKEN_SWARMCTL_ADMIN'


class TestGitAuthenticatesAsTheRoleWithoutTheVault:
    """The git side. A fleet `push` must authenticate as its role and leave the box's store alone."""

    @pytest.mark.skipif(os.name == 'nt', reason='the POSIX launcher; the .cmd path is asserted below')
    def test_git_receives_the_role_token_through_askpass(self, tmp_path, monkeypatch) -> None:
        """END TO END through REAL git: the token reaches git, from the environment, via ASKPASS.

        `git credential fill` with our env must answer with the ROLE's token -- proving the handoff
        works -- while the helper list is empty, proving it came from ASKPASS and not from a store.
        """
        monkeypatch.setattr(credentials, 'store_path', lambda: tmp_path / 'absent.json')
        with credentials.git_env_for(
            _SCHEME, _HOST, 'swarm-agent', env={**os.environ, 'SWARM_TOKEN_AGENT': 'role-token-abc'}
        ) as environ:
            filled = _git(
                environ, 'credential', 'fill', stdin=f'protocol={_SCHEME}\nhost={_HOST}\nusername=swarm-agent\n\n'
            )
        assert 'password=role-token-abc' in filled.stdout, filled.stderr

    def test_the_helper_list_is_CLEARED_so_there_is_nothing_to_write_into(self, tmp_path, monkeypatch) -> None:
        """THE HALF THAT MAKES THE PROPERTY PROVABLE rather than merely intended.

        Clearing `credential.helper` for the invocation means git has no helper to read the
        operator's vault with and none to store into -- so the ambient credential is unchanged even
        if git decided to cache, rather than because our code politely declined to write.
        """
        monkeypatch.setattr(credentials, 'store_path', lambda: tmp_path / 'absent.json')
        with credentials.git_env_for(
            _SCHEME, _HOST, 'swarm-agent', env={**os.environ, 'SWARM_TOKEN_AGENT': 'role-token-abc'}
        ) as environ:
            assert environ['GIT_CONFIG_KEY_0'] == 'credential.helper'
            assert environ['GIT_CONFIG_VALUE_0'] == '', 'a non-empty helper does not RESET the list'
            assert environ['GIT_CONFIG_COUNT'] == '1'
            assert environ['GIT_TERMINAL_PROMPT'] == '0'

    def test_the_token_is_never_on_a_command_line(self, tmp_path, monkeypatch) -> None:
        """A command line is visible to every process on the box and lands in shell history --
        which is why this package already refuses a `--admin-password` flag. The launcher file must
        carry no secret either; it reads one from its own environment.
        """
        monkeypatch.setattr(credentials, 'store_path', lambda: tmp_path / 'absent.json')
        with credentials.git_env_for(
            _SCHEME, _HOST, 'swarm-agent', env={**os.environ, 'SWARM_TOKEN_AGENT': 'role-token-abc'}
        ) as environ:
            launcher = Path(environ['GIT_ASKPASS'])
            assert 'role-token-abc' not in launcher.read_text(encoding='utf-8')

    def test_the_launcher_is_cleaned_up(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(credentials, 'store_path', lambda: tmp_path / 'absent.json')
        with credentials.git_env_for(
            _SCHEME, _HOST, 'swarm-agent', env={**os.environ, 'SWARM_TOKEN_AGENT': 'x'}
        ) as environ:
            launcher = Path(environ['GIT_ASKPASS'])
            assert launcher.exists()
        assert not launcher.exists()

    def test_a_missing_token_is_NAMED_rather_than_left_to_git(self, tmp_path, monkeypatch) -> None:
        """Falling through to git would prompt, hang, or authenticate as whoever the vault holds.
        All three are worse than a sentence naming the role and the variable that supplies it.
        """
        monkeypatch.setattr(credentials, 'store_path', lambda: tmp_path / 'absent.json')
        with pytest.raises(LookupError, match='SWARM_TOKEN_AGENT'):
            with credentials.git_env_for(_SCHEME, _HOST, 'swarm-agent', env={}):
                pass  # pragma: no cover -- the context manager must not open


def test_no_route_to_the_ambient_store_survives_in_the_source() -> None:
    """A SOURCE-LEVEL BACKSTOP, and it is deliberately the WEAKER of the two checks.

    The behavioural tests above are the real guard, but they can only see routes that go through
    git. A future call reaching the Windows vault directly -- `cmdkey`, DPAPI, a keyring package --
    would leave them all green. This names the known spellings so that reintroducing one is at
    least loud.

    SEARCH SCOPE: every `.py` file under `src/agent_swarm`, which is the whole importable package.
    It does NOT cover the `swarmctl`/`swarmctl.cmd` shims (they only locate an interpreter) and it
    cannot cover a spelling nobody has thought of.

    MATCHED ON THE ARGUMENT-LIST SHAPE, never on the prose. Every docstring in this package that
    RECORDS the removed defect necessarily spells `git credential approve`, and a substring search
    would fire on the history rather than on a call site -- a guard that cannot distinguish the two
    forces the record to be deleted to stay green, which is how a measured failure loses its
    explanation. `'credential', 'approve'` as adjacent quoted list items is only ever code.
    """
    argv_write = re.compile(r"""['"]credential['"]\s*,\s*['"](approve|reject)['"]""")
    direct_vault = re.compile(r"""['"](cmdkey|wincred)['"]|^\s*import\s+keyring\b""", re.MULTILINE)
    package = Path(credentials.__file__).parent
    offenders = []
    for path in sorted(package.rglob('*.py')):
        text = path.read_text(encoding='utf-8')
        offenders += [f'{path.name}: git credential {m}' for m in argv_write.findall(text)]
        offenders += [f'{path.name}: {m[0] or "keyring"}' for m in direct_vault.finditer(text)]
    assert not offenders, 'a route back into the ambient credential store: ' + ', '.join(offenders)
