"""PER-ROLE ATTESTATION -- a payload signed by the key of the role that produced it.

WHY THIS EXISTS. Trust today is a git-ref write permission held by a role's credential, so a leaked
credential is a forged truth and a compromised forge can paint any tree green. This module is the
other half of §3.1's fix: a verdict is a payload signed by the producing role's key, and a reader
verifies BEFORE the verdict counts. The forge drops to an untrusted index + transport -- it can see
and move every verdict, and if compromised can DELETE them, but it cannot MANUFACTURE a green trunk,
because the signing key lives only on the reader+producer side and never on the forge.

WHY HMAC-SHA256 AND NOT ED25519 / RSA. agent-swarm is `dependencies=[]` by design ("this layer
decides; it does not reach") and the environment carries no public-key primitives, and a hand-written
asymmetric scheme is forbidden ("a hand-written one is not on the table"). Symmetric HMAC from the
stdlib is the smallest correct primitive under that constraint, and it is ENOUGH here: the reader and
the producer SHARE the role's key -- exactly the trust boundary §3.1 draws -- so the forge, which
never holds the key, cannot compute a valid tag no matter how many signed payloads pass through it. A
leak degrades from "can forge truth" to "can drop garbage". This is NOT an asymmetric-signature
scheme, and it is not pretending to be one: it cannot let a reader verify a payload it does not
itself share a key with the producer for, and it does not need to, because in this swarm they do.

WHAT THIS DOES NOT DO. It does not authenticate a reader, rotate keys, or decide which roles may sign
which verdicts -- that is the forge's role-identity layer. It signs and verifies, nothing more.
"""

from __future__ import annotations

import hashlib
import hmac
import os

from agent_swarm import roles

__all__ = ['SigningKeyUnavailable', 'key_for_role', 'sign', 'verify']


def _env_for(role: str) -> str:
    """The environment variable the role's signing key is delivered in, keyed by role so each
    producer is a distinct signer."""
    return f'AGENT_SWARM_SIGNING_KEY_{role.upper()}'


class SigningKeyUnavailable(RuntimeError):
    """The role's signing key was not supplied. Refused rather than guessed: a key a caller silently
    derives would be one it also silently loses the reader's copy of, and the two would disagree
    forever without any signature ever failing.
    """


def sign(key: bytes | str, payload: bytes | str) -> str:
    """The HMAC-SHA256 tag of `payload` under `key`, as lowercase hex.

    A TAG, not an encryption: the payload is unchanged and the tag is what a reader checks. Hex so
    it rides comfortably in a comment or a ref payload without an encoding dance.
    """
    return hmac.new(_to_bytes(key), _to_bytes(payload), hashlib.sha256).hexdigest()


def verify(key: bytes | str, payload: bytes | str, tag: str) -> bool:
    """Whether `tag` is `sign(key, payload)`. False on ANY mismatch -- including a tag that is not
    the right shape, which is noise rather than an error a reader should have to handle.

    CONSTANT-TIME, and that is the load-bearing half of the module: the comparison routes through
    `hmac.compare_digest`, so it never short-circuits on a matching prefix and a timing side channel
    cannot leak how much of the tag was right.
    """
    if not isinstance(tag, str):
        return False
    try:
        expected = sign(key, payload)
    except (TypeError, ValueError):
        return False
    return hmac.compare_digest(expected, tag)


def key_for_role(role: str) -> bytes:
    """The signing key issued to `role`, delivered alongside its credential.

    READ FROM THE ENVIRONMENT (``AGENT_SWARM_SIGNING_KEY_<ROLE>``) because that is the vehicle a
    credential is delivered by, and it keeps this layer reaching nothing. This is NOT yet stored in
    the credential store -- provisioning the per-role key alongside the forge token is the next step
    and is out of scope here; the environment variable is the honest place the key arrives for now.
    """
    if role not in roles.ROLE_NAMES:
        raise ValueError(f'unknown role {role!r}; known roles are {sorted(roles.ROLE_NAMES)}')
    raw = os.environ.get(_env_for(role))
    if not raw:
        raise SigningKeyUnavailable(f'no signing key for role {role!r}; set {_env_for(role)}')
    return raw.encode()


def _to_bytes(value: bytes | str) -> bytes:
    return value if isinstance(value, bytes) else value.encode()
