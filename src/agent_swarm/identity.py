"""WHO THIS MACHINE IS, stably, across reboots -- and why a hostname was not enough.

WHAT THIS IS. The runner-identity half of motronics' `scripts/ci/ci_tick.py`, extracted under the
migration criterion (does the CODE name a project noun?). It names none: a hostname, a platform
identifier and a hash. Its one project fact -- the checkout path that seasons the FALLBACK salt --
arrives as a required argument.

THE BARE HOSTNAME WAS NOT UNIQUE, and identity is load-bearing in two places that fail SILENTLY
when it collides:

* a heartbeat namespace where each runner deletes every stamp but the one it just wrote, so a
  second box answering to the same name erases the first one's beat every tick, and each reads as
  dead to the fleet while both are alive;
* a claim release keyed on ``holder == runner``, where a free lock beside an own-claim is read as
  proof the claimer died -- so two same-named boxes each treat the OTHER's live claim as their own
  abandoned one, and both run the job.

Neither is visible on a one-runner fleet, and the second is DUPLICATE EXECUTION rather than a
crash. It was found as an incident, not by review.

SALTED RATHER THAN CHECKED, and this is the design decision worth keeping. "Detect the collision
and warn" cannot work here: only the colliding boxes could notice, they see each other solely
through the refs the collision corrupts, and the warning lands on a box nobody is watching. A salt
makes the collision impossible instead of detectable.

AND THE SALT IS NEVER RANDOM. A per-process salt would make every tick a new runner -- a liveness
count that grows without bound, a capability union full of ghosts, and a "delete every stamp that
is not mine" that never cleans any of them up. That is a worse failure than the one being fixed, so
even the fallback is derived from stable inputs.

THE PLATFORM COVERAGE IS THE POINT OF :func:`machine_uuid`, because the fallback is stable but NOT
UNIQUE: two nodes provisioned identically -- same user, same checkout path -- hash to the same salt
AND share a hostname prefix, which re-opens the very collision the salt exists to close. Every
platform that can answer is therefore asked before the fallback is reached.
"""

from __future__ import annotations

import hashlib
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

#: Resolved once at import: ruff S607 refuses a partial executable path, and resolving it means a
#: box without the tool fails at the first call with a name rather than at every call.
_IOREG = shutil.which('ioreg') or 'ioreg'

#: RESOLVED AT IMPORT so :func:`machine_uuid` needs no lazy import inside the function -- a lazy
#: import there would want a lint suppression, and a suppression is a defect deferred. `None` on
#: every non-Windows box, where the file legs answer instead.
try:
    import winreg as _winreg
except ImportError:  # pragma: no cover -- every non-Windows box
    _winreg = None

_MACHINE_ID_FILES = (Path('/etc/machine-id'), Path('/var/lib/dbus/machine-id'))

_IOPLATFORM_UUID = re.compile(r'"IOPlatformUUID"\s*=\s*"([^"]+)"')

SALT_LENGTH = 8


def machine_uuid() -> str | None:
    """A machine identifier that survives reboots, or ``None`` if this platform will not say.

    READ, NEVER GENERATED. A freshly generated id would be perfectly unique and would make every
    reboot a new fleet member, which is the failure mode the salt exists to avoid, reintroduced by
    the mechanism meant to prevent it.

    THREE LEGS, one per platform family: Linux and most BSDs keep `/etc/machine-id` (or dbus's
    copy), macOS keeps `IOPlatformUUID` in the IORegistry, Windows keeps `MachineGuid` in the
    registry. The macOS leg is the one that was MISSING originally, and its absence is what made
    the identical-provisioning case reachable on real hardware.
    """
    for path in _MACHINE_ID_FILES:
        try:
            text = path.read_text(encoding='utf-8').strip()
        except OSError:
            continue
        if text:
            return text
    if sys.platform == 'darwin':
        try:
            out = subprocess.run(
                [_IOREG, '-rd1', '-c', 'IOPlatformExpertDevice'],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            return None
        match = _IOPLATFORM_UUID.search(out)
        return match.group(1) if match else None
    if _winreg is not None:
        try:
            with _winreg.OpenKey(_winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\Microsoft\Cryptography') as key:
                return str(_winreg.QueryValueEx(key, 'MachineGuid')[0])
        except OSError:
            return None
    return None


def runner_salt(root: Path) -> str:
    """A short, STABLE discriminator for this machine. Never random.

    `root` IS REQUIRED and is the only project fact here: it seasons the FALLBACK, which is reached
    only when no platform will name the machine. Two checkouts on one box are two runners, which is
    what a caller running several fleets off one machine needs -- and a default would silently make
    them one, which is the collision this module exists to prevent, arriving through its own door.
    """
    raw = machine_uuid() or f'{Path.home()}|{sys.platform}|{root}'
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()[:SALT_LENGTH]


def runner_id(root: Path) -> str:
    """The runner's fleet identity: ``<hostname>-<salt>``.

    THE HOSTNAME STAYS IN FRONT because this string is operator-facing -- it appears in liveness
    reports and heartbeat lines -- and a bare hash would make that output unreadable. The salt is
    what makes it correct; the hostname is what makes it usable, and dropping either has been tried.
    """
    return f'{platform.node()}-{runner_salt(root)}'
