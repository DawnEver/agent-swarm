"""Which tests cover a diff -- the tiers, and the parameters that keep them project-neutral.

Every fixture here is a SYNTHETIC tree under `tmp_path`, which is the only way to assert a tier's
NEGATIVE half: that it does not select something. Against a real repository "this file was not
selected" is unfalsifiable noise.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_swarm import selection


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """A small repo: `src/pkg/` mirrored into `tests/unit/`, plus a non-package `scripts/`."""
    for rel in (
        'src/pkg/solver.py',
        'src/pkg/sub/widget.py',
        'scripts/runner.py',
        'tests/unit/test_solver.py',
        'tests/unit/test_solver_extra.py',
        'tests/unit/sub/test_widget.py',
        'tests/unit/sub/test_vendor_widget.py',
        'tests/unit/test_elsewhere_widget.py',
        'tests/architecture/test_scans_everything.py',
    ):
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('', encoding='utf-8')
    return tmp_path


class TestByConvention:
    def test_the_mirrored_path_comes_first(self, tree):
        found = selection.by_convention(
            Path('src/pkg/sub/widget.py'), tests_root=tree / 'tests', package_root=('src', 'pkg')
        )
        assert found[0] == tree / 'tests' / 'unit' / 'sub' / 'test_widget.py'

    def test_the_underscore_suffix_spelling_is_found_too(self, tree):
        """An exact-stem convention is the one a repo MOSTLY follows and not the one it always
        follows; a suggester that silently drops the real covering test makes "nothing covers this"
        indistinguishable from "I only looked for one spelling".
        """
        found = selection.by_convention(
            Path('src/pkg/solver.py'), tests_root=tree / 'tests', package_root=('src', 'pkg')
        )
        assert tree / 'tests' / 'unit' / 'test_solver_extra.py' in found

    def test_the_PREFIX_spelling_is_scoped_to_the_mirrored_directory(self, tree):
        """`test_*_<stem>.py` unscoped would match every `test_<vendor>_widget.py` in the tree. A
        test in the MIRROR of a source file's own package is about that package by construction; one
        anywhere else is not.
        """
        found = selection.by_convention(
            Path('src/pkg/sub/widget.py'), tests_root=tree / 'tests', package_root=('src', 'pkg')
        )
        assert tree / 'tests' / 'unit' / 'sub' / 'test_vendor_widget.py' in found
        assert tree / 'tests' / 'unit' / 'test_elsewhere_widget.py' not in found

    def test_a_file_outside_the_package_root_gets_no_mirror(self, tree):
        """THE PARAMETER'S POINT. `package_root` decides what mirrors; a path outside it has no
        mirrored location, and inventing one would name a file that does not exist.
        """
        found = selection.by_convention(
            Path('scripts/runner.py'), tests_root=tree / 'tests', package_root=('src', 'pkg')
        )
        assert all('unit/sub' not in p.as_posix() for p in found)

    def test_dunder_init_and_non_python_select_nothing(self, tree):
        args = {'tests_root': tree / 'tests', 'package_root': ('src', 'pkg')}
        assert selection.by_convention(Path('src/pkg/__init__.py'), **args) == []
        assert selection.by_convention(Path('README.md'), **args) == []


class TestByMention:
    def test_a_test_naming_the_path_is_found(self, tmp_path):
        (tmp_path / 'tests').mkdir()
        (tmp_path / 'tests' / 'test_a.py').write_text("load('src/pkg/thing.py')\n", encoding='utf-8')
        (tmp_path / 'tests' / 'test_b.py').write_text('nothing relevant\n', encoding='utf-8')
        found = selection.by_mention(Path('src/pkg/thing.py'), tests_root=tmp_path / 'tests')
        assert [p.name for p in found] == ['test_a.py']

    def test_the_BASENAME_needle_only_fires_under_a_declared_prefix(self, tmp_path):
        """MEASURED: a directory that is not an importable package can only be tested by loading its
        files BY PATH, so the literal `scripts/x.py` never appears and the full-path needle found 4
        of 20 such tests. The basename needle is what lets the tier see its own population -- and it
        is scoped, because a bare basename match everywhere would be far too wide.
        """
        (tmp_path / 'tests').mkdir()
        (tmp_path / 'tests' / 'test_loads.py').write_text(
            "spec_from_file_location('r', 'runner.py')\n", encoding='utf-8'
        )

        without = selection.by_mention(Path('scripts/runner.py'), tests_root=tmp_path / 'tests')
        assert without == [], 'the full path never appears in the by-path idiom'

        with_prefix = selection.by_mention(
            Path('scripts/runner.py'), tests_root=tmp_path / 'tests', basename_prefixes=('scripts/',)
        )
        assert [p.name for p in with_prefix] == ['test_loads.py']


class TestByScan:
    """The blind spot no per-file tier can see: a test that scans a directory names no file in it."""

    def test_a_declared_row_selects_every_test_it_names(self, tmp_path):
        declared = {'cases/': ('tests/test_one.py', 'tests/test_two.py')}
        found = selection.by_scan(
            Path('cases/a/manifest.toml'), declared=declared, patterns={}, tests_root=tmp_path, root=tmp_path
        )
        assert [p.as_posix() for p in found] == ['tests/test_one.py', 'tests/test_two.py']

    def test_a_path_outside_the_prefix_selects_none_of_them(self, tmp_path):
        declared = {'cases/': ('tests/test_one.py',)}
        assert (
            selection.by_scan(Path('src/pkg/x.py'), declared=declared, patterns={}, tests_root=tmp_path, root=tmp_path)
            == []
        )

    def test_a_DERIVED_row_finds_scanners_the_declared_map_never_listed(self, tmp_path):
        """THE REASON DERIVATION EXISTS. Widening the declared map from one test per prefix to a
        tuple fixed its TYPE and left the population hand-written -- so it could still be incomplete,
        and MEASURED it was: 21 files scanned one glob and the row named 2. A derived row cannot fall
        behind, because there is no list to update.
        """
        tests = tmp_path / 'tests'
        tests.mkdir()
        (tests / 'test_scanner.py').write_text("for m in root.glob('*/manifest.toml'):\n    pass\n", encoding='utf-8')
        (tests / 'test_unrelated.py').write_text('x = 1\n', encoding='utf-8')

        found = selection.by_scan(
            Path('cases/a/manifest.toml'),
            declared={},
            patterns={'cases/': r'\*/manifest\.toml'},
            tests_root=tests,
            root=tmp_path,
        )
        assert [p.as_posix() for p in found] == ['tests/test_scanner.py']

    def test_a_test_reachable_BOTH_ways_appears_once(self, tmp_path):
        """A duplicate would put the same path on the command line twice."""
        tests = tmp_path / 'tests'
        tests.mkdir()
        (tests / 'test_scanner.py').write_text("root.glob('*/manifest.toml')\n", encoding='utf-8')
        found = selection.by_scan(
            Path('cases/a/manifest.toml'),
            declared={'cases/': ('tests/test_scanner.py',)},
            patterns={'cases/': r'\*/manifest\.toml'},
            tests_root=tests,
            root=tmp_path,
        )
        assert [p.as_posix() for p in found] == ['tests/test_scanner.py']


class TestCovering:
    def test_a_path_nothing_covers_is_REPORTED_not_dropped(self):
        """ "The targeted run was green" has to be distinguishable from "there was nothing to run",
        and those look identical if the uncovered paths are swallowed.
        """
        tests, uncovered = selection.covering([Path('src/pkg/x.py')], tiers=[], mention=lambda _p: [])
        assert tests == []
        assert uncovered == [Path('src/pkg/x.py')]

    def test_a_changed_test_measures_itself(self):
        tests, uncovered = selection.covering([Path('tests/unit/test_x.py')], tiers=[], mention=lambda _p: [])
        assert tests == [Path('tests/unit/test_x.py')]
        assert uncovered == []

    def test_a_changed_CONFTEST_selects_its_subtree_not_itself(self):
        """A conftest is not a test and collects ZERO. Selecting it produced a run that passed while
        measuring nothing -- a vacuous green, from the tool built to avoid one.
        """
        tests, _uncovered = selection.covering([Path('tests/unit/conftest.py')], tiers=[], mention=lambda _p: [])
        assert tests == [Path('tests/unit')]

    def test_mention_is_a_FALLBACK_when_a_precise_tier_hit(self, tmp_path):
        precise = tmp_path / 'tests' / 'precise.py'
        mentioned = tmp_path / 'tests' / 'mentioned.py'
        for path in (precise, mentioned):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('', encoding='utf-8')
        tests, _u = selection.covering(
            [Path('src/pkg/x.py')], tiers=[lambda _p: [precise]], mention=lambda _p: [mentioned]
        )
        assert tests == [precise], 'a precise hit suppresses the text-matching fallback'

    def test_mention_is_ADDITIVE_under_a_declared_prefix(self, tmp_path):
        """THE MEASURED DEFECT. A single coarse declared row covering a whole directory fired for
        every file under it and HID the tests that actually load them -- 1 test selected where 20
        exercise the file, and the 1 did not import it. A coarse row that hides a precise tier
        cannot be fixed by adding rows; a richer row would shadow just as hard.
        """
        coarse = tmp_path / 'tests' / 'coarse.py'
        loader = tmp_path / 'tests' / 'loader.py'
        for path in (coarse, loader):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('', encoding='utf-8')
        tests, _u = selection.covering(
            [Path('scripts/runner.py')],
            tiers=[lambda _p: [coarse]],
            mention=lambda _p: [loader],
            additive_prefixes=('scripts/',),
        )
        assert sorted(p.name for p in tests) == ['coarse.py', 'loader.py']


class TestSplitByCost:
    def test_a_declared_slow_prefix_is_withheld(self):
        fast, slow = selection.split_by_cost(
            [Path('tests/unit/test_a.py'), Path('tests/integration/test_b.py')],
            slow_prefixes=('tests/integration/',),
        )
        assert [p.name for p in fast] == ['test_a.py']
        assert [p.name for p in slow] == ['test_b.py']

    def test_a_test_at_the_ROOT_of_the_tests_tree_counts_as_slow(self):
        fast, slow = selection.split_by_cost([Path('tests/test_library.py')], slow_prefixes=())
        assert fast == []
        assert [p.name for p in slow] == ['test_library.py']

    def test_a_tier_DIRECTORY_is_judged_by_its_prefix_alone(self, tmp_path):
        """`tests/architecture` matches the shape of the root-level-file heuristic -- one `/` -- while
        being neither a file nor slow. Judging it by depth would withhold a whole cheap tier.
        """
        tier = tmp_path / 'tests' / 'architecture'
        tier.mkdir(parents=True)
        fast, slow = selection.split_by_cost([tier], slow_prefixes=('tests/integration/',))
        assert slow == []
        assert fast == [tier]


class TestAuditDeclared:
    def test_a_row_naming_a_missing_file_is_reported(self, tmp_path):
        problems = selection.audit_declared({'cases/': ('tests/gone.py',)}, tmp_path)
        assert problems and 'no such test file' in problems[0]

    def test_a_row_whose_test_no_longer_mentions_the_prefix_is_reported(self, tmp_path):
        """The direction that matters: a row claiming coverage that stopped existing."""
        (tmp_path / 'tests').mkdir()
        (tmp_path / 'tests' / 'x.py').write_text('nothing about that directory\n', encoding='utf-8')
        problems = selection.audit_declared({'cases/': ('tests/x.py',)}, tmp_path)
        assert problems and 'never mentions' in problems[0]

    def test_a_TRUE_row_is_not_reported(self, tmp_path):
        """The control. An audit that flagged everything would be as useless as one that flagged
        nothing, and only this direction proves it discriminates.
        """
        (tmp_path / 'tests').mkdir()
        (tmp_path / 'tests' / 'x.py').write_text("glob('cases/*/manifest.toml')\n", encoding='utf-8')
        assert selection.audit_declared({'cases/': ('tests/x.py',)}, tmp_path) == []

    def test_a_DIRECTORY_row_is_held_to_the_same_standard_one_level_up(self, tmp_path):
        """A directory row exists so a tier cannot outgrow a hand-written list. At least one test
        under it must really mention the prefix, or the row claims coverage that is not there.
        """
        tier = tmp_path / 'tests' / 'architecture'
        tier.mkdir(parents=True)
        assert 'holds no test files' in selection.audit_declared({'cases/': ('tests/architecture',)}, tmp_path)[0]

        (tier / 'test_a.py').write_text('unrelated\n', encoding='utf-8')
        assert 'no test under it mentions' in selection.audit_declared({'cases/': ('tests/architecture',)}, tmp_path)[0]

        (tier / 'test_b.py').write_text("scan('cases/')\n", encoding='utf-8')
        assert selection.audit_declared({'cases/': ('tests/architecture',)}, tmp_path) == []


def test_the_defer_code_is_neither_a_pass_nor_a_failure():
    """A selector has THREE outcomes. Rendering "I am the wrong instrument" as a failure is the same
    defect as rendering a failure as a pass: the caller cannot tell what happened.
    """
    assert selection.DEFER_RC not in (0, 1)
