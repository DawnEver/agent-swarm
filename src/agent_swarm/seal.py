"""The SEAL/UNSEAL seam for a rotation payload, and the honest refusal when no AEAD is available.

WHAT THIS IS. Token rotation publishes a role-token replacement as CIPHERTEXT on the forge, sealed
to the target machine's key, so a leaker who can read the ref gets no replacement. The sealing
needs an authenticated-encryption primitive -- an AEAD. This module is the interface that primitive
fits into, and the place that says which side of the deployment provides it.

WHY IT IS AN INTERFACE AND A REFUSAL, NOT AN IMPLEMENTATION. The package's `dependencies = []`
(see `pyproject.toml`) is what lets `agent_swarm` run from a bare source checkout on the Gitea host
-- and the Gitea host is the only place with minting authority. So the tempting place to implement
the crypto is here, on the host. But the host has NO AEAD primitive available, measured 2026-08-14:

    hashlib.scrypt        present   (a KDF, not a cipher)
    hashlib.pbkdf2_hmac   present   (a KDF, not a cipher)
    hashlib.hkdf          ABSENT
    hmac                  present   (a MAC, not a cipher)
    any AES/ChaCha block/stream cipher  ABSENT from the stdlib

Encryption requires a CIPHER. The stdlib supplies hashes, KDFs and a MAC but no cipher, so any AEAD
built purely from it is a hand-rolled stream or Feistel construction derived by XORing a scrypt
keystream into the plaintext. That is exactly the shape this project refuses to ship for a secret:
"加密后 MAC" with a self-made stream cipher is not a known-answer-tested construction, and a
credential path is the one place a plausible-looking cipher is worse than a loud refusal. So the
honest deliverable is an interface, an exception, and a DEFAULT that RAISES -- never a silent no-op
and never a made-up cipher.

WHERE THE REAL AEAD LIVES. FALLBACK A (the design doc): sealing happens on the box WITH
dependencies -- a machine where `cryptography` or `nacl` is installed -- and the seal/unseal pair is
passed IN. The client machine only ever UNSEALS, and it receives the `unseal` callable from the
caller. No copy of `agent_swarm` invents a cipher; a caller with a real AEAD supplies one, and
anything without one fails loudly at the call rather than at a point where nobody is looking.

THE REAL ONE IS HERE NOW, BEHIND AN EXTRA. `seal.real()` is a genuine, audited AEAD -- AESGCM from
the `cryptography` library, keyed by HKDF-SHA256 expansion of the machine key -- supplied to the
rotation path the same way Fallback A said a box-with-deps supplies it. It is guarded by the `crypto`
extra (`pyproject.toml`): absent `cryptography`, `import agent_swarm` still succeeds, `seal.real()`
RAISES a clear reason, and `seal.unimplemented()` remains the never-silent default. The seam is
unchanged -- `RotationSeal` still has the same two-method shape, so the orchestration tests and the
client both keep passing through it.

THIS MODULE RUNS ON THE BOX WITH DEPENDENCIES. It is the seam, stated once, so nobody re-derives
"stdlib is enough" by looking at the imports list and seeing nothing third-party.
"""

from __future__ import annotations

import base64
import json
import os
from typing import Protocol, runtime_checkable


class SealError(RuntimeError):
    """A payload could not be unsealed: the wrong machine key, or a tampered / truncated payload.

    NEVER A SILENT TRUNCATION. A MAC failure is indistinguishable, here, from a wrong key, and both
    must REACH the caller -- the caller's job is to NOT store and NOT confirm, so the rotator can
    never conclude "delivered" from a payload that failed to open. If a failure could return a
    prefix, an attacker truncating the ciphertext would ship a replacement that is not the rotator's,
    and the ordering guarantee would be about a payload that never arrived.
    """


@runtime_checkable
class RotationSeal(Protocol):
    """A symmetric AEAD sealed to a machine key. THE SEAM the rotation path tests against.

    `seal`/`unseal` are NOT provided by this package (see the module docstring -- the stdlib has no
    AEAD cipher, and this project refuses to hand-roll one). A real implementation lives on the box
    WITH dependencies and is passed in at the call; the tests inject a stand-in that exercises the
    orchestration without claiming to be production crypto.

    Both sides are bytes: the machine key, the plaintext, and the sealed payload. JSON encoding of
    the plaintext is the CALLER's concern (see `agent_swarm.rotation`), not this seam's.
    """

    def seal(self, machine_key: bytes, plaintext: bytes) -> bytes:
        """Seal `plaintext` so only `machine_key` can open it, with authenticated integrity."""
        ...

    def unseal(self, machine_key: bytes, payload: bytes) -> bytes:
        """Open `payload`; raise :class:`SealError` on a wrong key, tampering, or truncation."""
        ...


def unimplemented() -> RotationSeal:
    """The DEFAULT on a box with no AEAD: a seal/unseal that RAISES rather than silently doing nothing.

    RETURNED AS A VALUE rather than raised at import time, so a caller can hold the interface and
    still receive a loud failure exactly when crypto is touched -- which is the point of the shape.
    An import-time raise would fire on every run that merely imports this module; a runtime raise
    fires only on the attempt, and the attempt is where a silent wrongness would be doing its damage.

    Raises:
        NotImplementedError: seal or unseal is called. Names the box with dependencies that must
            supply a real AEAD (Fallback A), because the Gitea host has none.
    """

    class _Unimplemented:
        _msg = (
            'no AEAD is available here: the stdlib has no cipher (only scrypt/pbkdf2/hmac), and '
            'this project refuses to hand-roll one. Seal/unseal must be supplied by the box WITH '
            'dependencies (Fallback A: seal on a box where `cryptography`/`nacl` is installed, '
            'unseal passed in here). Pass a RotationSeal into the rotation call instead of using '
            'this default.'
        )

        def seal(self, _machine_key: bytes, _plaintext: bytes) -> bytes:
            raise NotImplementedError(_Unimplemented._msg)

        def unseal(self, _machine_key: bytes, _payload: bytes) -> bytes:
            raise NotImplementedError(_Unimplemented._msg)

    return _Unimplemented()


# --------------------------------------------------------------------------- the real AEAD (cryptography, behind the `crypto` extra)

_HEADER = b'rot:crypto:1:'  # versioned so a future cipher or framing change reads, not guesses
_NONCE_BYTES = 12  # AES-GCM standard 96-bit nonce; random per seal (reuse would leak keystream xor)
_TAG_BYTES = 16  # AES-GCM authentication tag
_KEY_BYTES = 32  # AES-256

# Domain separation: the KDF info and the AAD are both this package's namespace, so a machine key
# derived for THIS purpose can never be confounded with one derived for another call site, and a
# payload cannot be replayed under a different AAD.
_KDF_INFO = b'agent-swarm:rotation-seal:key:v1'
_AAD = b'agent-swarm:rotation:seal:v1'


class _AESGCMSeal:
    """A ``RotationSeal`` backed by ``cryptography``'s AESGCM -- the audited AEAD, not a hand-rolled one.

    Key derivation: HKDF-SHA256 of the machine key, 32-byte output, ``info=_KDF_INFO``. HKDF over
    scrypt because the machine key is ALREADY high-entropy -- `credentials.generate_machine_key` is
    `secrets.token_bytes(32)`, i.e. 256 CSPRNG bits. scrypt's cost exists to slow an attacker
    brute-forcing a low-entropy password; it is pure waste on an already-random secret, whereas HKDF
    is the correct primitive to expand one uniformly-random input into a fixed-length key with domain
    separation. Both ends use the same info, so an old payload still opens and a new one does not
    silently change semantics.
    """

    def __init__(self, aesgcm_cls, hkdf_cls, hashes):
        # Injected so the class never imports cryptography at module scope (lazy, behind the extra);
        # `real()` does the one-time lazy import and passes the types in.
        self._aesgcm_cls = aesgcm_cls
        self._hkdf_cls = hkdf_cls
        self._hashes = hashes

    def _derive(self, machine_key: bytes) -> bytes:
        return self._hkdf_cls(algorithm=self._hashes.SHA256(), length=_KEY_BYTES, salt=None, info=_KDF_INFO).derive(
            machine_key
        )

    def seal(self, machine_key: bytes, plaintext: bytes) -> bytes:
        key = self._derive(machine_key)
        nonce = os.urandom(_NONCE_BYTES)
        ciphertext = self._aesgcm_cls(key).encrypt(nonce, plaintext, _AAD)
        return _HEADER + nonce + ciphertext

    def unseal(self, machine_key: bytes, payload: bytes) -> bytes:
        if not payload.startswith(_HEADER) or len(payload) < len(_HEADER) + _NONCE_BYTES + _TAG_BYTES:
            raise SealError('not a rotation payload (truncated or wrong format)')
        nonce = payload[len(_HEADER) : len(_HEADER) + _NONCE_BYTES]
        ciphertext = payload[len(_HEADER) + _NONCE_BYTES :]
        key = self._derive(machine_key)
        try:
            return self._aesgcm_cls(key).decrypt(nonce, ciphertext, _AAD)
        except Exception as exc:
            # AESGCM raises InvalidTag on a wrong key, tampering, or truncation -- and a truncated
            # body can surface as a ValueError from the framing, so convert every decrypt failure to
            # SealError. NEVER return a prefix: the caller must not conclude "delivered" from a
            # payload that failed to open, and a truncated ciphertext is exactly the shape that would
            # otherwise ship a non-rotator replacement.
            raise SealError('tampered payload or wrong machine key') from exc


def real() -> RotationSeal:
    """The genuine AEAD seal/unseal for a machine that has the `crypto` extra installed.

    RAISES WHEN `cryptography` IS ABSENT, rather than silently degrading -- the shape `unimplemented()`
    already established. Importing `agent_swarm.seal` never imports `cryptography`; only reaching this
    function does, and only here does its absence become a loud, named reason. The implementation
    returned satisfies :class:`RotationSeal`, so it drops into the same seam the orchestration and the
    client already use.

    Raises:
        NotImplementedError: `cryptography` (the `crypto` extra) is not installed. The Gitea host,
            running from a bare checkout, hits this; a box with `agent-swarm[crypto]` gets a real AEAD.

    """
    # Lazy import behind the `crypto` extra, so a bare checkout (`dependencies = []`) still imports
    # this module. The AGENTS convention wants a comment on any lazy import naming why -- this is it.
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: PLC0415
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF  # noqa: PLC0415
        from cryptography.hazmat.primitives import hashes  # noqa: PLC0415
    except ImportError as exc:
        msg = (
            'no AEAD is available: the `crypto` extra (the audited `cryptography` library) is not '
            'installed. Install it with `pip install agent-swarm[crypto]` (or `uv pip install '
            'cryptography`) on a box WITH dependencies; the Gitea host has none and must not. This '
            'never hand-rolls a cipher -- absent the library, seal/unseal refuse loudly.'
        )
        raise NotImplementedError(msg) from exc
    return _AESGCMSeal(AESGCM, HKDF, hashes)


def encode_plaintext(tokens: dict[str, str]) -> bytes:
    """The wire form of a sealed rotation: JSON bytes. Kept HERE so seal and the vocabulary share it.

    `apply_sealed_rotation` decodes whatever this produced, so a key/value shape change has one home.
    The plaintext carries role credentials, so it never passes through a log or an error message.
    """
    return json.dumps(tokens, sort_keys=True).encode('utf-8')


def decode_plaintext(data: bytes) -> dict[str, str]:
    """The inverse of :func:`encode_plaintext`. Malformed plaintext is a ``ValueError``, never a
    silent `{}` -- a rotation that opened but carried nothing must not read as "delivered nothing"."""
    raw = json.loads(data.decode('utf-8'))
    return {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}


def to_bundle(key: bytes) -> str:
    """A machine key as it travels in an emit/consume bundle: base64, NOT encrypted.

    The bundle is already a plaintext, owner-only, offline move (see `swarmctl`'s `emit`); a machine
    key beside the role tokens it was issued with is no more exposure than the tokens themselves.
    Base64 is a serialisation, never a secrecy claim.
    """
    return base64.b64encode(key).decode('ascii')


def from_bundle(text: str) -> bytes:
    """The inverse of :func:`to_bundle`. A malformed key string is a ``ValueError``, not a silent
    empty key -- a bundle that yields no key must fail the consume, not enrol a keyless machine."""
    return base64.b64decode(text.encode('ascii'))
