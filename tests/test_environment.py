"""What a verdict was earned IN, and when yours may reuse it.

PROVENANCE. Extracted from motronics' `scripts/ci/ci.py`, where the cluster was written in answer to
a measured waste: every box had a different environment, every difference forced its own verdict
namespace, and the same tree was therefore re-tested from scratch on each machine. These tests pin
the GRADING that replaced it, and the two places where "unknown" must cost a re-run.

THE PART THAT IS NEW HERE rather than carried over is the last section: the cluster had two project
facts welded into it as module constants, and the extraction turns them into required arguments. A
default is the mechanism by which such a coupling goes invisible -- every caller that omits it gets
somebody else's project and nothing ever fails to say so -- so the tests assert the `TypeError`, and
each one has a discriminating half proving the function still works when told.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from agent_swarm import environment
from agent_swarm.environment import (
    OUTSIDE_CLOSURE,
    canonical,
    compute_envkey,
    env_diff,
    env_is_reusable,
    env_manifest,
    format_env_diff,
    interpreter_identity,
    local_digest,
    project_closure,
    split_manifest,
)


#: A manifest is `interpreter`, then `name==version` rows, then `path=digest` rows.
BASE = [
    'cpython-3.13.0-win32',
    'jupyterlab==4.0.0',
    'numpy==2.0.0',
    'config/lab.toml=abc123',
]

#: What the project can REACH. `jupyterlab` is deliberately outside it: it is the package everyone
#: has a different version of and no test can observe.
CLOSURE = frozenset({'someproject', 'numpy'})


def _with(*, drop: str = '\x00', add: str | None = None) -> list[str]:
    """`BASE` with the line starting `drop` removed and `add` inserted, re-sorted like a manifest."""
    kept = [line for line in BASE if not line.startswith(drop)]
    return [kept[0], *sorted([*kept[1:], *([add] if add else [])])]


class _Dist:
    """A distribution double. `metadata` is a mapping and `version` a string -- the only two things
    `env_manifest` reads, and it reads them with `getattr` precisely so this can be this small."""

    def __init__(self, name: str | None, version: str | None):
        self.metadata = {'Name': name} if name is not None else {}
        self.version = version


# --------------------------------------------------------------------- the manifest and its key


def test_the_key_is_the_hash_OF_the_manifest():
    """One derivation, two consumers. A key computed in parallel with the manifest could describe a
    different environment from the manifest stored beside it, and nothing would ever notice."""
    manifest = env_manifest([_Dist('numpy', '2.0.0')])
    assert compute_envkey(manifest) == hashlib.sha256('\n'.join(manifest).encode()).hexdigest()[:16]


def test_the_manifest_is_READABLE_not_a_digest():
    """The whole reason it is stored: a hash cannot be diffed."""
    assert 'numpy==2.0.0' in env_manifest([_Dist('numpy', '2.0.0')])


def test_the_manifest_leads_with_the_interpreter():
    assert env_manifest([])[0] == interpreter_identity()


def test_the_manifest_SORTS_its_distributions():
    """`importlib.metadata` promises nothing about order. Unsorted, the same environment keys
    differently per process and every lookup misses for a reason nobody can reproduce."""
    forward = env_manifest([_Dist('a', '1'), _Dist('z', '9')])
    backward = env_manifest([_Dist('z', '9'), _Dist('a', '1')])
    assert forward == backward


def test_an_UNREADABLE_distribution_is_recorded_rather_than_dropped():
    """The failure direction. Dropped, a corrupt install hashes identically to not having the
    package -- which is the direction that serves an unearned green."""
    assert 'UNREADABLE==UNREADABLE' in env_manifest([_Dist(None, None)])


def test_the_local_lines_are_part_of_the_key():
    """Machine-local test inputs are not in any tree hash, so if they are not here they are nowhere."""
    assert compute_envkey(env_manifest([], local_lines=['config/lab.toml=aa'])) != compute_envkey(env_manifest([]))


# --------------------------------------------------------------------- the diff, and its grades


def test_an_identical_environment_yields_NO_difference():
    assert env_diff(BASE, BASE, CLOSURE) == []
    assert env_is_reusable(env_diff(BASE, BASE, CLOSURE))


def test_a_package_the_project_CANNOT_REACH_does_not_block_reuse():
    """The grade that pays for the whole mechanism: differs, and say what differed, and reuse."""
    changes = env_diff(BASE, _with(drop='jupyterlab'), CLOSURE)
    assert [c.item for c in changes] == ['jupyterlab']
    assert changes[0].verdict == OUTSIDE_CLOSURE
    assert env_is_reusable(changes)


def test_a_package_the_project_DEPENDS_ON_blocks_reuse():
    changes = env_diff(BASE, _with(drop='numpy==', add='numpy==2.1.0'), CLOSURE)
    assert [c.item for c in changes] == ['numpy']
    assert not env_is_reusable(changes)


def test_the_difference_NAMES_BOTH_VERSIONS():
    """A reader must learn WHAT differed, not only that something did."""
    (change,) = env_diff(BASE, _with(drop='numpy==', add='numpy==2.1.0'), CLOSURE)
    assert (change.theirs, change.mine) == ('2.0.0', '2.1.0')


def test_ABSENT_is_distinguished_from_a_version_change():
    """`None` on one side, and the render says ABSENT rather than printing nothing."""
    (change,) = env_diff(BASE, _with(drop='jupyterlab'), CLOSURE)
    assert change.mine is None
    assert 'ABSENT' in format_env_diff([change])[0]


def test_a_distribution_name_is_CANONICALISED():
    """PEP 503. Two spellings of one distribution would otherwise read as a removal plus an
    addition, for a package nobody touched."""
    assert env_diff(['Jupyter_Lab==4.0.0'], ['jupyter-lab==4.0.0'], CLOSURE) == []
    assert canonical('Foo_Bar.Baz') == 'foo-bar-baz'


def test_an_EMPTY_closure_means_cannot_tell_and_refuses_reuse():
    """Not "nothing is relevant". An empty closure is what a missing project install produces, and
    reading it as permissive would reuse verdicts across unrelated environments."""
    changes = env_diff(BASE, _with(drop='jupyterlab'), frozenset())
    assert changes[0].blocks_reuse
    assert not env_is_reusable(changes)


def test_a_MACHINE_LOCAL_test_input_always_blocks_reuse():
    """No dependency graph has anything to say about a file the tests read off the disk."""
    changes = env_diff(BASE, _with(drop='config/lab.toml', add='config/lab.toml=deadbeef'), CLOSURE)
    assert [c.item for c in changes] == ['config/lab.toml']
    assert not env_is_reusable(changes)


def test_a_DIFFERENT_INTERPRETER_always_blocks_reuse():
    """The row with no separator. It is keyed under `interpreter` rather than parsed as a package,
    so it can never be silently dropped."""
    changes = env_diff(BASE, _with(drop='cpython-3.13', add='cpython-3.12.0-linux'), CLOSURE)
    assert [c.item for c in changes] == ['interpreter']
    assert not env_is_reusable(changes)


def test_the_interpreter_row_survives_the_split():
    dists, files = split_manifest(BASE)
    assert files['interpreter'] == 'cpython-3.13.0-win32'
    assert dists['numpy'] == '2.0.0'


# --------------------------------------------------------------------- the render


def test_the_render_puts_BLOCKING_entries_first():
    """A silent alphabetical truncation would hide the entries that cost a re-run behind the ones
    that do not."""
    changes = env_diff(BASE, [*_with(drop='numpy==', add='numpy==2.1.0'), 'aaa-irrelevant==1.0'], CLOSURE)
    assert format_env_diff(changes)[0].strip().startswith('! numpy')


def test_the_render_SAYS_when_it_truncates():
    """And how many of the hidden ones matter, which is the only number the reader needs."""
    many = [f'pkg{i}==1.0' for i in range(40)]
    rendered = format_env_diff(env_diff(BASE, sorted([*BASE, *many]), CLOSURE), limit=5)
    assert len(rendered) == 6
    assert rendered[-1].strip().startswith('... 35 more')


# --------------------------------------------------------------------- the closure


def test_the_closure_reaches_a_real_dependency_of_an_installed_distribution():
    """Against the real graph rather than a double, because the thing being tested is the walk over
    `Requires-Dist` and a double would agree with whatever the walk did."""
    closure = project_closure('pytest')
    assert 'pytest' in closure
    assert 'pluggy' in closure


def test_an_uninstallable_root_yields_the_bare_root_not_a_crash():
    """Cannot-tell has to be REPRESENTABLE. A crash here would make a runner that has not yet
    installed the project unable to ask the question at all."""
    assert project_closure('a-distribution-nobody-has-published') == frozenset({'a-distribution-nobody-has-published'})


def test_the_closure_canonicalises_its_root():
    assert 'pytest' in project_closure('PyTest')


# --------------------------------------------------------------------- the local digest


def test_the_local_digest_walks_a_directory_and_hashes_a_file(tmp_path: Path):
    (tmp_path / 'config').mkdir()
    (tmp_path / 'config' / 'lab.toml').write_text('x', encoding='utf-8')
    (tmp_path / 'uv.lock').write_text('y', encoding='utf-8')
    lines = local_digest(tmp_path, ('config', 'uv.lock'))
    assert [line.split('=')[0] for line in lines] == ['config/lab.toml', 'uv.lock']
    assert all(len(line.split('=')[1]) == 16 for line in lines)


def test_the_local_digest_is_relative_to_the_root_it_was_given(tmp_path: Path):
    """POSIX-separated and root-relative, or the same file keys differently on two machines purely
    from where the checkout lives."""
    (tmp_path / 'docs').mkdir()
    (tmp_path / 'docs' / 'a.md').write_text('x', encoding='utf-8')
    assert local_digest(tmp_path, ('docs',))[0].startswith('docs/a.md=')


def test_an_ABSENT_local_path_contributes_nothing(tmp_path: Path):
    """A gitignored input is genuinely absent in a fresh worktree, and that must not be a crash."""
    assert local_digest(tmp_path, ('config', 'uv.lock')) == []


def test_the_digest_CHANGES_when_the_file_does(tmp_path: Path):
    """The discriminating half: a digest function that returned a constant would satisfy every
    test above."""
    (tmp_path / 'uv.lock').write_text('y', encoding='utf-8')
    before = local_digest(tmp_path, ('uv.lock',))
    (tmp_path / 'uv.lock').write_text('z', encoding='utf-8')
    assert local_digest(tmp_path, ('uv.lock',)) != before


# --------------------------------------------------------------------- the project facts are ARGUMENTS


def test_the_closure_will_not_guess_a_root():
    """The extraction's whole point. A default root would be `DEFAULT_REPO` wearing a different
    noun: invisible, and wrong for every consumer but one."""
    with pytest.raises(TypeError):
        project_closure()  # type: ignore[call-arg]


def test_the_local_digest_will_not_guess_a_root_or_its_paths():
    with pytest.raises(TypeError):
        local_digest()  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        local_digest(Path('.'))  # type: ignore[call-arg]


def test_the_diff_will_not_guess_a_closure():
    """The dangerous default of the three. A `closure=None` that computed one for whichever
    distribution the module named would grade every difference against the wrong graph and return a
    confident answer."""
    with pytest.raises(TypeError):
        env_diff(BASE, BASE)  # type: ignore[call-arg]


def test_the_module_holds_no_project_constant():
    """The converse, because a leftover constant nothing reaches for is still the value the next
    caller reaches for. Guarded generally by `test_this_package_names_no_specific_project`; named
    here so the two specific spellings that were welded in cannot come back under a new one."""
    assert not hasattr(environment, 'PROJECT_DISTRIBUTION')
    assert not hasattr(environment, 'ENV_LOCAL_PATHS')
