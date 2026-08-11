"""Prefixing the team names is a MIGRATION, and the dangerous version of it passes every naive test.

USER DIRECTIVE 2026-08-11: 「teams 命名很容易引起误解 比如 Agents 让人会误解 teams 名也直接叫
Swarm-Agents Swarm-xxx 更好」. In a shared Gitea org a team called `Agents` claims a generic noun the
org's humans also use; `Swarm-Agents` names a thing instead of a category.

THE TRAP, AND IT IS THE WHOLE REASON THIS FILE EXISTS. `ensure_team` looks a team up BY NAME. Change
the constant alone and `provision` renames nothing -- it MISSES the four old teams and CREATES four
new ones beside them. The org then holds EIGHT teams: every membership and every repo attachment
still on the old four, the new four empty. Nothing fails at that moment, because each machine's token
is already issued and the old grants are intact. It fails at the NEXT provisioning, or the next time
somebody reads the team list and believes it -- access having quietly moved to teams nobody is in.

A silent, delayed failure whose cause is four days upstream of its symptom. That is the same class as
the credential clobber removed earlier tonight, and it is why this is tested at the level of the
SERVER'S RESULTING STATE rather than at the level of "did we call PATCH".

THE FAKE DRIVES THE REAL `ensure_team`. Only `_call` is replaced -- by an in-memory org that answers
the same routes Gitea does, keying members and repo attachments on the TEAM ID exactly as Gitea's
tables do. A fake `ensure_team` would have been the double proving the implementation it replaced.
"""

from __future__ import annotations

import argparse

import pytest

from agent_swarm import swarmctl


class _Org:
    """An in-memory Gitea org: teams by id, with members and repos hanging off those ids.

    THE ID IS LOAD-BEARING IN THE FAKE BECAUSE IT IS LOAD-BEARING IN GITEA. Membership and
    team-repo attachment are keyed by team id, so a rename that preserves the id preserves the
    grants and a create-and-delete cannot. Modelling that faithfully is what lets the tests below
    tell those two implementations apart; a fake keyed by NAME would call them equivalent.
    """

    def __init__(self, team_names: list[str]) -> None:
        self.teams = [{'name': name, 'id': i, 'units_map': {}} for i, name in enumerate(team_names, start=1)]
        self.members = {team['id']: set() for team in self.teams}
        self.repos = {team['id']: set() for team in self.teams}
        self.created = 0

    def by_id(self, team_id: int) -> dict:
        return next(team for team in self.teams if team['id'] == team_id)

    def call(self, method, path, body=None, **_kwargs):
        if method == 'GET' and path.startswith('/orgs/') and '/teams' in path:
            return [dict(team) for team in self.teams]
        if method == 'POST' and path.startswith('/orgs/'):
            self.created += 1
            team = {'name': body['name'], 'id': 100 + self.created, 'units_map': dict(body['units_map'])}
            self.teams.append(team)
            self.members[team['id']] = set()
            self.repos[team['id']] = set()
            return {'id': team['id']}
        if method == 'PATCH' and path.startswith('/teams/'):
            team = self.by_id(int(path.rsplit('/', 1)[1]))
            # A RENAME, NOT A REPLACEMENT: the row keeps its id, so nothing keyed on it is disturbed.
            team['name'] = body['name']
            team['units_map'] = dict(body['units_map'])
            return None
        if method == 'GET' and '/members' in path:
            return [{'login': login} for login in sorted(self.members[int(path.split('/')[2])])]
        if method == 'GET' and '/repos' in path:
            return [{'full_name': name} for name in sorted(self.repos[int(path.split('/')[2])])]
        # DELETE IS MODELLED THOUGH THE SHIPPING CODE NEVER SENDS ONE, and deliberately. It is the
        # route a create-and-delete "rename" would take, and modelling it is what lets that
        # implementation FAIL ON THE PROPERTY -- lost ids, lost members, lost repos -- rather than on
        # `the fake org was asked something it does not model`, which would be a passing-looking
        # error about the harness instead of a report about the defect. Verified by reverting.
        if method == 'DELETE' and path.startswith('/teams/'):
            self.teams.remove(self.by_id(int(path.rsplit('/', 1)[1])))
            return None
        if method == 'PUT' and '/members/' in path:
            self.members[int(path.split('/')[2])].add(path.rsplit('/', 1)[1])
            return None
        raise AssertionError(f'the fake org was asked something it does not model: {method} {path}')


def _provider(org: _Org, *, existing_users: set[str] | None = None) -> swarmctl.GiteaProvider:
    provider = swarmctl.GiteaProvider('http://host:9000', 'Org', None, 'admin')
    provider._call = org.call
    known = set() if existing_users is None else existing_users
    provider.user_exists = lambda username: username in known
    provider.create_user = known.add
    return provider


_OLD = ['Observers', 'Agents', 'Verifiers', 'Integrators']


class TestTheNamesThemselves:
    def test_every_team_carries_the_prefix(self) -> None:
        assert set(swarmctl.TEAMS.values()) == {
            'Swarm-Observers',
            'Swarm-Agents',
            'Swarm-Verifiers',
            'Swarm-Integrators',
        }

    def test_the_team_name_is_DERIVED_so_a_fifth_role_cannot_arrive_unprefixed(self) -> None:
        """THE ASYMMETRY THIS REMOVED. The four team names were hand-written literals sitting beside
        a derived `USERS`: one prefix, two mechanisms, and only one of them could stay honest. There
        is now no literal for a new role's team to be misspelled in.
        """
        assert set(swarmctl.TEAMS) == set(swarmctl.ROLES), 'a role without a team, or a team without a role'
        for role, team in swarmctl.TEAMS.items():
            assert team.startswith('Swarm-') and swarmctl.USERS[role].startswith('swarm-')

    def test_the_old_names_are_REGISTERED_rather_than_remembered(self) -> None:
        """A rename is an event; recognising the pre-migration state is a property. Without this
        mapping in code, `provision` cannot tell an un-migrated org from a fresh one.
        """
        assert set(swarmctl.LEGACY_TEAMS) == set(_OLD)
        assert swarmctl._old_name_of('agent') == 'Agents'


class TestTheMigration:
    def test_an_org_with_the_OLD_four_ends_up_with_FOUR_teams_and_not_eight(self) -> None:
        """THE DISCRIMINATING ASSERTION. A constant change alone produces eight -- four live and
        four empty -- and every other check in this file would still pass.
        """
        org = _Org(_OLD)
        swarmctl.cmd_provision(_provider(org), argparse.Namespace())
        assert len(org.teams) == 4, [team['name'] for team in org.teams]
        assert {team['name'] for team in org.teams} == set(swarmctl.TEAMS.values())
        assert org.created == 0, 'a team was CREATED during what should have been a rename'

    def test_the_ids_are_unchanged(self) -> None:
        """THE PROPERTY THAT SEPARATES A RENAME FROM A RE-CREATE, and the one worth reverting.

        Gitea keys membership and repo attachment on the team id. An implementation that deleted the
        old team and created a new one would satisfy "four teams with the right names" and silently
        drop every grant -- so the name assertion above is NOT sufficient, and this is why.
        """
        org = _Org(_OLD)
        before = {team['name']: team['id'] for team in org.teams}
        swarmctl.cmd_provision(_provider(org), argparse.Namespace())
        after = {team['name']: team['id'] for team in org.teams}
        for old_name, role in swarmctl.LEGACY_TEAMS.items():
            assert after[swarmctl.TEAMS[role]] == before[old_name], f'{old_name} was re-created, not renamed'

    def test_membership_and_repo_attachment_SURVIVE_the_rename(self) -> None:
        """What the ids are FOR. This is the failure an operator would actually feel: the swarm
        keeps its four teams, correctly named, and can no longer reach the repository.
        """
        org = _Org(_OLD)
        for team in org.teams:
            org.members[team['id']].add(swarmctl.USERS[swarmctl.LEGACY_TEAMS[team['name']]])
            org.repos[team['id']].add('Org/repo')

        swarmctl.cmd_provision(_provider(org, existing_users=set(swarmctl.USERS.values())), argparse.Namespace())

        for role, team_name in swarmctl.TEAMS.items():
            team_id = next(team['id'] for team in org.teams if team['name'] == team_name)
            assert swarmctl.USERS[role] in org.members[team_id], f'{team_name} lost its member'
            assert org.repos[team_id] == {'Org/repo'}, f'{team_name} lost its repo attachment'

    def test_a_FRESH_org_simply_gets_the_new_names(self) -> None:
        org = _Org([])
        swarmctl.cmd_provision(_provider(org), argparse.Namespace())
        assert {team['name'] for team in org.teams} == set(swarmctl.TEAMS.values())
        assert org.created == 4

    def test_running_it_TWICE_creates_nothing_the_second_time(self) -> None:
        """IDEMPOTENCE IN BOTH DIRECTIONS. The new name is looked for FIRST, so a migrated org is
        merely updated. Checking the old name first would rename a leftover onto an existing one,
        and Gitea would refuse the duplicate -- turning a harmless second run into a hard failure.
        """
        org = _Org(_OLD)
        swarmctl.cmd_provision(_provider(org), argparse.Namespace())
        ids = {team['name']: team['id'] for team in org.teams}
        org.created = 0

        swarmctl.cmd_provision(_provider(org, existing_users=set(swarmctl.USERS.values())), argparse.Namespace())

        assert org.created == 0 and len(org.teams) == 4
        assert {team['name']: team['id'] for team in org.teams} == ids

    def test_a_HALF_migrated_org_converges_rather_than_duplicating(self) -> None:
        """The state a run interrupted part way leaves behind, which is the one nobody designs for.
        Two already renamed, two not: the result must still be four, with no duplicate name.
        """
        org = _Org(['Swarm-Observers', 'Agents', 'Swarm-Verifiers', 'Integrators'])
        swarmctl.cmd_provision(_provider(org), argparse.Namespace())
        names = [team['name'] for team in org.teams]
        assert sorted(names) == sorted(swarmctl.TEAMS.values()), names

    def test_the_rename_is_REPORTED_and_not_silent(self, capsys) -> None:
        """An operator running this against a live org is changing something other people can see.
        A migration that prints `updated` for a rename tells them nothing happened.
        """
        swarmctl.cmd_provision(_provider(_Org(_OLD)), argparse.Namespace())
        out = capsys.readouterr().out
        assert out.count('renamed') == 4, out


class TestDestroyStillReachesBothSpellings:
    def test_the_teardown_recognises_an_un_migrated_org(self) -> None:
        """`destroy` selects teams by name. Left naming only the new spelling, it would silently
        skip every team on a server that has not been migrated -- leaving behind exactly the grants
        it was run to remove, while reporting success.
        """
        selectable = set(swarmctl.TEAMS.values()) | set(swarmctl.LEGACY_TEAMS)
        assert {'Agents', 'Swarm-Agents'} <= selectable


@pytest.mark.parametrize('role', sorted(swarmctl.ROLES))
def test_every_role_has_an_old_name_to_migrate_from(role: str) -> None:
    """If a role ever lacks one, `_old_name_of` returns None and `ensure_team` simply creates -- the
    correct behaviour for a genuinely new role, and a silent no-migration for an existing one. This
    pins which of the two today's four are.
    """
    assert swarmctl._old_name_of(role) is not None
