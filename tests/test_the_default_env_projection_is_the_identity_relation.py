"""One manifest, several questions -- and the DEFAULT answer must be today's answer, exactly.

Trust is the identity relation now (`trust_key == envkey`), it is the strictest possible policy and
it is already correct. A projection layer that widened it by arriving would be a declaration that
lies in the purest form: nothing would fail, and every verdict would silently become adoptable on a
box that could change its outcome.
"""

from __future__ import annotations

import pytest

from agent_swarm import environment, manifest

pytestmark = pytest.mark.unit

_LINES = (
    'cpython-3.12.4-win32',
    'agent-swarm==0.1.0',
    'numpy==2.1.0',
    'pytest==8.3.2',
    'config/local.toml=0123456789abcdef',
)


def _manifest(*extra: str, lines: tuple[str, ...] = _LINES) -> manifest.EnvManifest:
    return manifest.EnvManifest.from_lines((*lines, *extra))


class TestTheIdentityProjectionIsTheDefault:
    def test_the_default_key_IS_the_envkey_this_repo_already_computes(self):
        """Not "equivalent to", not "derived from" -- the same 16 hex characters, because the
        projection selects every line and hands them to the existing computation unchanged."""
        record = _manifest()
        assert record.key() == environment.compute_envkey(_LINES)

    def test_the_default_is_trust_and_it_is_the_identity(self):
        assert manifest.DEFAULT_PROJECTION is manifest.TRUST
        assert manifest.TRUST.is_identity
        assert manifest.TRUST.select(_manifest()) == list(_LINES)

    def test_naming_it_explicitly_changes_nothing(self):
        record = _manifest()
        assert record.key(manifest.TRUST) == record.key()
        assert record.projected().projection == 'trust/v1'

    @pytest.mark.parametrize(
        'extra',
        [
            'numpy==2.2.0',  # a version this project depends on
            'jupyter==1.0.0',  # a package it does not
            'docs/index.md=aaaaaaaaaaaaaaaa',  # a machine-local test input
        ],
    )
    def test_ANY_difference_moves_the_trust_key(self, extra: str):
        """The strictness, planted rather than re-derived. Widening is fleet WIDTH, not
        correctness, so anything that widens it here must red."""
        assert _manifest(extra).key() != _manifest().key()


class TestPlacementIsNarrowerAndSaysExactlyHowMuch:
    def test_a_version_bump_does_not_move_the_placement_key(self):
        """ "May this box run the job at all" does not turn on a patch release. A key wide enough
        for trust is too wide for this: it moves on an unrelated upgrade and every placement
        decision is recomputed for nothing."""
        upgraded = _manifest(lines=tuple(line.replace('numpy==2.1.0', 'numpy==2.9.0') for line in _LINES))
        assert upgraded.key(manifest.PLACEMENT) == _manifest().key(manifest.PLACEMENT)

    def test_a_missing_package_DOES(self):
        without = _manifest(lines=tuple(line for line in _LINES if not line.startswith('numpy')))
        assert without.key(manifest.PLACEMENT) != _manifest().key(manifest.PLACEMENT)

    def test_a_different_interpreter_DOES(self):
        other = _manifest(lines=('cpython-3.13.0-linux', *_LINES[1:]))
        assert other.key(manifest.PLACEMENT) != _manifest().key(manifest.PLACEMENT)

    def test_a_machine_local_test_input_does_not(self):
        """SCOPE, STATED SO NO READER SUPPLIES "everything": placement observes the interpreter and
        WHICH distributions are present. It does not observe versions and it does not observe the
        files tests read -- those decide trust, and this key must never be used for trust."""
        edited = _manifest(lines=tuple(line.replace('0123456789abcdef', 'ffffffffffffffff') for line in _LINES))
        assert edited.key(manifest.PLACEMENT) == _manifest().key(manifest.PLACEMENT)
        assert edited.key() != _manifest().key(), 'the same change must still move trust'

    def test_placement_is_not_the_identity_and_says_so(self):
        assert not manifest.PLACEMENT.is_identity
        assert manifest.PLACEMENT.key(_manifest()) != _manifest().key()


class TestTheRegistry:
    def test_every_projection_is_named_and_versioned(self):
        for spelling, projection in manifest.PROJECTIONS.items():
            assert spelling == f'{projection.name}/v{projection.version}'
            assert projection.question, f'{spelling} does not say which question it answers'

    def test_an_unknown_projection_is_a_refusal_that_lists_the_known_ones(self):
        with pytest.raises(KeyError) as caught:
            manifest.projection('trust/v99')
        assert 'trust/v1' in str(caught.value)

    def test_a_projected_key_carries_which_relation_produced_it(self):
        """A bare 16-hex string cannot say which equivalence it belongs to, and two projections'
        keys stored in one place would compare equal by accident."""
        projected = _manifest().projected(manifest.PLACEMENT)
        assert projected.projection == 'placement/v1'
        assert projected.key == manifest.PLACEMENT.key(_manifest())
        assert projected != _manifest().projected(manifest.TRUST)


class TestTheFullRecord:
    def test_a_manifest_without_an_interpreter_line_is_refused(self):
        """No projection here can answer without it, so the refusal belongs at construction rather
        than in each one."""
        with pytest.raises(manifest.MalformedManifest):
            manifest.EnvManifest.from_lines(('numpy==2.1.0',))

    def test_it_keeps_the_lines_verbatim_so_a_difference_can_be_EXPLAINED(self):
        record = _manifest()
        assert record.lines == _LINES
        assert record.distributions['numpy'] == '2.1.0'
        assert record.files['config/local.toml'] == '0123456789abcdef'
        assert record.interpreter == 'cpython-3.12.4-win32'

    def test_diagnostics_grades_a_difference_rather_than_hashing_it(self):
        """Why two boxes differ is not an equivalence relation -- it needs BOTH operands and a
        dependency closure -- so it is a function of two manifests, never a third key."""
        theirs = _manifest()
        mine = _manifest(lines=tuple(line.replace('numpy==2.1.0', 'numpy==2.9.0') for line in _LINES))
        changes = manifest.explain(theirs, mine, closure=frozenset({'agent-swarm', 'numpy'}))
        assert [(c.item, c.theirs, c.mine) for c in changes] == [('numpy', '2.1.0', '2.9.0')]
        assert changes[0].blocks_reuse

    def test_a_difference_outside_the_closure_does_not_block_reuse(self):
        theirs = _manifest()
        mine = _manifest('jupyter==1.0.0')
        changes = manifest.explain(theirs, mine, closure=frozenset({'agent-swarm', 'numpy'}))
        assert not any(c.blocks_reuse for c in changes)
