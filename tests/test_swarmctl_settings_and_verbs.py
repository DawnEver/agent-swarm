"""swarmctl's argument surface: where a setting comes from, and what a bare verb means.

WHY THIS FILE EXISTS. The verb-to-flag mapping used to live in a `.cmd` wrapper, and a smoke run of
that wrapper found three defects that no amount of reading had:

* the admin-present guard NEVER FIRED -- it shelled out to `find`, which on any box with Git for
  Windows ahead on PATH is POSIX find, so the test silently became an error and the guard passed
  unconditionally. A guard that cannot fail is the file's own subject matter.
* `%~dp0` was read AFTER `shift`, which renumbers `%0` too, so the script looked for itself inside
  whatever directory the user had named.
* a value containing a drive letter or a space reached Python torn in half.

The cross-platform requirement then removed the wrapper's right to hold any of this: a `sh` port
would be the same logic written twice, in two shells, with only one of them ever tested. So the
parsing lives in Python and THIS is the test the wrappers no longer need.

WHAT IS ASSERTED, and why each one is a defect if it flips:

* PRECEDENCE built-in < config < environment < command line. Get this backwards and a machine that
  once ran `config` can never be pointed anywhere else for a single run.
* `config` persists only what was TYPED. Persisting resolved defaults would freeze this run's
  hostname and today's branch into the file, where they read as deliberate choices.
* NO SECRET AND NO ONE-SHOT FLAG IS PERSISTABLE. `--confirm` remembered across runs is a loaded gun;
  a token in a plaintext config file is the invariant this project states outright.
* `--machine` defaults to the hostname, so "was it typed" is a DIFFERENT question from "does it have
  a value" -- a bare `revoke` must not quietly mean "revoke this machine".
* the positional shorthand fills the same field the long flag does, for every verb that has one.
* `revoke all` does NOT supply its own `--confirm`: a shorthand that confirms itself confirms
  nothing.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from agent_swarm import swarmctl as _swarmctl

#: This module's own source, read by the two assertions below that are about what the code SAYS.
_SOURCE = Path(_swarmctl.__file__)


@pytest.fixture(scope='module')
def swarmctl():
    """The module under test, as an ORDINARY IMPORT.

    It used to be loaded by path with `importlib.util.spec_from_file_location`, because it lived in
    another project as a bare script that nothing could import. Here it is a module of this package,
    so the loader preamble is gone -- and with it a whole class of mistake, since a hand-rolled load
    can silently execute a DIFFERENT file from the one an import would resolve.
    """
    return _swarmctl


@pytest.fixture
def isolated(swarmctl, monkeypatch, tmp_path):
    """Point the config at a temp dir on every platform, and clear inherited SWARM_* variables.

    A test that reads the developer's real config would pass or fail according to whose box it ran
    on -- the class of green this repo treats as no information at all.
    """
    monkeypatch.setattr(swarmctl, 'config_path', lambda: str(tmp_path / 'config.json'))
    for key in [k for k in os.environ if k.startswith('SWARM_')]:
        monkeypatch.delenv(key, raising=False)
    return swarmctl


def parse(swarmctl, monkeypatch, argv: list[str]):
    """Run swarmctl's parsing exactly as `main` does, and stop before any network call."""
    monkeypatch.setattr(sys, 'argv', ['swarmctl', *argv])
    return swarmctl.parse_argv(argv)


# --------------------------------------------------------------------------- precedence


def test_a_stored_setting_is_used_when_nothing_else_says_otherwise(isolated, monkeypatch):
    isolated.save_config({'org': 'FromFile', 'base_url': 'http://file:9000'})
    args = parse(isolated, monkeypatch, ['list'])
    assert args.org == 'FromFile'
    assert args.base_url == 'http://file:9000'


def test_the_environment_beats_the_file(isolated, monkeypatch):
    isolated.save_config({'org': 'FromFile'})
    monkeypatch.setenv('SWARM_ORG', 'FromEnv')
    assert parse(isolated, monkeypatch, ['list']).org == 'FromEnv'


def test_the_command_line_beats_the_environment(isolated, monkeypatch):
    isolated.save_config({'org': 'FromFile'})
    monkeypatch.setenv('SWARM_ORG', 'FromEnv')
    assert parse(isolated, monkeypatch, ['list', '--org', 'FromFlag']).org == 'FromFlag'


def test_a_built_in_default_survives_an_empty_config(isolated, monkeypatch):
    assert parse(isolated, monkeypatch, ['list']).branch == 'main'


# --------------------------------------------------------------------------- config verb


def test_config_persists_only_what_was_typed(isolated, monkeypatch):
    args = parse(isolated, monkeypatch, ['config', '--org', 'Typed'])
    isolated.cmd_config(None, args)
    stored = isolated.load_config()
    assert stored == {'org': 'Typed'}, 'a resolved default must not be written as if it were chosen'


def test_config_takes_the_admin_user_as_its_positional(isolated, monkeypatch):
    args = parse(isolated, monkeypatch, ['config', 'MingyangBao'])
    isolated.cmd_config(None, args)
    assert isolated.load_config() == {'admin_user': 'MingyangBao'}


def test_config_updates_rather_than_replaces(isolated, monkeypatch):
    isolated.cmd_config(None, parse(isolated, monkeypatch, ['config', 'admin1']))
    isolated.cmd_config(None, parse(isolated, monkeypatch, ['config', '--org', 'Later']))
    assert isolated.load_config() == {'admin_user': 'admin1', 'org': 'Later'}


def test_config_rewrites_a_key_instead_of_keeping_the_old_value(isolated, monkeypatch):
    isolated.cmd_config(None, parse(isolated, monkeypatch, ['config', 'admin1']))
    isolated.cmd_config(None, parse(isolated, monkeypatch, ['config', 'admin2']))
    assert isolated.load_config()['admin_user'] == 'admin2'


@pytest.mark.parametrize(
    'key', ['confirm', 'protect', 'all_tokens', 'unmanaged', 'bundle', 'out', 'token_name', 'erase_local', 'machine']
)
def test_no_one_shot_or_destructive_flag_can_be_persisted(swarmctl, key):
    """A remembered `--confirm` is a loaded gun; a remembered `--machine` silently retargets every
    later run at a box that may no longer exist.
    """
    assert key not in swarmctl.CONFIG_KEYS


def test_no_config_key_could_hold_a_secret(swarmctl):
    """The file is plaintext. Tokens belong in the credential store, which is the whole reason
    `enroll` pipes them into `git credential approve` instead of writing them anywhere.
    """
    forbidden = ('token', 'password', 'secret', 'key')
    leaks = [k for k in swarmctl.CONFIG_KEYS if any(word in k for word in forbidden)]
    assert not leaks, f'these config keys could hold a credential: {leaks}'


def test_the_config_file_is_not_next_to_the_script(swarmctl):
    """It travels per MACHINE, not per checkout: clone the repo on a second box and you must not
    inherit the first box's admin account and Gitea path.
    """
    assert str(_SOURCE.parent) not in swarmctl.config_path()


def test_an_unreadable_config_is_loud(isolated):
    with Path(isolated.config_path()).open('w', encoding='utf-8') as handle:
        handle.write('{ not json')
    with pytest.raises(isolated.Fail, match='unreadable'):
        isolated.load_config()


def test_an_unknown_key_in_the_file_is_ignored(isolated):
    with Path(isolated.config_path()).open('w', encoding='utf-8') as handle:
        json.dump({'org': 'Kept', 'confirm': 'DESTROY'}, handle)
    assert isolated.load_config() == {'org': 'Kept'}


# --------------------------------------------------------------------------- positionals


@pytest.mark.parametrize(
    ('verb', 'value', 'field'),
    [
        ('onboard', 'Org/Repo', 'repo'),
        ('verify', 'Org/Repo', 'repo'),
        ('emit', 'WS1', 'machine'),
        ('admin-emit', 'WS1', 'machine'),
        ('consume', 'bundle.json', 'bundle'),
        ('destroy', 'DESTROY', 'confirm'),
    ],
)
def test_the_positional_fills_the_same_field_as_the_long_flag(isolated, monkeypatch, verb, value, field):
    assert getattr(parse(isolated, monkeypatch, [verb, value]), field) == value


@pytest.mark.parametrize(
    ('verb', 'value', 'field'),
    [
        ('onboard', 'Org/Repo', 'repo'),
        ('emit', 'WS1', 'machine'),
        ('consume', 'bundle.json', 'bundle'),
    ],
)
def test_the_long_flag_still_works(isolated, monkeypatch, verb, value, field):
    flag = '--' + field.replace('_', '-')
    assert getattr(parse(isolated, monkeypatch, [verb, flag, value]), field) == value


def test_a_value_with_spaces_and_a_drive_letter_survives_intact(isolated, monkeypatch):
    """The wrapper used to tear this in half at the colon, producing `--bundle C` plus a stray path."""
    path = r'C:\dir with space\bundle.json'
    assert parse(isolated, monkeypatch, ['consume', path]).bundle == path


# --------------------------------------------------------------------------- revoke selectors


def test_revoke_with_a_name_selects_that_machine_only(isolated, monkeypatch):
    args = parse(isolated, monkeypatch, ['revoke', 'WS1'])
    assert (args.machine, args.machine_given) == ('WS1', True)
    assert not args.unmanaged and not args.all_tokens


def test_revoke_unmanaged_is_a_selector_not_a_machine(isolated, monkeypatch):
    args = parse(isolated, monkeypatch, ['revoke', 'unmanaged'])
    assert args.unmanaged and not args.machine_given


def test_revoke_all_does_not_confirm_itself(isolated, monkeypatch):
    """The positional says WHAT to revoke. If it also supplied `--confirm REVOKE-ALL`, the
    confirmation would be produced by the same keystroke it is supposed to guard.
    """
    args = parse(isolated, monkeypatch, ['revoke', 'all'])
    assert args.all_tokens
    assert args.confirm != 'REVOKE-ALL'


def test_a_bare_revoke_selects_nothing(isolated, monkeypatch):
    """`--machine` defaults to this hostname, so "has a value" must not be read as "was chosen"."""
    args = parse(isolated, monkeypatch, ['revoke'])
    assert not args.machine_given and not args.unmanaged and not args.all_tokens


def test_machine_given_is_true_when_the_flag_is_typed(isolated, monkeypatch):
    assert parse(isolated, monkeypatch, ['revoke', '--machine', 'WS1']).machine_given


# --------------------------------------------------------------------------- protect


def test_p_is_the_short_form_of_protect(isolated, monkeypatch):
    assert parse(isolated, monkeypatch, ['onboard', 'Org/Repo', '-p']).protect


def test_protect_is_off_unless_asked(isolated, monkeypatch):
    """Turning it on before anything writes a commit status freezes the branch behind a check
    nothing produces.
    """
    assert not parse(isolated, monkeypatch, ['onboard', 'Org/Repo']).protect
