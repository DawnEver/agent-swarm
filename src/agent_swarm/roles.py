"""THE ONE OWNER OF WHAT THIS SWARM'S ACCOUNTS ARE CALLED.

WHY IT IS ITS OWN MODULE AND NOT A CONSTANT SOMEWHERE. The scheme had FOUR spellings, found
2026-08-11 while auditing a fifth:

    swarmctl.USERS            derived from a prefix -- correct, but only one of four
    forge.ROLE_ACCOUNTS       four hand-written literals
    credentials.env_var_for   `username.startswith('swarm-')`, the prefix inlined
    workbench_cli             `username='swarm-agent'`, a bare literal

Duplication is the small half. The load-bearing half is that TWO of them each declared itself the
sole authority -- `forge.ROLE_ACCOUNTS` said "this mapping is the ONLY thing that makes a process act
as one role rather than another", and `swarmctl.USERS` said the same of itself. A reader who
believed either would edit one and ship a fleet whose components disagree about who they are. That
is a scope-lie in both directions: not a wrong comment, but one that routes you away from the other
copy.

THE FIX HAD TO BE A NEW MODULE, and the arrow says why: `forge` is DRIVER and `swarmctl` is ENTRY, so
`forge` CANNOT import the derivation from `swarmctl` without pointing up. The fact must live at or
below the lowest consumer. It is HOST because it imports nothing and knows nothing about Gitea --
these names would be the same against any forge.

WHAT LIVES HERE AND WHAT DOES NOT. Here: the role vocabulary and the account spelling. NOT here: the
units and scopes each role is granted (`swarmctl.ROLES`), because that is provisioning policy about
one server's permission model, and the team names, which are derived beside it. The link is checked
rather than trusted -- `test_the_role_vocabulary_has_one_owner.py` asserts those key sets agree, so a
fifth role added to one and not the other reds instead of half-existing.
"""

from __future__ import annotations

PREFIX = 'swarm'
"""The one home of the prefix. Every account name, and every team name, is built from it."""

ROLE_NAMES: tuple[str, ...] = ('observer', 'agent', 'verifier', 'integrator')
"""The role vocabulary, in ascending privilege. ORDER IS MEANINGFUL to a reader and to nothing else;
no code may index into this, because a role's identity is its name.
"""

ACCOUNTS: dict[str, str] = {role: f'{PREFIX}-{role}' for role in ROLE_NAMES}
"""role -> the forge account it authenticates as.

`git credential fill` keys on (protocol, host, USERNAME), so this mapping is what makes a process act
as one role rather than another. NOT SERVER-ENFORCED: Gitea has no scope for commit status, so
"only the verifier marks a commit green" is carried by which process holds which credential.
Measured 2026-08-10.
"""


def account_for(role: str) -> str:
    """`agent` -> `swarm-agent`. KeyError on an unknown role, deliberately -- a typo'd role that
    silently produced a plausible account name would authenticate as nobody and fail far away."""
    return ACCOUNTS[role]


def role_of(account: str) -> str | None:
    """`swarm-agent` -> `agent`; None for anything this swarm did not issue.

    THREE-VALUED BY OMISSION IS THE HAZARD HERE, so it is stated: None means "not ours", never "not
    allowed". A caller that turns None into a refusal is accusing a human's own account of
    misbehaving when it is merely a stranger.
    """
    return next((role for role, name in ACCOUNTS.items() if name == account), None)


def strip_prefix(account: str) -> str:
    """The bare part of an account name, for composing per-role identifiers.

    Tolerates an unprefixed name rather than raising: `credentials.env_var_for` normalises ANY
    username so the env-var scheme has no hole, and that must keep working for accounts this module
    does not know.
    """
    return account[len(PREFIX) + 1 :] if account.startswith(f'{PREFIX}-') else account
