"""Evidence is a RECORD with required fields, not a run that printed something reassuring.

The forbidden path is `attempt -> "tests passed" -> merge`. Every assertion here plants the shape
that path needs -- a missing field, a zero-collected run, an undeclared file -- and calls the real
constructor rather than re-deriving what it ought to reject.
"""

from __future__ import annotations

import pytest

from agent_swarm import evidence


_DIGEST = 'a' * 40
_ENVKEY = '0123456789abcdef'


def _counts(**over: int) -> evidence.RunCounts:
    base = {'passed': 12, 'failed': 0, 'errors': 0, 'skipped': 1, 'xfailed': 0, 'xpassed': 0}
    return evidence.RunCounts(**{**base, **over})


def _evidence(**over: object) -> evidence.Evidence:
    base: dict[str, object] = {
        'tree': _DIGEST,
        'environment': _ENVKEY,
        'counts': _counts(),
        'effects': evidence.Effects(declared=('src/a.py',), observed=('src/a.py',)),
        'artifacts': (),
    }
    return evidence.Evidence(**{**base, **over})  # type: ignore[arg-type]


class TestAMissingFieldIsARefusal:
    def test_every_missing_field_is_named_at_once(self):
        """One at a time would make a caller iterate, learning the requirement by attrition."""
        with pytest.raises(evidence.IncompleteEvidence) as caught:
            evidence.Evidence.from_mapping({'tree': _DIGEST})
        message = str(caught.value)
        for field in ('environment', 'counts', 'effects', 'artifacts'):
            assert field in message, f'{field} is required and the refusal does not name it'

    @pytest.mark.parametrize('field', ['tree', 'environment'])
    def test_an_empty_identity_is_not_a_value(self, field: str):
        with pytest.raises(evidence.IncompleteEvidence):
            _evidence(**{field: ''})

    def test_a_negative_count_is_refused(self):
        with pytest.raises(evidence.IncompleteEvidence):
            _counts(passed=-1)


class TestTheVacuousGreen:
    def test_a_run_that_collected_nothing_does_not_support_a_pass(self):
        """Zero failures and zero tests is the cheapest green there is, and it is not evidence."""
        empty = _evidence(counts=_counts(passed=0, skipped=0))
        assert empty.counts.total == 0
        assert not empty.supports_pass
        assert 'collected no test' in empty.refusal_reason

    def test_a_failure_does_not_support_a_pass(self):
        assert not _evidence(counts=_counts(failed=1)).supports_pass

    def test_an_error_does_not_support_a_pass_either(self):
        """errors is a separate column and a report that only reads `failed` calls a broken run
        clean -- the exact shape this record exists to make impossible."""
        assert not _evidence(counts=_counts(errors=1)).supports_pass

    def test_a_real_run_does(self):
        assert _evidence().supports_pass
        assert _evidence().refusal_reason == ''


class TestObservedEffects:
    def test_a_deviation_is_RECORDED_and_not_refused(self):
        """A submission whose observed effects exceed its declared intent is ACCEPTED; scope is
        intent and routing, never a lock. So the deviation must be readable, and only that."""
        effects = evidence.Effects(declared=('src/a.py',), observed=('src/a.py', 'src/b.py'))
        record = _evidence(effects=effects)
        assert record.effects.undeclared == ('src/b.py',)
        assert record.effects.deviates
        assert record.supports_pass, 'a deviation must not be a refusal'

    def test_a_declared_file_that_never_changed_is_visible_too(self):
        effects = evidence.Effects(declared=('src/a.py', 'src/b.py'), observed=('src/a.py',))
        assert effects.unrealised == ('src/b.py',)
        assert effects.deviates


class TestItSerialisesWithoutLosingAField:
    def test_round_trip(self):
        record = _evidence(
            effects=evidence.Effects(declared=('src/a.py',), observed=('src/a.py', 'src/b.py')),
            artifacts=(evidence.Artifact(name='gate.log', sha256='b' * 64, size_bytes=1024),),
        )
        assert evidence.Evidence.from_mapping(record.to_mapping()) == record

    def test_the_digest_moves_when_any_field_does(self):
        """Pinned as a RATIO between two operating points -- that the digests DIFFER -- rather than
        as one hex value, which would be a reference nothing could ever contradict."""
        base = _evidence()
        assert base.digest() == _evidence().digest()
        assert base.digest() != _evidence(tree='c' * 40).digest()
        assert base.digest() != _evidence(counts=_counts(passed=13)).digest()
        assert base.digest() != _evidence(environment='fedcba9876543210').digest()

    def test_an_artifact_digest_must_look_like_one(self):
        with pytest.raises(evidence.IncompleteEvidence):
            evidence.Artifact(name='gate.log', sha256='not-a-digest', size_bytes=1)


def test_nothing_here_turns_TEXT_into_evidence():
    """The control. `parse`/`from_log` returning an Evidence would reinstate exactly the inference
    -- "the log looks like success, therefore success" -- that the record replaces."""
    text_doors = [name for name in dir(evidence) if name in {'parse', 'from_log', 'from_text', 'from_output'}]
    assert not text_doors, f'evidence can be inferred from prose again via {text_doors}'
