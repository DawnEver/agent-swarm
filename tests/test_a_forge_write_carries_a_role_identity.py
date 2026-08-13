"""A forge write carries a role identity, and choosing the box's credential must be TYPED.

WHAT WAS MEASURED, 2026-08-13, in the consumer rather than here. `credentials.git_env_for` -- this
package's own way to hand git one role's askpass -- had ZERO call sites in motronics. Every forge
write `GitRefStore` made therefore went out under whatever credential the box happened to hold. The
consumers are `liveness`, `submission`, `workbench_cli` and `integration`, and `integration` is the
CAS advance: the one shape that changes `main`. So the plane that lands code authenticated as nobody
in particular, and it was invisible because on a single-human box the ambient credential answers.

WHY A REQUIRED ARGUMENT AND NOT A DEFAULT. `identity=ambient_identity` as a default would have kept
every consumer working and left the behaviour exactly as measured -- which is the objection, not the
convenience. This package already deleted a default of that shape from `default_forge`, for the
reason its docstring gives: a default that works silently is invisible in the code, in review, and
in the numbers. Requiring it makes "use the box's credential" something a consumer TYPES.

THE PREDICATE IS REUSED, NOT RE-INVENTED. The identity applies exactly where `mutates_the_forge` is
true -- the same test that already gates withholding. A second notion of "a write" is how two guards
end up disagreeing about one argv.
"""

from __future__ import annotations

import contextlib
import subprocess
from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest

from agent_swarm.refstore import GIT_TIMEOUT_S, GitRefStore, ambient_identity

_GIT = 'git'


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        [_GIT, '-C', str(root), *args], capture_output=True, text=True, check=True, timeout=60
    ).stdout.strip()


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    root = tmp_path / 'work'
    root.mkdir()
    _git(root.parent, 'init', '-b', 'main', str(root))
    _git(root, 'config', 'user.email', 'test@example.invalid')
    _git(root, 'config', 'user.name', 'test')
    (root / 'a.txt').write_text('a\n', encoding='utf-8')
    _git(root, 'add', '-A')
    _git(root, 'commit', '-m', 'first')
    return root


def _recording_identity(seen: list[str]):
    """An identity that records that it was ENTERED, and hands back a marker git will carry."""

    @contextlib.contextmanager
    def identity() -> Iterator[Mapping[str, str]]:
        seen.append('entered')
        yield {'SWARM_TEST_IDENTITY': 'the-integrator'}

    return identity


def _store(root: Path, identity) -> GitRefStore:
    return GitRefStore(root, 'origin', withhold_writes=lambda: False, identity=identity)


def test_identity_has_NO_DEFAULT(checkout: Path) -> None:
    """The whole point. A default would have left the measured behaviour in place, silently."""
    with pytest.raises(TypeError, match='identity'):
        GitRefStore(checkout, 'origin', withhold_writes=lambda: False)  # type: ignore[call-arg]


def test_a_WRITE_enters_the_identity(checkout: Path, monkeypatch) -> None:
    """The direction that matters: a push must go out as somebody.

    `push` is asserted through the real `run`, against a remote that does not exist -- the call
    FAILS, deliberately. What is under test is that the identity was entered before git was spawned,
    and a test needing a live forge to prove that would be a test nobody runs.
    """
    seen: list[str] = []
    store = _store(checkout, _recording_identity(seen))

    captured = {}

    def fake_run(argv, **kwargs):
        captured['env'] = kwargs.get('env')
        captured['timeout'] = kwargs.get('timeout')
        return subprocess.CompletedProcess(argv, 1, stdout='', stderr='no such remote')

    monkeypatch.setattr(subprocess, 'run', fake_run)
    store.run('push', 'origin', 'HEAD:refs/ci/probe/1')

    assert seen == ['entered'], 'a forge write must be made under an identity'
    assert captured['env'] is not None, 'a write must be given an environment, not inherit ambiently'
    assert captured['env']['SWARM_TEST_IDENTITY'] == 'the-integrator'


def test_a_WRITE_keeps_the_inherited_environment(checkout: Path, monkeypatch) -> None:
    """MERGED over `os.environ`, never replacing it.

    git needs PATH, HOME and the rest to run at all, so a hand-built environment would be a second
    declaration of what git requires -- right until the day git needs one more variable, and then
    wrong in a way that reads as a credential failure.
    """
    monkeypatch.setenv('A_VARIABLE_GIT_MIGHT_NEED', 'present')
    store = _store(checkout, _recording_identity([]))

    captured = {}
    monkeypatch.setattr(
        subprocess,
        'run',
        lambda argv, **kw: captured.setdefault('env', kw.get('env')) or subprocess.CompletedProcess(argv, 0),
    )
    store.run('push', 'origin', 'HEAD:refs/ci/probe/2')

    assert captured['env']['A_VARIABLE_GIT_MIGHT_NEED'] == 'present'
    assert captured['env']['SWARM_TEST_IDENTITY'] == 'the-integrator'


def test_a_READ_does_NOT_enter_the_identity(checkout: Path) -> None:
    """The discriminating half. Reads already worked on the ambient credential and are the bulk of
    the calls; wrapping them would pay an askpass launch per `ls-remote` to answer a question the
    server answers for any credential that can clone.

    Without this test, `test_a_WRITE_enters_the_identity` passes against a store that wraps
    EVERYTHING -- which is a different design, and a slower one, with no evidence for it here.
    """
    seen: list[str] = []
    store = _store(checkout, _recording_identity(seen))
    assert store.text('rev-parse', 'HEAD')
    assert seen == [], 'a read must not pay for an identity it does not need'


def test_a_WITHHELD_write_does_not_enter_the_identity_either(checkout: Path) -> None:
    """A rehearsal spawns nothing, so it must not mint a credential either.

    An askpass launch is a side effect on the OS credential store, and a `--dry-run` that touched it
    would be the rehearsal-is-indistinguishable-from-a-real-run defect wearing different clothes.
    """
    seen: list[str] = []
    store = GitRefStore(checkout, 'origin', withhold_writes=lambda: True, identity=_recording_identity(seen))
    done = store.run('push', 'origin', 'HEAD:refs/ci/probe/3')
    assert done.returncode == 0, 'a withheld write still returns SUCCESS-shaped, as before'
    assert seen == [], 'a rehearsal must not enter an identity'


def test_every_call_is_BOUNDED(checkout: Path, monkeypatch) -> None:
    """A forge write with no timeout is a runner that stops ticking without saying why.

    `subprocess.run` waits forever; the clock's `proc.poll()` keeps returning None; liveness keeps
    beating for a process that will never return. Measured absent alongside `env=` on 2026-08-13.
    """
    captured = {}
    monkeypatch.setattr(
        subprocess,
        'run',
        lambda argv, **kw: captured.setdefault('timeout', kw.get('timeout')) or subprocess.CompletedProcess(argv, 0),
    )
    _store(checkout, ambient_identity).run('rev-parse', 'HEAD')
    assert captured['timeout'] == GIT_TIMEOUT_S


def test_a_CALLER_may_still_choose_its_own_timeout(checkout: Path, monkeypatch) -> None:
    """`setdefault`, not an assignment: a caller that knows its call is slower keeps saying so."""
    captured = {}
    monkeypatch.setattr(
        subprocess,
        'run',
        lambda argv, **kw: captured.setdefault('timeout', kw.get('timeout')) or subprocess.CompletedProcess(argv, 0),
    )
    _store(checkout, ambient_identity).run('rev-parse', 'HEAD', timeout=7)
    assert captured['timeout'] == 7


def test_ambient_identity_yields_nothing_and_says_why(checkout: Path) -> None:
    """It is a NAMED choice, so it must be usable -- and its docstring must carry the cost.

    A sentinel that silently did the old thing with no explanation would be the default it replaced,
    renamed.
    """
    with ambient_identity() as extra:
        assert dict(extra) == {}
    assert 'A choice, not a default' in (ambient_identity.__doc__ or '')
    assert 'audit trail' in (ambient_identity.__doc__ or '')
