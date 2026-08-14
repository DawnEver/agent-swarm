r"""Machine-key-sealed role-token rotation: the forge is the channel, the seal carries the trust.

THE DESIGN, IN ONE PARAGRAPH. Role tokens stand forever and are never rotated (a grep for "rotat"
finds only list rotation), so a leaked credential is a leaked credential for life. Minting authority
stays on the Gitea host -- a role token cannot mint its own replacement, and giving it `write:user`
would let a leaked token renew itself forever, which voids rotation. So a machine carries a MACHINE
KEY (established at enrolment over the existing emit/consume bundle), the rotator publishes a
replacement SEALED to that key on the forge at `refs/swarm/rotation/<machine>/<epoch>`, and the
machine fetches with the very credential being rotated, unseals with its key, atomically replaces
its tokens, and CONFIRMS -- after which, and only after which, the rotator may revoke the old token.

THE SEAL IS THE TRUST, NOT THE CHANNEL. A leaker who can read the forge's refs reads CIPHERTEXT; a
leaked role token buys no replacement. And the ordering is load-bearing: revoke-before-delivery
isolates an offline machine in the window (its credential is dead, its bundle is one-shot), so the
rotator must wait for the confirmation. Both halves are pinned here.

WHY THE SEAL IS A STAND-IN IN THIS FILE. The Gitea host has no AEAD primitive (measured 2026-08-14:
the stdlib has scrypt/pbkdf2/hmac but no cipher, and this project refuses to hand-roll one -- see
`agent_swarm.seal`). So `seal`/`unseal` are an interface, implemented on the box WITH dependencies
(Fallback A) and passed in. The `_DoubleSeal` here is a TEST-ONLY stand-in whose job is to behave
like a correct AEAD -- reject a wrong key, reject a tampered payload -- so the ORCHESTRATION can be
tested. It makes no claim to confidentiality (it carries the plaintext), because confidentiality is
the real AEAD's job and lives off-package.
"""

from __future__ import annotations

import base64
import hashlib
import hmac

import pytest

from agent_swarm import credentials, refs, rotation, seal


# --------------------------------------------------------------------------- the test-only AEAD stand-in


_MAGIC = b'rot:1:'


def _double_seal(machine_key: bytes, plaintext: bytes) -> bytes:
    """A stand-in for the box-with-deps AEAD's `seal`. Authenticates with HMAC; does NOT encrypt,
    deliberately -- confidentiality is the real AEAD's job, and the orchestration under test here
    needs only correct accept/reject behaviour."""
    tag = hmac.new(machine_key, plaintext, hashlib.sha256).digest()
    return _MAGIC + tag + plaintext


def _double_unseal(machine_key: bytes, payload: bytes) -> bytes:
    """The stand-in `unseal`: wrong key or any tampering RAISES `SealError`, never returns a prefix."""
    if not payload.startswith(_MAGIC) or len(payload) < len(_MAGIC) + hashlib.sha256().digest_size:
        raise seal.SealError('not a rotation payload (truncated or wrong format)')
    digest_size = hashlib.sha256().digest_size
    tag, body = payload[len(_MAGIC) : len(_MAGIC) + digest_size], payload[len(_MAGIC) + digest_size :]
    if not hmac.compare_digest(tag, hmac.new(machine_key, body, hashlib.sha256).digest()):
        raise seal.SealError('tampered payload or wrong machine key')
    return body


# --------------------------------------------------------------------------- the ref namespace and its retention


def test_the_rotation_ref_grammar_is_well_formed():
    ref = refs.rotation_ref('WS1', 7)
    assert ref == 'refs/swarm/rotation/WS1/7'
    assert refs.rotation_machine(ref) == 'WS1'
    assert refs.rotation_epoch(ref) == 7
    assert refs.rotation_machine('refs/verdicts/abc/kind/env') is None
    assert refs.rotation_epoch(refs.rotation_ref('WS1', 10)) == 10, 'the epoch must sort as a number'


def test_the_rotation_namespace_is_age_swept_in_the_same_commit():
    """The repository rule: a new ref namespace is swept by age the same commit it is introduced.
    A sealed rotation payload is a live credential in ciphertext, so one that lingers is a standing
    recoverable secret rather than a stale answer -- doubly worth sweeping."""
    assert refs.rotation_glob() in refs.aged_globs()


# --------------------------------------------------------------------------- the seal is an interface, not a hand-rolled cipher


def test_unimplemented_seal_RAISES_instead_of_silently_opening():
    """The honest deliverable where no AEAD exists. A silent no-op here would read as "sealed" while
    shipping plaintext; an import-time raise would break every import. The failure is AT THE ATTEMPT."""
    bogus = seal.unimplemented()
    with pytest.raises(NotImplementedError):
        bogus.seal(b'k' * 32, b'plaintext')
    with pytest.raises(NotImplementedError):
        bogus.unseal(b'k' * 32, b'whatever')


def test_the_seal_interface_exists_and_accepts_a_real_implementation():
    """`RotationSeal` is a Protocol a box-with-deps implementation can satisfy -- the seam the tests
    and the client both pass through, rather than a fixed function."""
    impl = type('_Seal', (), {'seal': _double_seal, 'unseal': _double_unseal})()
    assert isinstance(impl, seal.RotationSeal)


# --------------------------------------------------------------------------- a sealed payload: wrong key and tampering


def test_a_payload_sealed_for_another_machine_key_cannot_be_opened():
    machine = b'A' * 32
    other = b'B' * 32
    payload = _double_seal(machine, seal.encode_plaintext({'swarm-agent': 'new-tok'}))
    with pytest.raises(seal.SealError):
        _double_unseal(other, payload)


def test_a_tampered_payload_is_refused_not_silently_truncated():
    machine = b'A' * 32
    payload = bytearray(_double_seal(machine, seal.encode_plaintext({'swarm-agent': 'new-tok'})))
    payload[-1] ^= 0x01  # flip one ciphertext byte: a forgery of the body
    with pytest.raises(seal.SealError):
        _double_unseal(machine, bytes(payload))


def test_a_truncated_payload_is_refused_not_silently_truncated():
    machine = b'A' * 32
    payload = _double_seal(machine, seal.encode_plaintext({'swarm-agent': 'new-tok'}))
    with pytest.raises(seal.SealError):
        _double_unseal(machine, payload[:10])


# --------------------------------------------------------------------------- the client: fetch -> unseal -> atomic replace


def test_apply_replaces_every_role_and_confirms():
    machine = b'A' * 32
    usernames = ['swarm-agent', 'swarm-observer', 'swarm-verifier', 'swarm-integrator']
    replacement = {username: f'new-{username}' for username in usernames}
    payload = _double_seal(machine, seal.encode_plaintext(replacement))
    stored: dict[str, str] = {}

    confirmation = rotation.apply_sealed_rotation(
        payload=payload,
        machine_key=machine,
        unseal=_double_unseal,
        store=lambda username, token: stored.__setitem__(username, token),
        expected_usernames=usernames,
        ref='refs/swarm/rotation/WS1/1',
    )

    assert stored == replacement
    assert confirmation.ref == 'refs/swarm/rotation/WS1/1'
    assert set(confirmation.roles) == set(usernames)


def test_apply_writes_through_the_real_owner_only_store(tmp_path, monkeypatch):
    """The atomic replace is the REAL `credentials.store_token`, which reads back and refuses a write
    that did not keep -- so "replaced" means what the store actually holds, not what was handed to it."""
    store = tmp_path / 'credentials.json'
    monkeypatch.setattr(credentials, 'store_path', lambda: store)
    machine = b'\x41' * 32
    usernames = ['swarm-agent', 'swarm-verifier']
    for username in usernames:
        credentials.store_token('http', 'h:9000', username, 'old-' + username)
    payload = _double_seal(machine, seal.encode_plaintext({u: 'new-' + u for u in usernames}))

    rotation.apply_sealed_rotation(
        payload=payload,
        machine_key=machine,
        unseal=_double_unseal,
        store=lambda u, t: credentials.store_token('http', 'h:9000', u, t),
        expected_usernames=usernames,
        ref='refs/swarm/rotation/WS1/2',
    )

    for username in usernames:
        assert credentials.resolve_token('http', 'h:9000', username, env={}) == 'new-' + username


def test_a_wrong_machine_key_applies_nothing():
    machine = b'A' * 32
    other = b'B' * 32
    payload = _double_seal(machine, seal.encode_plaintext({'swarm-agent': 'new-tok'}))
    stored: dict[str, str] = {}
    with pytest.raises(seal.SealError):
        rotation.apply_sealed_rotation(
            payload=payload,
            machine_key=other,
            unseal=_double_unseal,
            store=lambda u, t: stored.__setitem__(u, t),
            expected_usernames=['swarm-agent'],
            ref='refs/swarm/rotation/WS1/1',
        )
    assert stored == {}, 'nothing may be stored from a payload that failed to open'


def test_a_partial_replacement_stores_nothing_and_confirms_nothing():
    """A replacement that names only some of the expected roles must not be half-applied -- a role
    silently keeping its stale token is the thing rotation exists to remove."""
    machine = b'A' * 32
    payload = _double_seal(machine, seal.encode_plaintext({'swarm-agent': 'new-tok'}))
    stored: dict[str, str] = {}
    with pytest.raises(rotation.RotationError, match='missing swarm-verifier'):
        rotation.apply_sealed_rotation(
            payload=payload,
            machine_key=machine,
            unseal=_double_unseal,
            store=lambda u, t: stored.__setitem__(u, t),
            expected_usernames=['swarm-agent', 'swarm-verifier'],
            ref='refs/swarm/rotation/WS1/1',
        )
    assert stored == {}, 'nothing may be stored from an incomplete replacement'


def test_an_unexpected_role_is_refused_not_silently_deposited():
    machine = b'A' * 32
    payload = _double_seal(machine, seal.encode_plaintext({'swarm-agent': 'new', 'evil': 'tok'}))
    stored: dict[str, str] = {}
    with pytest.raises(rotation.RotationError, match='unknown evil'):
        rotation.apply_sealed_rotation(
            payload=payload,
            machine_key=machine,
            unseal=_double_unseal,
            store=lambda u, t: stored.__setitem__(u, t),
            expected_usernames=['swarm-agent'],
            ref='refs/swarm/rotation/WS1/1',
        )
    assert stored == {}


# --------------------------------------------------------------------------- the revoke order: delivery confirms first


def test_revoke_happens_only_after_the_confirmation():
    confirmed = {'refs/swarm/rotation/WS1/3'}
    revoked: list[str] = []
    rotation.revoke_after_delivery(confirmed=confirmed, ref='refs/swarm/rotation/WS1/3', revoke=revoked.append)
    assert revoked == ['refs/swarm/rotation/WS1/3']


def test_an_offline_machine_in_the_window_is_not_revoked():
    """WS1 has NOT confirmed (it is offline); only WS2 has. Revoking WS1 would isolate it -- its
    credential is dead and its bundle is one-shot. The guard must refuse and the revoke must not run."""
    confirmed = {'refs/swarm/rotation/WS2/3'}
    revoked: list[str] = []
    with pytest.raises(rotation.RotationError, match='has not confirmed'):
        rotation.revoke_after_delivery(confirmed=confirmed, ref='refs/swarm/rotation/WS1/3', revoke=revoked.append)
    assert revoked == [], 'the revoke must not be reached on an unconfirmed delivery'


def test_the_revoke_order_is_a_rule_not_a_convention():
    """Even a rotator that forgets to check cannot revoke-before-delivery: the guard raises on the
    bare ref, so the only path that revokes is the one that already confirmed."""
    with pytest.raises(rotation.RotationError):
        rotation.guard_delivery_before_revoke(confirmed=set(), ref='refs/swarm/rotation/WS1/3')


# --------------------------------------------------------------------------- the machine key: owner-only, isolated, not in git env


@pytest.fixture
def store(tmp_path, monkeypatch):
    path = tmp_path / 'credentials.json'
    monkeypatch.setattr(credentials, 'store_path', lambda: path)
    return path


def test_machine_key_round_trips_in_the_owner_only_store(store):
    key = credentials.generate_machine_key()
    credentials.store_machine_key('WS1', 'h:9000', key)
    assert credentials.resolve_machine_key('WS1', 'h:9000') == key
    assert credentials.resolve_machine_key('WS1', 'h:9000') == key


def test_machine_key_is_isolated_from_role_tokens(store):
    """The machine key lives under `machine://<machine>@<host>`, a namespace no role-token lookup
    reaches -- so a credential path that touches role tokens can never hand the key to a transport."""
    key = b'\x42' * 32
    credentials.store_machine_key('WS1', 'h:9000', key)
    credentials.store_token('http', 'h:9000', 'swarm-agent', 'role-tok')
    for username in ('swarm-agent', 'swarm-observer', 'swarm-verifier', 'swarm-integrator'):
        token = credentials.resolve_token('http', 'h:9000', username, env={})
        assert token is None or token == 'role-tok', 'a role-token lookup returned the machine key'
    assert credentials.resolve_token('http', 'h:9000', 'swarm-observer', env={}) is None
    assert credentials.resolve_machine_key('WS1', 'h:9000') == key


def test_machine_key_never_enters_the_git_environment(store):
    """`git_env_for` authenticates a role with `SWARM_ASKPASS_TOKEN`; the machine key is not among
    them. A key that rode into a child git process would be a standing export -- this pins it absent."""
    key = b'\x7f' * 32
    credentials.store_machine_key('WS1', 'h:9000', key)
    credentials.store_token('http', 'h:9000', 'swarm-agent', 'role-tok')
    with credentials.git_env_for('http', 'h:9000', 'swarm-agent') as env:
        assert env['SWARM_ASKPASS_TOKEN'] == 'role-tok'
        key_strings = {key.decode('latin1'), base64.b64encode(key).decode('ascii')}
        assert not (set(env.values()) & key_strings), 'the machine key leaked into the git environment'


def test_machine_key_serialises_through_the_bundle_round_trip():
    """`emit` ships the key base64 in the bundle; `consume` reads it back to the same bytes. The
    bundle is already a plaintext owner-only move, so base64 here is serialisation, not secrecy."""
    key = credentials.generate_machine_key()
    assert seal.from_bundle(seal.to_bundle(key)) == key


def test_a_malformed_bundle_key_fails_the_consume_not_a_keyless_machine():
    with pytest.raises(ValueError):
        seal.from_bundle('a')
