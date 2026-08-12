"""The publication check must catch the real shape: an import written against a sibling's SOURCE.

MOVED HERE WITH THE CODE from motronics' `tests/unit/scripts/test_check_installed_symbols.py`,
2026-08-12. The defect it exists for was measured that day: a test file naming a symbol that had
landed in a sibling repository's working tree but not in the INSTALLED copy of it. The file had never
once been importable and took its module down at collection.

These tests drive the REAL resolver against the REAL installed environment rather than mocking
``importlib``. A mocked import would be the test checking its own copy of the question -- and the
question here is precisely "what does this environment actually have", which only the environment can
answer.

THE PACKAGE UNDER TEST IS `pytest`, DELIBERATELY. Pointing these at `agent_swarm` itself would make
them a claim about how THIS suite happens to be installed (editable, on `pythonpath`, or from a pin),
which is a different question and one that varies per box. `pytest` is present by construction
wherever these run, and using a package this one has no relationship with is what shows the
extraction really took the package name as an argument.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from agent_swarm.installed_symbols import installed_revision, main, requested, unresolvable_imports

PACKAGE = 'pytest'
DISTRIBUTION = 'pytest'
REINSTALL = 'uv pip install --reinstall-package pytest'

_A_REAL_MODULE = 'pytest'
_A_NAME_THAT_CANNOT_EXIST = 'A_SYMBOL_THAT_HAS_NEVER_EXISTED'


def _write(tmp_path: Path, name: str, source: str) -> Path:
    path = tmp_path / name
    path.write_text(source, encoding='utf-8')
    return path


def test_a_symbol_the_installed_copy_lacks_is_refused(tmp_path: Path) -> None:
    path = _write(tmp_path, 'user.py', f'from {_A_REAL_MODULE} import {_A_NAME_THAT_CANNOT_EXIST}\n')
    assert unresolvable_imports([path], package=PACKAGE) == {
        str(path): [f'from {_A_REAL_MODULE} import {_A_NAME_THAT_CANNOT_EXIST}']
    }


def test_a_symbol_the_installed_copy_HAS_is_allowed(tmp_path: Path) -> None:
    # The converse, and it is what stops this check being a blanket refusal of the dependency.
    path = _write(tmp_path, 'ok.py', f'from {_A_REAL_MODULE} import fixture\n')
    assert unresolvable_imports([path], package=PACKAGE) == {}


def test_a_submodule_imported_by_name_resolves(tmp_path: Path) -> None:
    # `from X import Y` where Y is a MODULE, not an attribute. A resolver that only asked `hasattr`
    # would refuse this whenever the parent package had not already imported it -- a false refusal
    # that trains readers to ignore the check.
    path = _write(tmp_path, 'sub.py', 'from _pytest import config\n')
    assert unresolvable_imports([path], package='_pytest') == {}


def test_a_module_that_does_not_exist_is_refused(tmp_path: Path) -> None:
    path = _write(tmp_path, 'gone.py', f'import {PACKAGE}.a_module_that_has_never_existed\n')
    assert unresolvable_imports([path], package=PACKAGE) == {
        str(path): [f'import {PACKAGE}.a_module_that_has_never_existed']
    }


def test_an_import_inside_a_function_body_counts(tmp_path: Path) -> None:
    # It fails just as hard, one call later. A module-level-only scan would pass on the lazy-import
    # spelling that heavy optional dependencies are legitimately written with.
    path = _write(tmp_path, 'lazy.py', f'def go():\n    from {_A_REAL_MODULE} import {_A_NAME_THAT_CANNOT_EXIST}\n')
    assert unresolvable_imports([path], package=PACKAGE) == {
        str(path): [f'from {_A_REAL_MODULE} import {_A_NAME_THAT_CANNOT_EXIST}']
    }


def test_imports_of_other_packages_are_not_this_checks_business(tmp_path: Path) -> None:
    # Widening to every dependency would report the whole of a mid-install environment and make the
    # output noisy enough to be ignored -- how a guard stops being consulted.
    path = _write(tmp_path, 'other.py', 'from json import a_thing_that_does_not_exist\nimport nope_not_a_package\n')
    assert unresolvable_imports([path], package=PACKAGE) == {}


def test_a_file_that_cannot_be_parsed_is_not_reported_here(tmp_path: Path) -> None:
    # A syntax error has a louder guard. Reporting it twice would make this check answer for
    # something it does not own.
    path = _write(tmp_path, 'broken.py', 'def (\n')
    assert unresolvable_imports([path], package=PACKAGE) == {}


def test_the_requested_imports_are_read_in_both_spellings() -> None:
    """A `None` SYMBOL IS THE DATA, NOT A HOLE, and this docstring exists because the first version
    of this test tripped over it and the tempting repair would have hidden the answer.

    `import X` and `from X import Y` are two different questions -- the first is satisfied by the
    MODULE existing, the second additionally by the name -- so `requested` returns `(module, None)`
    for the plain spelling and `_resolves` short-circuits to True on it. That is declared in the
    return type (`list[tuple[str, str | None]]`), so nothing upstream is producing a row it should
    not. What was wrong was HERE: the assertion sorted the pairs, and with both imports naming the
    SAME module the tie-break compared `None` with a `str`.

    A `key=lambda r: (r[0], r[1] or '')` would have made the red go away and left the next reader
    believing the ordering was meaningful. There is no order in this result to assert -- it follows
    `ast.walk` -- so the assertion is on the SET, and the `None` is pinned separately below so it
    cannot be read as an accident of the comparison.
    """
    source = f'import {PACKAGE}.mark\nfrom {PACKAGE}.mark import skip\nimport json\n'
    wanted = requested(source, package=PACKAGE)

    assert set(wanted) == {(f'{PACKAGE}.mark', None), (f'{PACKAGE}.mark', 'skip')}
    assert (f'{PACKAGE}.mark', None) in wanted, 'the plain `import X` spelling must survive as a None symbol'
    assert len(wanted) == 2, f'each import statement is one row: {wanted}'


def test_a_relative_import_is_not_mistaken_for_the_package() -> None:
    # `from .pytest import x` inside some other package is a different module entirely; the level
    # guard is what keeps this from refusing a name it never had an opinion about.
    assert requested(f'from .{PACKAGE} import thing\n', package=PACKAGE) == []


def test_the_installed_revision_reads_the_DISTRIBUTION_and_says_when_it_cannot() -> None:
    """It never invents provenance. A distribution installed from an index HAS no `direct_url.json`,
    and one that is absent is absent -- both are reported in words rather than as an empty string a
    caller would print as if it were a commit.
    """
    assert installed_revision(DISTRIBUTION)
    assert installed_revision('a-distribution-that-has-never-existed') == 'NOT INSTALLED'


def _git(repo: Path, *args: str) -> None:
    subprocess.run([shutil.which('git') or 'git', *args], cwd=repo, check=True, capture_output=True, timeout=60)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, 'init', '-q')
    _git(tmp_path, 'config', 'user.email', 't@t')
    _git(tmp_path, 'config', 'user.name', 't')
    (tmp_path / 'keep.py').write_text('', encoding='utf-8')
    _git(tmp_path, 'add', '-A')
    _git(tmp_path, 'commit', '-qm', 'base')
    return tmp_path


def test_main_refuses_a_STAGED_file_and_names_the_CALLERS_remedy(repo: Path, capsys) -> None:
    """END TO END through the CLI, in a real git repository, because the whole claim is about what
    git reports as staged -- a mocked index would test a copy of the logic instead of the guard.

    It also pins that the printed remedy is the one the CALLER passed. A refusal whose remedy this
    package invented would send the reader to a command their project does not use.
    """
    (repo / 'bad.py').write_text(f'from {_A_REAL_MODULE} import {_A_NAME_THAT_CANNOT_EXIST}\n', encoding='utf-8')
    _git(repo, 'add', 'bad.py')

    code = main(['--repo', str(repo)], package=PACKAGE, distribution=DISTRIBUTION, reinstall_command=REINSTALL)
    assert code == 1

    out = capsys.readouterr().out
    assert 'REFUSED' in out and _A_NAME_THAT_CANNOT_EXIST in out
    assert REINSTALL in out, 'the refusal must name the remedy, not the rule'


def test_main_passes_a_clean_index(repo: Path) -> None:
    (repo / 'good.py').write_text(f'from {_A_REAL_MODULE} import fixture\n', encoding='utf-8')
    _git(repo, 'add', 'good.py')

    assert main(['--repo', str(repo)], package=PACKAGE, distribution=DISTRIBUTION, reinstall_command=REINSTALL) == 0
