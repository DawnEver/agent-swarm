"""A licence notice is a legal declaration, so an UNKNOWN must read as an alarm and never as MIT.

MOVED HERE WITH THE CODE, 2026-08-12. What stayed behind is the consumer's own legal content -- the
copyright line, its curated overrides, its in-tree package names and the prose around the tables.
What moved is the rendering: raw metadata strings to SPDX, families, deterministic tables, and the
repository URL behind a direct git dependency.

WHY THE GUESSING IS THE RISKY PART. Upstreams ship the licence field in every spelling there is, and
the failure direction is asymmetric: a string this cannot recognise must become UNKNOWN, which a
consumer treats as a stop, rather than being coerced into the nearest plausible identifier. So the
tests below spend most of their assertions on what must NOT be recognised.

WHAT THESE CANNOT SEE: `rust_crates` shells out to a crate metadata tool and is not exercised here;
and `python_packages` / `git_sources` read THIS interpreter's environment, so they are asserted on
properties that hold for any environment, never on a particular installed set.
"""

from __future__ import annotations

import importlib.metadata as imd

from agent_swarm.notices import (
    FAMILIES,
    UNKNOWN,
    family,
    git_sources,
    grouped,
    normalize_license,
    python_packages,
    table,
)


def _row(name: str, license_: str, version: str = '1.0') -> dict:
    return {'name': name, 'version': version, 'license': license_, 'url': 'https://example/x'}


class TestNormalizeLicense:
    def test_an_override_wins_over_everything(self) -> None:
        """An override exists because one upstream's metadata was too broken to trust, so it must
        beat the raw string rather than merely filling in for a missing one.
        """
        assert normalize_license('pkg', 'Apache Software License', overrides={'pkg': 'MIT'}) == 'MIT'

    def test_the_overrides_really_decide_the_answer(self) -> None:
        """THE DISCRIMINATING TEST FOR THE EXTRACTION on the curation side, and why `overrides` has
        no default. ONE package with ONE raw string, TWO override maps, TWO answers. A normalizer
        that had kept the original project's curated table baked in would answer identically to
        both -- and a second consumer would be shipping somebody else's legal judgement.
        """
        raw = 'see the LICENCE file'
        assert normalize_license('pkg', raw, overrides={'pkg': 'MIT'}) == 'MIT'
        assert normalize_license('pkg', raw, overrides={'pkg': 'BSD-3-Clause'}) == 'BSD-3-Clause'
        assert normalize_license('pkg', raw, overrides={}) == UNKNOWN

    def test_a_full_licence_TEXT_is_UNKNOWN_and_not_a_guess(self) -> None:
        """Some distributions paste the whole licence into the field. The first identifier-looking
        word in it is not the answer, and a notice that reported one would be a legal claim invented
        by a regex.
        """
        assert normalize_license('pkg', 'Copyright (c) 2020 Somebody\nAll rights reserved.', overrides={}) == UNKNOWN
        assert normalize_license('pkg', 'Permission is hereby granted, free of charge...', overrides={}) == UNKNOWN
        assert normalize_license('pkg', '', overrides={}) == UNKNOWN

    def test_a_classifier_string_is_read(self) -> None:
        """Many packages leave the licence field empty and declare only a classifier; the field
        alone produced UNKNOWNs on packages whose licence was plainly stated.
        """
        assert normalize_license('pkg', 'License :: OSI Approved :: MIT License', overrides={}) == 'MIT'
        assert (
            normalize_license('pkg', 'License :: OSI Approved :: Apache Software License', overrides={}) == 'Apache-2.0'
        )

    def test_the_slash_form_of_a_dual_licence_becomes_an_SPDX_expression(self) -> None:
        """Rust crates write the dual as a slash; the SPDX spelling is `OR`, and a table mixing the
        two spellings cannot be grouped or compared.
        """
        assert normalize_license('pkg', 'MIT/Apache-2.0', overrides={}) == 'MIT OR Apache-2.0'

    def test_an_spdx_expression_passes_through_with_its_parens_tolerated(self) -> None:
        assert normalize_license('pkg', 'MIT OR Apache-2.0', overrides={}) == 'MIT OR Apache-2.0'
        assert normalize_license('pkg', '(MIT OR Apache-2.0) AND Unicode-3.0', overrides={}) == (
            '(MIT OR Apache-2.0) AND Unicode-3.0'
        )

    def test_whitespace_and_case_do_not_change_the_answer(self) -> None:
        assert normalize_license('pkg', '  MIT   License \n', overrides={}) == 'MIT'

    def test_an_unrecognised_string_is_UNKNOWN(self) -> None:
        """The control for every recognition above: a normalizer that returned something plausible
        for anything would satisfy them all and be worthless.
        """
        assert normalize_license('pkg', 'Freeware, ask us nicely', overrides={}) == UNKNOWN


class TestFamiliesAndRendering:
    def test_every_family_is_reachable_and_LGPL_lands_with_GPL(self) -> None:
        assert family('Apache-2.0') == 'Apache 2.0'
        assert family('BSD-3-Clause') == 'BSD'
        assert family('GPL-2.0-or-later') == 'GPL / LGPL'
        assert family('LGPL-3.0-or-later') == 'GPL / LGPL', 'LGPL starts with L, so plain prefix order gets it wrong'
        assert family('MIT') == 'MIT'
        assert family('MPL-2.0') == 'MPL'
        assert family('PSF-2.0') == 'PSF'
        assert family(UNKNOWN) == 'Other'

    def test_an_expression_belongs_to_the_family_of_its_FIRST_term(self) -> None:
        assert family('MIT OR Apache-2.0') == 'MIT'
        assert family('(MIT OR Apache-2.0) AND Unicode-3.0') == 'MIT'

    def test_a_table_is_sorted_case_insensitively_by_name_then_version(self) -> None:
        rendered = table([_row('Zeta', 'MIT'), _row('alpha', 'MIT', '2.0'), _row('alpha', 'MIT', '1.0')])
        names = [line.split('|')[1].strip() for line in rendered.splitlines()[2:]]
        assert names == ['alpha', 'alpha', 'Zeta']
        versions = [line.split('|')[2].strip() for line in rendered.splitlines()[2:4]]
        assert versions == ['1.0', '2.0']

    def test_a_table_is_a_fixed_point_under_input_order(self) -> None:
        """Determinism is what makes a byte-for-byte `--check` possible; without it the notice would
        differ on every regeneration and the drift check would have to be abandoned.
        """
        rows = [_row('b', 'MIT'), _row('a', 'Apache-2.0'), _row('c', UNKNOWN)]
        assert table(rows) == table(list(reversed(rows)))

    def test_grouped_follows_the_declared_family_order_and_omits_empty_families(self) -> None:
        rows = [_row('u', UNKNOWN), _row('m', 'MIT'), _row('a', 'Apache-2.0')]
        rendered = grouped(rows)
        positions = [rendered.index(name) for name in ('Apache 2.0', 'MIT', 'other licences')]
        assert positions == sorted(positions), 'sections must follow FAMILIES order, not input order'
        assert 'BSD' not in rendered and 'MPL' not in rendered, 'an empty family renders as a heading over nothing'
        assert FAMILIES.index('Apache 2.0') < FAMILIES.index('MIT') < FAMILIES.index('Other')

    def test_grouped_is_a_fixed_point_under_input_order(self) -> None:
        rows = [_row('b', 'MIT'), _row('a', 'Apache-2.0'), _row('c', 'BSD-3-Clause')]
        assert grouped(rows) == grouped(list(reversed(rows)))

    def test_every_row_survives_grouping(self) -> None:
        """A row whose licence matched no family must still appear. Dropping it would remove a
        dependency from a legal notice, which is the one failure this document cannot have.
        """
        rows = [_row('m', 'MIT'), _row('weird', 'Freeware-1.0'), _row('u', UNKNOWN)]
        rendered = grouped(rows)
        assert all(row['name'] in rendered for row in rows)


class TestGitSources:
    def test_a_fragment_a_query_and_a_pinned_sha_are_all_stripped(self) -> None:
        """They are provenance, not the repository URL. A link carrying a pin sends the reader to
        one commit of a repository whose licence they are trying to look up.
        """
        declared = [
            'frag-pkg @ git+https://h/o/frag.git#egg=frag-pkg',
            'query-pkg @ git+https://h/o/query.git?rev=main',
            'sha-pkg @ git+https://h/o/sha.git@0123456789abcdef0123456789abcdef01234567',
            'short-pkg @ git+https://h/o/short.git@0123abc',
        ]
        found = git_sources(declared)
        assert found['frag-pkg'] == 'https://h/o/frag.git'
        assert found['query-pkg'] == 'https://h/o/query.git'
        assert found['sha-pkg'] == 'https://h/o/sha.git'
        assert found['short-pkg'] == 'https://h/o/short.git'

    def test_a_requirement_that_is_not_a_git_one_contributes_nothing(self) -> None:
        assert 'numpy' not in git_sources(['numpy>=2', 'pytest'])

    def test_the_declared_specs_really_reach_the_answer(self) -> None:
        """Two different declared lists give two different maps -- the environment half cannot
        account for that, so the caller's argument is proven to be read.
        """
        assert git_sources(['one-pkg @ git+https://h/o/one.git']).get('one-pkg') == 'https://h/o/one.git'
        assert git_sources(['two-pkg @ git+https://h/o/two.git']).get('one-pkg') is None


class TestPythonPackages:
    def test_the_in_tree_set_really_excludes(self) -> None:
        """THE DISCRIMINATING TEST FOR THE EXTRACTION on the environment side, and why `in_tree` has
        no default. The same environment, two `in_tree` sets, two answers: without this the module
        could ignore the argument and list a product as a third party in its own notice.
        """
        installed = {dist.metadata['Name'] for dist in imd.distributions()} - {None}
        victim = min(name for name in installed if name)

        with_victim = {row['name'] for row in python_packages(in_tree=frozenset(), overrides={})}
        without = {row['name'] for row in python_packages(in_tree=frozenset({victim}), overrides={})}

        assert victim in with_victim
        assert victim not in without
        assert with_victim - without == {victim}, 'excluding one name must not disturb any other row'

    def test_every_row_has_the_four_fields_a_table_needs(self) -> None:
        rows = python_packages(in_tree=frozenset(), overrides={})
        assert rows, 'an empty environment would make every assertion here vacuous'
        assert all(set(row) == {'name', 'version', 'license', 'url'} for row in rows)
        assert all(row['url'] for row in rows), 'a blank URL cell must be the placeholder, never empty'

    def test_the_overrides_reach_the_installed_rows_too(self) -> None:
        installed = {dist.metadata['Name'] for dist in imd.distributions()} - {None}
        victim = min(name for name in installed if name)
        rows = python_packages(in_tree=frozenset(), overrides={victim: 'Zzz-Test-1.0'})
        assert next(row for row in rows if row['name'] == victim)['license'] == 'Zzz-Test-1.0'
