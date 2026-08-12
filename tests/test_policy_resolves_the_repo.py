"""`policy.resolve_repo` -- precedence and the origin cross-check, with no project named anywhere.

WHY THIS IS A PACKAGE CONCERN. `default_forge` carried one project's repository as a DEFAULT, and
deleting it made `repo` required -- correct, and it left consumers with nowhere to put the
resolution. The first consumer wrote 139 lines of it, none of which named that consumer. This is
that code, once, where the second consumer can find it.
"""

from __future__ import annotations

import pytest

from agent_swarm.policy import RepoDisagreesWithOrigin, RepoUndeclared, repo_from, resolve_repo

ENV = 'SWARM_REPO_FOR_TESTS'


def test_the_declaration_is_used_when_nothing_overrides_it():
    assert resolve_repo('owner/name', env_var=ENV) == 'owner/name'


def test_the_environment_wins(monkeypatch):
    """ONE override, named by the CONSUMER. A variable invented here would be a second spelling of
    a setting an operator has already set for their admin CLI.
    """
    monkeypatch.setenv(ENV, 'other/repo')
    assert resolve_repo('owner/name', env_var=ENV) == 'other/repo'


def test_the_environment_wins_even_when_NOTHING_is_declared(monkeypatch):
    """The override must not require a declaration to override. A box configured entirely by
    environment is a legitimate deployment, and demanding a file it does not have would be a
    precondition nobody stated.
    """
    monkeypatch.setenv(ENV, 'other/repo')
    assert resolve_repo(None, env_var=ENV) == 'other/repo'


@pytest.mark.parametrize('nothing', [None, ''])
def test_an_undeclared_repo_RAISES_rather_than_defaulting(nothing):
    """NEVER a default. That is the exact defect `DEFAULT_REPO` was: it works, so nobody finds out,
    until the day the fleet writes to somebody else's issue tracker.
    """
    with pytest.raises(RepoUndeclared, match='no default'):
        resolve_repo(nothing, env_var=ENV)


def test_a_declaration_disagreeing_with_origin_is_REFUSED():
    """Work claimed in one repository and objects fetched from another. The symptom is an
    unfetchable candidate, which reads as a retention or network problem -- so it is refused here.
    """
    with pytest.raises(RepoDisagreesWithOrigin, match='origin points at'):
        resolve_repo('owner/name', env_var=ENV, origin='http://forge:9000/someone/else.git')


def test_an_AGREEING_origin_is_not_an_obstacle():
    """The CONTROL. A check never shown to say yes is indistinguishable from one that always says
    no, and this one is on the path of every scheduling call.
    """
    assert resolve_repo('owner/name', env_var=ENV, origin='ssh://git@forge:22/owner/name.git') == 'owner/name'


def test_no_origin_at_all_is_not_an_error():
    """A lane worktree or an air-gapped box legitimately has no remote. Refusing to schedule because
    git could not be asked would be unknown read as wrong.
    """
    assert resolve_repo('owner/name', env_var=ENV, root=None) == 'owner/name'


def test_repo_from_reads_the_shape_a_TOML_file_becomes():
    assert repo_from({'forge': {'repo': 'owner/name'}}, env_var=ENV) == 'owner/name'


def test_repo_from_on_a_policy_declaring_no_forge_block_raises():
    with pytest.raises(RepoUndeclared):
        repo_from({'schedule': {'poll_seconds': 45}}, env_var=ENV)
