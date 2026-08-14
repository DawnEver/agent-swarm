"""A verdict is a payload signed by the key of the role that produced it, and a reader verifies
BEFORE the verdict counts.

THE SECURITY MODEL, asserted rather than assumed. Trust today is a git-ref write permission held by
a role's credential, so a leaked credential is a forged truth and a compromised forge can paint any
tree green. §3.1 turns the forge into an untrusted index + transport: the signing key lives only on
the reader+producer side, never on the forge, so neither a leaked credential nor a compromised forge
can manufacture a tag the reader will accept. A leak degrades from "can forge truth" to "can drop
garbage"; an unsigned or badly-signed verdict is detectable noise, never a verdict.

THE PRIMITIVE IS HMAC-SHA256, and deliberately so: agent-swarm is `dependencies=[]` and has no
public-key primitives, and a hand-written asymmetric scheme is forbidden. Symmetric HMAC is enough
because reader and producer SHARE the role key -- the trust boundary §3.1 draws -- so the forge,
which never holds the key, cannot compute a valid tag no matter what it sees.
"""

from __future__ import annotations

import inspect

import pytest

from agent_swarm import evidence, roles, signing


def _counts(**over: int) -> evidence.RunCounts:
    base = {'passed': 12, 'failed': 0, 'errors': 0, 'skipped': 1, 'xfailed': 0, 'xpassed': 0}
    return evidence.RunCounts(**{**base, **over})


def _evidence(**over: object) -> evidence.Evidence:
    base: dict[str, object] = {
        'tree': 'a' * 40,
        'environment': '0123456789abcdef',
        'counts': _counts(),
        'effects': evidence.Effects(declared=('src/a.py',), observed=('src/a.py',)),
        'artifacts': (),
        'signer': 'verifier',
    }
    return evidence.Evidence(**{**base, **over})  # type: ignore[arg-type]


class TestTheHMACPrimitive:
    def test_sign_then_verify_round_trips(self):
        tag = signing.sign(b'role-secret', b'the payload')
        assert signing.verify(b'role-secret', b'the payload', tag)

    def test_a_tampered_payload_fails(self):
        tag = signing.sign(b'k', b'payload')
        assert not signing.verify(b'k', b'payload!', tag)

    def test_a_tampered_tag_fails(self):
        tag = signing.sign(b'k', b'payload')
        flipped = ('0' if tag[0] != '0' else '1') + tag[1:]
        assert flipped != tag
        assert not signing.verify(b'k', b'payload', flipped)

    def test_a_different_key_fails(self):
        tag = signing.sign(b'k1', b'payload')
        assert not signing.verify(b'k2', b'payload', tag)

    def test_a_malformed_tag_is_FALSE_not_an_error(self):
        """The must-verify read path sees noise as noise -- a bad tag shape must not raise, or the
        reader would have to handle errors where it is only allowed to say 'not a verdict'."""
        tag = signing.sign(b'k', b'payload')
        assert not signing.verify(b'k', b'payload', 'not-hex-at-all')
        assert not signing.verify(b'k', b'payload', 12345)  # type: ignore[arg-type]
        assert not signing.verify(b'k', b'payload', tag[:-1])

    def test_verify_compares_in_CONSTANT_TIME(self):
        """The comparison never short-circuits on a matching prefix. Confirmed by inspection of the
        one primitive that decides accept/reject: it must route through `hmac.compare_digest`."""
        source = inspect.getsource(signing.verify)
        assert 'hmac.compare_digest' in source


class TestPerRoleKeys:
    def test_key_for_role_reads_the_roles_key(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv('AGENT_SWARM_SIGNING_KEY_VERIFIER', 'verifier-secret')
        assert signing.key_for_role('verifier') == b'verifier-secret'

    def test_a_role_without_a_supplied_key_is_refused(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv('AGENT_SWARM_SIGNING_KEY_AGENT', raising=False)
        with pytest.raises(signing.SigningKeyUnavailable):
            signing.key_for_role('agent')

    def test_an_unknown_role_is_refused(self):
        with pytest.raises(ValueError):
            signing.key_for_role('no-such-role')

    def test_each_role_gets_its_own_distinct_key(self, monkeypatch: pytest.MonkeyPatch):
        """The roles are distinct producers; the key scheme must not collapse them into one."""
        for role in roles.ROLE_NAMES:
            monkeypatch.setenv(f'AGENT_SWARM_SIGNING_KEY_{role.upper()}', f'{role}-secret')
        keys = {role: signing.key_for_role(role) for role in roles.ROLE_NAMES}
        assert len({k for k in keys.values()}) == len(roles.ROLE_NAMES)

    def test_a_signature_under_one_roles_key_fails_under_another(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv('AGENT_SWARM_SIGNING_KEY_VERIFIER', 'v')
        monkeypatch.setenv('AGENT_SWARM_SIGNING_KEY_INTEGRATOR', 'i')
        tag = signing.sign(signing.key_for_role('verifier'), b'payload')
        assert not signing.verify(signing.key_for_role('integrator'), b'payload', tag)


class TestEvidenceSignsAndVerifies:
    def test_an_evidence_with_a_signer_signs_and_verifies(self):
        record = _evidence()
        tag = record.sign('verifier-key')
        assert record.verify('verifier-key', tag)

    def test_signing_without_a_signer_is_refused(self):
        with pytest.raises(evidence.IncompleteEvidence):
            _evidence(signer=None).sign('k')

    def test_verifying_without_a_signer_answers_FALSE(self):
        assert not _evidence(signer=None).verify('k', 'anything')

    def test_a_signature_binds_the_records_content(self):
        record = _evidence()
        tag = record.sign('k')
        assert not _evidence(tree='c' * 40).verify('k', tag)
        assert not _evidence(counts=_counts(passed=13)).verify('k', tag)

    def test_a_signature_fails_under_another_signers_key(self):
        record = _evidence()
        tag = record.sign('verifier-key')
        assert not record.verify('integrator-key', tag)

    def test_the_signer_survives_a_serialisation_round_trip(self):
        record = _evidence()
        rebuilt = evidence.Evidence.from_mapping(record.to_mapping())
        assert rebuilt.signer == 'verifier'
        assert rebuilt == record


class TestTheVerdictPayload:
    def test_sign_verdict_produces_a_record_a_reader_verifies(self):
        record = evidence.sign_verdict(
            'verifier-key', testkey='t', kind='gate', envkey='e', verdict='PASS', evidence=_evidence()
        )
        assert evidence.verify_verdict(record, 'verifier-key')

    def test_verify_rejects_a_record_whose_verdict_word_was_changed(self):
        record = evidence.sign_verdict('k', testkey='t', kind='gate', envkey='e', verdict='PASS', evidence=_evidence())
        tampered = evidence.RecordVerdict(
            testkey='t',
            kind='gate',
            envkey='e',
            verdict='FAIL',
            evidence=record.evidence,
            tag=record.tag,
            signer=record.signer,
        )
        assert not evidence.verify_verdict(tampered, 'k')

    def test_verify_rejects_a_record_signed_under_another_roles_key(self):
        record = evidence.sign_verdict(
            'verifier-key', testkey='t', kind='gate', envkey='e', verdict='PASS', evidence=_evidence()
        )
        assert not evidence.verify_verdict(record, 'integrator-key')

    def test_sign_verdict_requires_a_signer_on_the_evidence(self):
        with pytest.raises(evidence.IncompleteEvidence):
            evidence.sign_verdict(
                'k', testkey='t', kind='gate', envkey='e', verdict='PASS', evidence=_evidence(signer=None)
            )

    def test_the_verdict_record_rejects_a_producer_that_is_not_the_signer(self):
        with pytest.raises(evidence.IncompleteEvidence):
            evidence.RecordVerdict(
                testkey='t',
                kind='gate',
                envkey='e',
                verdict='PASS',
                evidence=_evidence(signer='verifier'),
                tag='ab' * 32,
                signer='agent',
            )


class TestTheMustVerifyReadPathTreatsNoiseAsNoise:
    """THE INVARIANT. On the read path a verdict is adopted ONLY after the signature verifies;
    anything else -- unsigned, badly signed, wrong role -- is detectable noise, never a verdict."""

    def test_an_UNSIGNED_verdict_is_rejected(self):
        unsigned = evidence.RecordVerdict(
            testkey='t', kind='gate', envkey='e', verdict='PASS', evidence=_evidence(), tag='', signer='verifier'
        )
        assert not evidence.verify_verdict(unsigned, 'verifier-key')

    def test_a_BADLY_signed_verdict_is_rejected(self):
        signed = evidence.sign_verdict('k', testkey='t', kind='gate', envkey='e', verdict='PASS', evidence=_evidence())
        bad = evidence.RecordVerdict(
            testkey='t',
            kind='gate',
            envkey='e',
            verdict='PASS',
            evidence=signed.evidence,
            tag='0' * 64,
            signer='verifier',
        )
        assert not evidence.verify_verdict(bad, 'k')

    def test_a_valid_verdict_is_adopted(self):
        signed = evidence.sign_verdict('k', testkey='t', kind='gate', envkey='e', verdict='PASS', evidence=_evidence())
        assert evidence.verify_verdict(signed, 'k')
