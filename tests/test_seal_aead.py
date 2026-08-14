r"""The REAL AEAD seal/unseal: AESGCM from `cryptography`, keyed by HKDF of the machine key.

THE DIFFERENCE FROM test_rotation.py. That file drives the ORCHESTRATION against a test-only stand-in
whose job is accept/reject behaviour, not confidentiality. This file exercises the REAL primitive
`seal.real()` -- the audited AESGCM behind the `crypto` extra -- and pins the properties a rotation
credential path needs: a round trip, a wrong-key refusal, a tamper refusal, a truncation refusal,
cross-machine isolation, and a random nonce. It also pins the NO-cryptography behaviour: the module
still imports, and `seal.real()` RAISES a clear reason instead of silently shipping plaintext.

WHY THE REAL TESTS ARE SEPARATE. `test_rotation.py`'s stand-in deliberately carries the plaintext so
confidentiality failures would surface nowhere; pinning confidentiality needs the real ciphertext.
And `seal.real()` only exists when `cryptography` is installed -- which the `crypto`/`dev` extras
guarantee for a full dev checkout, and which a bare Gitea host never is.
"""

from __future__ import annotations

import builtins

import pytest

from agent_swarm import seal


# --------------------------------------------------------------------------- the real primitive exists


def test_real_is_a_rotation_seal_when_cryptography_is_installed():
    impl = seal.real()
    assert isinstance(impl, seal.RotationSeal)


# --------------------------------------------------------------------------- round trip and random nonce


def test_seal_unseal_round_trip():
    impl = seal.real()
    machine = b'\x01' * 32
    plaintext = seal.encode_plaintext({'swarm-agent': 'new-tok'})
    payload = impl.seal(machine, plaintext)
    assert impl.unseal(machine, payload) == plaintext


def test_sealing_is_confidential_not_plaintext_with_tag():
    """The real AEAD must NOT be the rotation stand-in's shape: the plaintext is not recoverable by a
    reader of the payload, and the payload is a versioned header + nonce + ciphertext (with tag)."""
    impl = seal.real()
    machine = b'\x02' * 32
    plaintext = seal.encode_plaintext({'swarm-agent': 'secret-value'})
    payload = impl.seal(machine, plaintext)
    assert plaintext not in payload, 'the real AEAD must encrypt, not carry the plaintext beside a tag'
    assert payload.startswith(b'rot:crypto:1:')


def test_two_seals_of_the_same_plaintext_differ_nonce_is_random():
    """A fresh random nonce per seal means the same machine key + plaintext gives different payloads.
    Deterministic encryption would let a forge observer detect an unchanged payload; random nonces
    deny that and are the safe default for a secret."
    """
    impl = seal.real()
    machine = b'\x03' * 32
    plaintext = seal.encode_plaintext({'swarm-agent': 'tok'})
    a = impl.seal(machine, plaintext)
    b = impl.seal(machine, plaintext)
    assert a != b, 'two seals with a random nonce must differ'
    assert impl.unseal(machine, a) == plaintext
    assert impl.unseal(machine, b) == plaintext


def test_two_seals_with_the_same_key_both_open_key_derivation_is_stable():
    """HKDF is deterministic: sealing twice with the same machine key yields two independent payloads
    that BOTH open with that key -- the key-derivation must not change between calls."""
    impl = seal.real()
    machine = b'\x04' * 32
    plaintext = seal.encode_plaintext({'swarm-agent': 'tok'})
    payloads = [impl.seal(machine, plaintext) for _ in range(3)]
    for payload in payloads:
        assert impl.unseal(machine, payload) == plaintext


# --------------------------------------------------------------------------- wrong key, tamper, truncation, isolation


def test_a_wrong_machine_key_cannot_open_a_sealed_payload():
    impl = seal.real()
    machine = b'A' * 32
    other = b'B' * 32
    payload = impl.seal(machine, seal.encode_plaintext({'swarm-agent': 'tok'}))
    with pytest.raises(seal.SealError):
        impl.unseal(other, payload)


def test_a_tampered_payload_is_refused_not_silently_truncated():
    impl = seal.real()
    machine = b'\x05' * 32
    payload = bytearray(impl.seal(machine, seal.encode_plaintext({'swarm-agent': 'tok'})))
    payload[-1] ^= 0x01  # flip one ciphertext/tag byte: a forgery
    with pytest.raises(seal.SealError):
        impl.unseal(machine, bytes(payload))


def test_a_truncated_payload_is_refused_not_silently_truncated():
    impl = seal.real()
    machine = b'\x06' * 32
    payload = impl.seal(machine, seal.encode_plaintext({'swarm-agent': 'tok'}))
    with pytest.raises(seal.SealError):
        impl.unseal(machine, payload[:10])
    with pytest.raises(seal.SealError):
        impl.unseal(machine, payload[:-1])


def test_a_payload_for_one_machine_cannot_be_opened_by_another():
    """Two machines each open ONLY their own payload -- cross-machine isolation is the whole point of
    sealing to the machine key, so one machine's leaked ciphertext buys no replacement on another."""
    impl = seal.real()
    ws1 = b'\x10' * 32
    ws2 = b'\x20' * 32
    p1 = impl.seal(ws1, seal.encode_plaintext({'swarm-agent': 'w1'}))
    p2 = impl.seal(ws2, seal.encode_plaintext({'swarm-agent': 'w2'}))
    assert impl.unseal(ws1, p1) == seal.encode_plaintext({'swarm-agent': 'w1'})
    assert impl.unseal(ws2, p2) == seal.encode_plaintext({'swarm-agent': 'w2'})
    with pytest.raises(seal.SealError):
        impl.unseal(ws2, p1)
    with pytest.raises(seal.SealError):
        impl.unseal(ws1, p2)


# --------------------------------------------------------------------------- absent cryptography: import survives, seal refuses


def test_the_module_imports_without_cryptography():
    """`import agent_swarm.seal` must not reach for `cryptography`; a bare Gitea checkout imports this
    module and only fails at the seal attempt, not at import time."""
    source = open(seal.__file__, encoding='utf-8').read()  # noqa: SIM115 - test only
    assert 'import cryptography' not in source.split('def real()')[0], 'cryptography must be imported lazily'


def test_real_RAISES_a_clear_reason_when_cryptography_is_absent(monkeypatch):
    """Absent the `crypto` extra, `seal.real()` must RAISE loudly (naming the extra), never silently
    return a no-op -- the shape `unimplemented()` established, so a caller cannot mistake "refused"
    for "sealed"."""
    real_import = builtins.__import__

    def block(name, *args, **kwargs):
        if name == 'cryptography' or name.startswith('cryptography.'):
            raise ImportError('cryptography not installed')
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, '__import__', block)
    with pytest.raises(NotImplementedError, match='crypto'):
        seal.real()


def test_unimplemented_default_still_RAISES_not_silently_opens():
    """The never-silent default remains reachable: a box with NO AEAD at all gets a loud refusal at
    the attempt, exactly as before the real one existed."""
    bogus = seal.unimplemented()
    with pytest.raises(NotImplementedError):
        bogus.seal(b'k' * 32, b'plaintext')
    with pytest.raises(NotImplementedError):
        bogus.unseal(b'k' * 32, b'whatever')
