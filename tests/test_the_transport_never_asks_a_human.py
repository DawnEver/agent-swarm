"""No git call this package makes may open a credential prompt. Including a READ.

THE CLAIM THIS REFUTES IS MY OWN, WRITTEN THE SAME DAY. `GitRefStore.run` applied the role identity
only to forge-MUTATING verbs, and the comment on the read branch said reads "keep the ambient
environment [...] to answer a question the server answers for any credential that can clone". The
performance half is true and is kept. The safety half is false, and the word that carries it is ANY:
it presupposes an ambient credential EXISTS. On a fresh fleet box none does -- and a read against a
remote spelled `http://<user>@<forge>/...` then does not fail, it PROMPTS.

MEASURED 2026-08-13 on WS2. `git fetch origin` -- a READ -- raised a Git Credential Manager window on
a human's desktop asking for the swarm-agent password, and hung until a 90 s timeout killed it. The
step then reported `TimeoutExpired`, which reads as an unreachable forge. The forge was reachable.

WHY THAT IS WORSE THAN FAILING. The OS credential store is keyed on HOST alone while four role
identities share this forge, so a human who answers the prompt displaces their OWN credential for
that host -- and learns days later, in another repository, from a 404 that reads as a deleted repo.
A prompt is not a slower failure; it is a DIFFERENT and worse outcome than failing.

AND THE KNOWLEDGE ALREADY EXISTED. `installer.py` carries this exact constant, with a comment
recording the same failure measured 2026-08-11 ("a helper raised a window and the command hung until
it was killed"), and `credentials.git_env_for` sets the terminal half. Three spellings, and the
busiest forge path in the package had none of them -- which is the duplicated-scheme shape: a rule
that has to be remembered at each new site is a rule that will be missed at one.

THE IDENTITY IS STILL WRITE-ONLY, deliberately, and that is not a hole this file papers over. What
changes is the FALLBACK: a read with no ambient credential now fails cleanly instead of asking. The
cost argument for not launching an askpass per `ls-remote` is untouched.
"""

from __future__ import annotations

import subprocess

import contextlib

from agent_swarm import refstore
from agent_swarm.credentials import NON_INTERACTIVE


@contextlib.contextmanager
def _no_extra():
    yield {}


def _capture(monkeypatch) -> dict:
    seen: dict = {}

    def run(argv, **kwargs):
        seen['argv'] = argv
        seen['env'] = kwargs.get('env') or {}
        return subprocess.CompletedProcess(argv, 0, stdout='', stderr='')

    monkeypatch.setattr(refstore.subprocess, 'run', run)
    return seen


def _store(tmp_path, identity=_no_extra):
    return refstore.GitRefStore(tmp_path, 'origin', withhold_writes=lambda: False, identity=identity)


def test_a_READ_cannot_open_a_credential_prompt(tmp_path, monkeypatch) -> None:
    """THE ONE THAT WAS MEASURED. `fetch` and `ls-remote` are reads and they reach the network."""
    seen = _capture(monkeypatch)
    _store(tmp_path).run('fetch', 'origin')
    for key, value in NON_INTERACTIVE.items():
        assert seen['env'].get(key) == value, f'a read may not ask a human: {key} was {seen["env"].get(key)!r}'


def test_a_WRITE_cannot_open_one_either(tmp_path, monkeypatch) -> None:
    """A write already carried a role, which normally makes the prompt unreachable -- but "normally"
    is not a guarantee: a role whose token the server refuses falls through to the same helper. The
    two branches must not differ on this, or the safer-looking one is the one that prompts.
    """
    seen = _capture(monkeypatch)
    _store(tmp_path).run('push', 'origin', 'HEAD:refs/ci/x')
    for key, value in NON_INTERACTIVE.items():
        assert seen['env'].get(key) == value


def test_the_ROLE_IDENTITY_still_reaches_a_write(tmp_path, monkeypatch) -> None:
    """The discriminating half. Making every call non-interactive must not quietly drop the identity
    -- that would turn a working write into a clean failure, which is the correct-looking direction
    to break in and therefore the one nobody would check.
    """
    seen = _capture(monkeypatch)

    @contextlib.contextmanager
    def role():
        yield {'GIT_ASKPASS': 'role-askpass'}

    store = _store(tmp_path, identity=role)
    store.run('push', 'origin', 'HEAD:refs/ci/x')
    assert seen['env']['GIT_ASKPASS'] == 'role-askpass'
    assert seen['env']['GIT_TERMINAL_PROMPT'] == '0'


def test_a_read_still_INHERITS_the_ambient_environment(tmp_path, monkeypatch) -> None:
    """The cost argument is kept, and stating it as a test is what stops the next reader "tidying"
    reads onto the identity path: an askpass launch per `ls-remote` would be paid on the hottest
    call in the package, to answer a question the server answers for any credential it accepts.

    PATH is the probe because git cannot run without it -- a hand-built environment would be a
    second declaration of what git requires, which is the defect one level up.
    """
    seen = _capture(monkeypatch)
    monkeypatch.setenv('PATH', 'the-ambient-path')
    _store(tmp_path).run('ls-remote', 'origin')
    assert seen['env']['PATH'] == 'the-ambient-path'


def test_ONE_SPELLING_shared_with_the_installer(tmp_path) -> None:
    """The rule was written three times and missed at the fourth site. A second copy here would
    reproduce exactly the defect this file exists to close, one release later.
    """
    from agent_swarm import installer

    assert installer.NON_INTERACTIVE is NON_INTERACTIVE
