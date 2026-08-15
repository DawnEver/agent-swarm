r"""A file holding live plaintext credentials is owner-only IN FACT, or it is deleted.

MEASURED 2026-08-10 on the fleet host, while generating a real enrolment bundle for a second
machine. `emit` wrote it with `os.open(..., S_IRUSR | S_IWUSR)` and printed "owner-readable only".
**On Windows the POSIX mode is essentially ignored**: NTFS gave the new file its parent directory's
INHERITED ACL, which granted `Authenticated Users` Modify and `Users` Read.

So every authenticated user on that box could modify a file containing four live role credentials,
and every user could read it -- underneath a line of output asserting the opposite. The declaration
was the only part that was owner-only, which is this project's dominant defect class landing on a
credential path.

WHY IT RAISES AND DELETES RATHER THAN WARNING. The entire purpose of this file is to cross a machine
boundary, and the operator chooses a transport on the belief that it is protected. A warning on a
file that still exists is the shape this repo names as forbidden -- diligence with the check absent.
Deleting it costs one re-run of `emit`; leaving it costs four credentials.

WHAT IS NOT CLAIMED: that this is airtight against an administrator. `BUILTIN\Administrators` and
`SYSTEM` can read anything on the box, and no ACL changes that. The threat this addresses is the
ORDINARY user on a shared workstation -- which is exactly what a fleet host is.
"""

from __future__ import annotations

import os
import stat
import subprocess

import pytest

from agent_swarm import credentials
from agent_swarm import swarmctl as _swarmctl


@pytest.fixture(scope='module')
def swarmctl():
    """The module under test, as an ORDINARY IMPORT.

    It used to be loaded by path with `importlib.util.spec_from_file_location`, because it lived in
    another project as a bare script that nothing could import. Here it is a module of this package,
    so the loader preamble is gone -- and with it a whole class of mistake, since a hand-rolled load
    can silently execute a DIFFERENT file from the one an import would resolve.
    """
    return _swarmctl


_PAYLOAD = {'scheme': 'http', 'host': 'h:9000', 'machine': 'M', 'credentials': {'swarm-agent': 'tok'}}


def _icacls(swarmctl, monkeypatch, *, others: str) -> list[list[str]]:
    """Stand in for `icacls`, both the write and the read-back, and record the argv."""
    calls: list[list[str]] = []

    def run(argv, **_kwargs):
        calls.append(list(argv))
        listing = f'bundle.json OWNER:(F)\n{others}' if len(argv) == 2 else 'Successfully processed 1 files.'
        return subprocess.CompletedProcess(argv, 0, stdout=listing, stderr='')

    monkeypatch.setattr(swarmctl.os, 'name', 'nt')
    monkeypatch.setenv('USERNAME', 'OWNER')
    # PATCHED WHERE `icacls` IS ACTUALLY RUN -- `credentials`, not `swarmctl`. This used to say
    # `swarmctl.subprocess` and worked only because both names bound the same module object, so the
    # sole thing keeping `import subprocess` alive in `swarmctl` was this test reaching through it.
    # When the Gitea CLI shell-out was retired 2026-08-15 that import went with it and five tests
    # here failed for a reason unrelated to anything they assert. Naming the real caller is what
    # makes the patch mean what it says.
    monkeypatch.setattr(credentials.subprocess, 'run', run)
    return calls


def test_a_file_that_stays_world_readable_is_DELETED(swarmctl, monkeypatch, tmp_path):
    """THE DEFECT ITSELF. The measured ACL granted Authenticated Users Modify on live credentials."""
    _icacls(swarmctl, monkeypatch, others='NT AUTHORITY\\Authenticated Users:(I)(M)')
    path = tmp_path / 'bundle.json'
    with pytest.raises(swarmctl.Fail, match='DELETED'):
        swarmctl.write_secret_file(path, _PAYLOAD)
    assert not path.exists(), 'a file holding credentials survived a failed lockdown'


def test_an_owner_only_file_is_kept(swarmctl, monkeypatch, tmp_path):
    """The discriminating half: a check that deleted everything would make `emit` unusable."""
    _icacls(swarmctl, monkeypatch, others='')
    path = tmp_path / 'bundle.json'
    swarmctl.write_secret_file(path, _PAYLOAD)
    assert path.exists()


def test_the_inherited_ACEs_are_DROPPED_not_merely_supplemented(swarmctl, monkeypatch, tmp_path):
    """`/inheritance:r` is the half that matters. Granting the owner full control while leaving the
    inherited entries in place changes nothing -- the measured ACL already gave the owner access.
    """
    calls = _icacls(swarmctl, monkeypatch, others='')
    swarmctl.write_secret_file(tmp_path / 'bundle.json', _PAYLOAD)
    assert any('/inheritance:r' in call for call in calls), calls


def test_the_permission_is_READ_BACK_not_assumed(swarmctl, monkeypatch, tmp_path):
    """The whole lesson: applying a permission and believing it is what failed. `icacls` must be
    invoked a second time, with no write arguments, to see what the file actually grants.
    """
    calls = _icacls(swarmctl, monkeypatch, others='')
    swarmctl.write_secret_file(tmp_path / 'bundle.json', _PAYLOAD)
    assert any(len(call) == 2 for call in calls), f'no read-back happened: {calls}'


def test_the_refusal_carries_no_credential(swarmctl, monkeypatch, tmp_path):
    """Project invariant: never log tokens. This message renders only when something went wrong,
    so it is never seen in a green run.
    """
    _icacls(swarmctl, monkeypatch, others='BUILTIN\\Users:(I)(RX)')
    with pytest.raises(swarmctl.Fail) as caught:
        swarmctl.write_secret_file(tmp_path / 'bundle.json', _PAYLOAD)
    assert 'tok' not in str(caught.value)


@pytest.mark.skipif(
    os.name == 'nt', reason='POSIX mode bits are not applicable on NTFS; the ACL is the mechanism there'
)
def test_on_posix_the_mode_really_is_owner_only(swarmctl, tmp_path):
    """The platform where `os.open`'s mode DOES work. Asserted rather than assumed, because the
    Windows fix must not have quietly become the only path.
    """
    path = tmp_path / 'bundle.json'
    swarmctl.write_secret_file(path, _PAYLOAD)
    assert not path.stat().st_mode & 0o077


def test_a_posix_file_that_is_group_readable_is_deleted(swarmctl, monkeypatch, tmp_path):
    """Forced, so the POSIX branch is exercised on every platform -- the fleet is about to be mixed
    and this branch would otherwise ship having run nowhere.
    """
    monkeypatch.setattr(swarmctl.os, 'name', 'posix')
    path = tmp_path / 'bundle.json'
    real_stat = swarmctl.Path.stat

    def leaky(self, *args, **kwargs):
        result = real_stat(self, *args, **kwargs)
        return os.stat_result((result.st_mode | stat.S_IRGRP, *tuple(result)[1:]))

    monkeypatch.setattr(swarmctl.Path, 'stat', leaky)
    with pytest.raises(swarmctl.Fail, match='group- or world-readable'):
        swarmctl.write_secret_file(path, _PAYLOAD)
    assert not path.exists()
