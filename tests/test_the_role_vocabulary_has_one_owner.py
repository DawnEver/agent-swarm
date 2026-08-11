"""There is ONE spelling of what this swarm's accounts are called.

MEASURED 2026-08-11, found while auditing something else -- the scheme had FOUR spellings:

    swarmctl.USERS            derived from a prefix
    forge.ROLE_ACCOUNTS       four hand-written literals
    credentials.env_var_for   `username.startswith('swarm-')`, the prefix inlined
    workbench_cli             `username='swarm-agent'`, bare

DUPLICATION IS THE SMALL HALF. Two of them each declared ITSELF the sole authority -- `forge`'s
docstring said "this mapping is the ONLY thing that makes a process act as one role rather than
another", and `swarmctl`'s said the same of itself. A reader who believed either would edit one and
ship a fleet whose halves disagree about who they are, which fails as a 401 far from the edit.

WHY IT HAD GROWN THAT WAY, because the cause is structural rather than careless: `forge` is DRIVER
and `swarmctl` is ENTRY, so `forge` COULD NOT import the derivation without pointing up the arrow.
A fact placed above its lowest consumer gets re-spelled beneath it. `roles` is HOST for that reason.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from agent_swarm import credentials, forge, roles, swarmctl, workbench_cli
from agent_swarm.job import TEST_RUN

pytestmark = pytest.mark.unit

_PACKAGE = Path(roles.__file__).parent


def test_the_two_re_exports_are_the_SAME_OBJECT_not_equal_copies():
    """Identity, not equality. Two dicts that merely compare equal today are two copies, and the
    whole defect was two copies that agreed until one was edited."""
    assert forge.ROLE_ACCOUNTS is roles.ACCOUNTS
    assert swarmctl.USERS is roles.ACCOUNTS


def test_the_provisioning_table_covers_exactly_the_role_vocabulary():
    """`roles` owns the names; `swarmctl.ROLES` owns each one's units and scopes. Split on purpose
    -- units are one server's permission model -- so the SEAM is checked. A fifth role added to one
    and not the other would otherwise half-exist: provisioned with no account, or an account with no
    grants."""
    assert set(swarmctl.ROLES) == set(roles.ROLE_NAMES)
    assert set(swarmctl.TEAMS) == set(roles.ROLE_NAMES)


def test_no_module_spells_an_account_name_as_a_LITERAL():
    """The regression guard, and it scans SOURCE because no behavioural test can see the difference
    -- a re-inlined literal produces exactly the same string until the day the prefix changes.

    SCOPE, stated so the reader does not supply "everything": every module under the package,
    rejecting `swarm-<role>` inside any string the code can USE AS A VALUE. Discarded string
    statements (docstrings, including attribute docstrings) are exempt, for the same reason
    `test_this_package_names_no_specific_project` exempts them: prose about the scheme is not a
    second copy of it.
    """
    offenders: list[str] = []
    for path in sorted(_PACKAGE.glob('*.py')):
        if path.stem == 'roles':  # the owner is allowed to spell it; that is what owning it means
            continue
        tree = ast.parse(path.read_text(encoding='utf-8'))
        discarded = {
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)
        }
        offenders.extend(
            f'{path.name}:{node.lineno} {node.value!r}'
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node not in discarded
            and any(account in node.value for account in roles.ACCOUNTS.values())
        )
    assert not offenders, 'an account name is spelled outside `roles`:\n  ' + '\n  '.join(offenders)


def test_the_literal_scanner_actually_CATCHES_one():
    """The instrument first. The assertion above passes against a scanner whose comparison never
    matches -- a typo in the prefix, a walk missing `Constant` -- and would then certify the package
    clean forever."""
    tree = ast.parse("forge = Gitea(username='swarm-agent')\n")
    hits = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and any(account in node.value for account in roles.ACCOUNTS.values())
    ]
    assert hits, 'the scanner does not fire on the exact line it was written to catch'


def test_env_var_for_still_normalises_a_username_this_swarm_never_issued():
    """The behaviour `strip_prefix` had to preserve. `env_var_for` deliberately accepts ANY username
    so the env scheme has no hole; moving the prefix into `roles` must not turn that into a raise."""
    assert credentials.env_var_for('swarm-agent') == 'SWARM_TOKEN_AGENT'
    assert credentials.env_var_for('someone-else') == 'SWARM_TOKEN_SOMEONE_ELSE'


def test_role_of_says_NOT_OURS_rather_than_NOT_ALLOWED():
    """Three-valued by omission is the hazard this package has already been bitten by: a None
    collapsed into False turns "a stranger's account" into an accusation about permissions."""
    assert roles.role_of('swarm-verifier') == 'verifier'
    assert roles.role_of('mingyang') is None


def test_an_unknown_role_RAISES_rather_than_composing_a_plausible_name():
    """`account_for('agnet')` returning `swarm-agnet` would authenticate as nobody and surface as a
    401 nowhere near the typo."""
    with pytest.raises(KeyError):
        roles.account_for('agnet')


def test_the_workbench_builds_its_forge_as_the_agent_account():
    """The call site the fourth literal was in -- asserted through the REAL constructor, so this
    fails if the wiring is dropped rather than if the constant is renamed."""
    settings = workbench_cli.Settings(
        base_url='http://example.invalid',
        repo='org/repo',
        namespace='ns',
        owner='tester@box',
        capabilities=frozenset(),
        kind=TEST_RUN,
        lease_seconds=60.0,
    )
    assert workbench_cli.build_workbench(settings).store.forge.username == roles.ACCOUNTS['agent']
