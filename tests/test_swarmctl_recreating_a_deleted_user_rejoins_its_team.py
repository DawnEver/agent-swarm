"""Deleting a role user and re-provisioning must put it back in its team.

THE SITUATION, live on 2026-08-10: four role accounts had accumulated tokens whose plaintext had
leaked, and the cleanest cure was to delete the ACCOUNTS -- which takes every token with them. That
leaves a half-state nothing else produces: the four teams still exist, with the right units and the
right repo attached, and NO MEMBERS.

WHY IT IS WORTH A TEST RATHER THAN A GLANCE. Every visible signal says healthy. `provision` prints
`created` for the user, `ensure_team` reports the team unchanged because its units really are
unchanged, and `onboard` says `already attached` because the repo really is attached. A version that
only re-added members when it had just created the TEAM would print exactly the same lines while
leaving four empty teams -- and the first thing to notice would be an agent unable to push, which
reads as a credential problem.

Delete-and-recreate is also the STANDARD cure for a leaked credential, so this is not an exotic
path; it is the recovery path, and the recovery path is the one nobody exercises until they need it.
"""

from __future__ import annotations

import argparse

import pytest

from agent_swarm import swarmctl as _swarmctl

pytestmark = pytest.mark.unit


@pytest.fixture(scope='module')
def swarmctl():
    """The module under test, as an ORDINARY IMPORT.

    It used to be loaded by path with `importlib.util.spec_from_file_location`, because it lived in
    another project as a bare script that nothing could import. Here it is a module of this package,
    so the loader preamble is gone -- and with it a whole class of mistake, since a hand-rolled load
    can silently execute a DIFFERENT file from the one an import would resolve.
    """
    return _swarmctl


class _Server:
    """Users, teams and membership -- and teams SURVIVE a user deletion, as Gitea's do."""

    def __init__(self, swarmctl) -> None:
        self.users: set[str] = set()
        self.teams: dict[str, int] = {}
        self.members: dict[int, set[str]] = {}
        self._swarmctl = swarmctl

    def provision(self):
        self._swarmctl.cmd_provision(_Provider(self), argparse.Namespace())

    def delete_users(self):
        """Exactly what the operator did: the accounts go, the teams stay."""
        self.users.clear()
        for team_id in self.members:
            self.members[team_id] = set()


class _Provider:
    name = 'gitea'

    def __init__(self, server: _Server) -> None:
        self._server = server

    def user_exists(self, username: str) -> bool:
        return username in self._server.users

    def create_user(self, username: str) -> None:
        self._server.users.add(username)

    def ensure_team(self, team_name: str, _units) -> tuple[int, str]:
        if team_name not in self._server.teams:
            team_id = len(self._server.teams) + 1
            self._server.teams[team_name] = team_id
            self._server.members[team_id] = set()
            return team_id, 'created'
        return self._server.teams[team_name], 'units up to date'

    def team_members(self, team_id: int) -> list[str]:
        return sorted(self._server.members[team_id])

    def add_member(self, team_id: int, username: str) -> None:
        self._server.members[team_id].add(username)


def test_a_recreated_user_is_put_back_in_its_team(swarmctl, capsys):
    """THE RECOVERY PATH. Users deleted, teams intact, and provision must repair the membership."""
    server = _Server(swarmctl)
    server.provision()
    server.delete_users()
    capsys.readouterr()

    server.provision()

    for role, (team_name, _units, _scopes) in swarmctl.ROLES.items():
        team_id = server.teams[team_name]
        assert swarmctl.USERS[role] in server.members[team_id], f'{team_name} is empty after re-provisioning'


def test_the_repair_is_REPORTED_not_silent(swarmctl, capsys):
    """An operator recovering from a leak needs to see the membership come back. Silence here is
    indistinguishable from four teams left empty.
    """
    server = _Server(swarmctl)
    server.provision()
    server.delete_users()
    capsys.readouterr()

    server.provision()

    assert capsys.readouterr().out.count('member added') == len(swarmctl.ROLES)


def test_a_second_provision_adds_nobody_twice(swarmctl, capsys):
    """The discriminating half: a repair that ran unconditionally would be a write on every run, and
    `already present` would stop meaning anything.
    """
    server = _Server(swarmctl)
    server.provision()
    capsys.readouterr()

    server.provision()

    assert 'member added' not in capsys.readouterr().out
    for team_id, members in server.members.items():
        assert len(members) == 1, f'team {team_id} has {members}'
