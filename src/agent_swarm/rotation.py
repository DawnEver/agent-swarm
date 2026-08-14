"""The CLIENT half of role-token rotation: open a sealed payload, replace tokens atomically, confirm.

THE TWO SIDES, AND WHICH HALF LIVES HERE. Minting authority stays on the Gitea host and SEALING
happens on the box-with-dependencies (Fallback A) -- see :mod:`agent_swarm.seal`. What lives here is
the part every fleet machine runs on its TICK: fetch the sealed payload for itself from the forge,
unseal it with its MACHINE KEY, validate it is a complete replacement, store it with the same
owner-only atomic write that established the originals, and emit a CONFIRMATION the rotator must wait
for before it may revoke the old token.

WHY THE ORDER IS LOAD-BEARING (revoke after delivery, never before). A machine that is offline during
the window holds only its old token. Revoking the old token before delivering the replacement
isolates that machine -- it can neither fetch (its credential is dead) nor be re-sealed to (its key
is gone, the bundle is one-shot). So the ONLY order the rotator may use is: deliver and receive the
confirmation, THEN revoke. This module provides the guard that makes revoke-before-delivery a hard
error rather than a convention, and the test suite pins both halves.

SEALED, NOT CHANNEL-AUTHENTICATED. The machine fetches over the ordinary git transport using the very
credential being rotated; a leaker who can read refs reads CIPHERTEXT sealed to the machine key, so a
leaked role token buys no replacement. Nothing here trusts the channel -- the payload must open with
the machine key or the whole apply is refused and nothing is stored or confirmed.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence, Set
from dataclasses import dataclass

from agent_swarm.seal import SealError


class RotationError(RuntimeError):
    """A rotation could not be applied safely: a malformed replacement, or a revoke out of order.

    The message never carries a token. A rotation payload is a credential, so a failure that
    rendered it would be the leak; the caller is told WHICH role was missing or WHICH order was
    broken, never what the payload contained.
    """


@dataclass(frozen=True, slots=True)
class Confirmation:
    """The machine's word that it stored the replacement named by `ref` and its roles.

    A CONFIRMATION IS THE THING THE ROTATOR WAITS FOR, and it is only produced by a successful
    :func:`apply_sealed_rotation` -- never by an attempt that opened but failed to validate, and
    never by one that raised :class:`SealError`. That is what makes "offline in the window" unable to
    be revoked: an offline machine produces no confirmation, so the guard refuses its revoke.
    """

    ref: str
    roles: tuple[str, ...]


def apply_sealed_rotation(
    *,
    payload: bytes,
    machine_key: bytes,
    unseal: Callable[[bytes, bytes], bytes],
    store: Callable[[str, str], None],
    expected_usernames: Sequence[str],
    ref: str,
) -> Confirmation:
    """Open, validate, then atomically replace -- in that order, and nothing stored before validation.

    RAISE, NEVER PARTIAL. If the payload does not open (:class:`SealError` -- wrong key or tampered)
    nothing is stored and no confirmation is produced. If it opens but is not a COMPLETE replacement
    (every expected username present, no surprises) a :class:`RotationError` is raised and again
    nothing is stored. Only a payload that fully validates reaches `store`, and only then does a
    confirmation exist for the rotator to wait on.

    `unseal` and `store` are the seams that keep this free of a transport and a crypto stack: `unseal`
    is a real AEAD supplied by the box-with-dependencies (Fallback A), `store` is the caller's
    owner-only atomic write (typically `credentials.store_token`, which reads back and refuses a write
    that did not keep). The FETCH of `payload` from the forge ref is the caller's job, done before
    this is called -- the channel is unauthenticated by design, which is why the seal is what carries
    the trust.

    Raises:
        SealError: the payload could not be opened (wrong machine key, tampered, or truncated).
        RotationError: the payload opened but is not a complete, well-formed replacement.

    """
    try:
        plaintext = unseal(machine_key, payload)
    except SealError:
        raise
    replacement = _complete_replacement(plaintext, expected_usernames)
    for username, token in replacement.items():
        store(username, token)
    return Confirmation(ref=ref, roles=tuple(replacement))


def _complete_replacement(plaintext: bytes, expected_usernames: Sequence[str]) -> dict[str, str]:
    """A replacement that names EXACTLY the expected usernames, or nothing at all.

    EXACT, NOT A SUBSET AND NOT A SUPERSET. A subset means a machine silently keeps a stale token for
    a role the rotator meant to rotate -- a half-rotated credential is the thing rotation exists to
    remove. A superset means a payload that is not the rotator's (or is corrupt) would deposit a
    credential under a username nothing else expects. Both are the fail-open shape, so both raise.
    """
    expected = set(expected_usernames)
    try:
        raw = json.loads(plaintext.decode('utf-8'))
        tokens = {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}
    except (UnicodeDecodeError, ValueError, TypeError):
        msg = f'replacement at {len(plaintext)} bytes is not a JSON token map; refused, nothing stored'
        raise RotationError(msg) from None
    if set(tokens) != expected:
        missing = sorted(expected - set(tokens))
        extra = sorted(set(tokens) - expected)
        detail = []
        if missing:
            detail.append(f'missing {", ".join(missing)}')
        if extra:
            detail.append(f'unknown {", ".join(extra)}')
        msg = f'replacement is not exactly the expected credentials ({"; ".join(detail)}); refused, nothing stored'
        raise RotationError(msg)
    bad = [name for name, token in tokens.items() if not token]
    if bad:
        msg = f'replacement carries an empty token for {", ".join(sorted(bad))}; refused, nothing stored'
        raise RotationError(msg)
    return tokens


def guard_delivery_before_revoke(confirmed: Set[str], ref: str) -> None:
    """Refuse a revoke whose delivery the machine has not confirmed. THE ordering guarantee, as a rule.

    `confirmed` is the set of refs for which a :class:`Confirmation` has been received (delivery is
    confirmed). Revoking `ref` when it is NOT in that set is the revoke-then-deliver order, which
    isolates an offline machine in the window -- so it is a hard error, never a warning, because a
    warning on a completed revoke is the forbidden shape: the damage (a dead machine) is already done
    and the line no longer reads as a guard.

    Raises:
        RotationError: `ref` has not been confirmed delivered. The machine may be offline in the
            window; the rotator must wait, not revoke.

    """
    if ref not in confirmed:
        msg = (
            f'cannot revoke the old token at {ref}: the machine has not confirmed the replacement is '
            f'stored. Revoking before delivery isolates an offline machine in the window -- it can '
            f'neither fetch (its credential is dead) nor be re-sealed (its bundle is one-shot). Wait '
            f'for the confirmation, then revoke.'
        )
        raise RotationError(msg)


def revoke_after_delivery(*, confirmed: Set[str], ref: str, revoke: Callable[[str], None]) -> None:
    """The ONLY rotator path that may revoke an old token: delivery confirmed first, then `revoke(ref)`.

    `revoke` is the seam (typically the provider's `revoke_token` for the old token's id). Calling it
    on an unconfirmed ref raises :class:`RotationError` and the revoke is not reached -- so the
    rotator cannot accidentally run revoke-then-deliver even if it forgets to check, which is the
    point of making the order a rule rather than a habit.
    """
    guard_delivery_before_revoke(confirmed, ref)
    revoke(ref)
