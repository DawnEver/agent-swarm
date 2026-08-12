"""The untracked-import check must catch the real shape, in a real git repo.

MOVED HERE WITH THE CODE from motronics' `tests/unit/scripts/test_check_untracked_imports.py`,
2026-08-12. The defect it exists for was measured twice in one session (2026-07-31): a new source
module left untracked while its importers were already staged. Every test passes locally -- the file
is on disk -- and the commit ships an ``ImportError`` to everyone else.

These tests build actual git repositories rather than mocking ``git``, because the whole check is a
claim about what git reports; a mocked index would be the test checking its own copy of the logic
instead of the guard.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from agent_swarm.staged_imports import dangling_imports, main, untracked_modules

#: The layout these fixtures use. Named once so the tests read as a CALLER supplying the fact, which
#: is the property the extraction was for: the module has no default and cannot guess this.
ROOTS = ('src/',)


def _git(repo: Path, *args: str) -> None:
    # timeout: a git call that hangs would park the whole suite at 0% CPU rather than failing.
    subprocess.run([shutil.which('git') or 'git', *args], cwd=repo, check=True, capture_output=True, timeout=60)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, 'init', '-q')
    _git(tmp_path, 'config', 'user.email', 't@t')
    _git(tmp_path, 'config', 'user.name', 't')
    (tmp_path / 'src' / 'pkg').mkdir(parents=True)
    (tmp_path / 'src' / 'pkg' / '__init__.py').write_text('', encoding='utf-8')
    _git(tmp_path, 'add', '-A')
    _git(tmp_path, 'commit', '-qm', 'base')
    return tmp_path


def test_a_staged_importer_of_an_untracked_module_is_refused(repo: Path) -> None:
    (repo / 'src' / 'pkg' / 'newmod.py').write_text('VALUE = 1\n', encoding='utf-8')
    (repo / 'src' / 'pkg' / 'user.py').write_text('from pkg.newmod import VALUE\n', encoding='utf-8')
    _git(repo, 'add', 'src/pkg/user.py')  # the importer only -- the module stays untracked

    assert dangling_imports(repo, importable_roots=ROOTS) == {'src/pkg/newmod.py': ['src/pkg/user.py']}


def test_staging_the_module_too_clears_it(repo: Path) -> None:
    (repo / 'src' / 'pkg' / 'newmod.py').write_text('VALUE = 1\n', encoding='utf-8')
    (repo / 'src' / 'pkg' / 'user.py').write_text('from pkg.newmod import VALUE\n', encoding='utf-8')
    _git(repo, 'add', 'src/pkg/user.py', 'src/pkg/newmod.py')

    assert dangling_imports(repo, importable_roots=ROOTS) == {}


def test_an_untracked_module_nobody_imports_is_not_an_error(repo: Path) -> None:
    # Not every new file is a hazard: an unimported module breaks no checkout. Reporting it would
    # make the check noisy enough to be ignored, which is how a guard stops being consulted.
    (repo / 'src' / 'pkg' / 'lonely.py').write_text('VALUE = 1\n', encoding='utf-8')

    assert untracked_modules(repo, importable_roots=ROOTS) == {'pkg.lonely': 'src/pkg/lonely.py'}
    assert dangling_imports(repo, importable_roots=ROOTS) == {}


def test_the_plain_import_spelling_is_caught_too(repo: Path) -> None:
    # `import pkg.newmod` and `from pkg import newmod` break identically; a check that only knew the
    # from-import spelling would pass on two thirds of the real call sites.
    (repo / 'src' / 'pkg' / 'newmod.py').write_text('VALUE = 1\n', encoding='utf-8')
    (repo / 'src' / 'pkg' / 'a.py').write_text('import pkg.newmod\n', encoding='utf-8')
    (repo / 'src' / 'pkg' / 'b.py').write_text('from pkg import newmod\n', encoding='utf-8')
    _git(repo, 'add', 'src/pkg/a.py', 'src/pkg/b.py')

    assert dangling_imports(repo, importable_roots=ROOTS) == {'src/pkg/newmod.py': ['src/pkg/a.py', 'src/pkg/b.py']}


def test_an_untracked_TEST_file_is_not_treated_as_an_import_hazard(repo: Path) -> None:
    # A missing test file is a lost safety net, not a broken checkout -- a different defect with a
    # different remedy, so this check does not conflate them.
    (repo / 'tests').mkdir()
    (repo / 'tests' / 'test_thing.py').write_text('def test_x(): pass\n', encoding='utf-8')

    assert untracked_modules(repo, importable_roots=ROOTS) == {}


def test_the_importable_roots_really_decide_what_is_scanned(repo: Path) -> None:
    """THE DISCRIMINATING TEST FOR THE EXTRACTION, and the reason `importable_roots` has no default.

    The same untracked module is invisible under one layout and a refusal under another. If this
    argument were ignored -- or silently defaulted -- a repository that keeps its package outside
    `src/` would get a clean report from a check that looked nowhere, which is the failure this
    package refuses to ship.
    """
    (repo / 'lib').mkdir()
    (repo / 'lib' / 'newmod.py').write_text('VALUE = 1\n', encoding='utf-8')

    assert untracked_modules(repo, importable_roots=('src/',)) == {}
    assert untracked_modules(repo, importable_roots=('lib/',)) == {'newmod': 'lib/newmod.py'}


def test_main_refuses_and_names_the_files(repo: Path, capsys) -> None:
    (repo / 'src' / 'pkg' / 'newmod.py').write_text('VALUE = 1\n', encoding='utf-8')
    (repo / 'src' / 'pkg' / 'user.py').write_text('from pkg.newmod import VALUE\n', encoding='utf-8')
    _git(repo, 'add', 'src/pkg/user.py')

    assert main(['--repo', str(repo)], importable_roots=ROOTS) == 1
    out = capsys.readouterr().out
    assert 'REFUSED' in out and 'src/pkg/newmod.py' in out and 'git add' in out
