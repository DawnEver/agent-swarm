"""Why an install is about to fail, before uv gets its turn -- plus the `--` argv split it needs.

EXTRACTED FROM motronics' `scripts/repo/install.py`, 2026-08-12. What stayed behind is that
project's: WHICH packages it installs, WHERE its manifest is, and the whole-box lock it takes around
the install. What is here reaches the outside world -- `git ls-remote` and `git credential fill` --
which is why this sits in the DRIVER layer.

DRIVER, AND THE REQUIREMENTS ARRIVE PARSED. `preflight` takes a list of requirement STRINGS, exactly
as `policy` takes a parsed mapping: a consumer reading TOML, JSON or a database reaches the same
code, and this module never reaches for a manifest path it was not told about.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from urllib.parse import urlsplit

#: RESOLVED ON PATH, not spelled bare: it consults the same PATH the developer uses, so nothing is
#: pinned to one install and a machine without the tool fails with a clear name.
_UV = shutil.which('uv') or 'uv'
_GIT = shutil.which('git') or 'git'

#: A CEILING ON THE PRE-FLIGHT, per probe. Its failure mode is not being wrong, it is being SLOW: a
#: check that adds a minute to every working install is a second failure mode dressed as a guard.
PROBE_TIMEOUT_S = 8.0

#: Belt and braces against an INTERACTIVE credential helper. `GIT_TERMINAL_PROMPT=0` suppresses the
#: terminal prompt and NOT the GUI one -- measured 2026-08-11, a helper raised a window and the
#: command hung until it was killed. These ask every helper we know of to stay silent; the timeout is
#: what actually holds, because an unknown helper will not read our variables.
_NON_INTERACTIVE = {
    'GIT_TERMINAL_PROMPT': '0',
    'GCM_INTERACTIVE': 'never',
    'GCM_PROVIDER': 'generic',
}


def uv_install(args: list[str]) -> int:
    """`uv pip install <args>`, inheriting stdio. Returns the child's exit code."""
    return subprocess.run([_UV, 'pip', 'install', *args], check=False).returncode


def split_passthrough(argv: list[str]) -> tuple[list[str], list[str]]:
    """`(our flags, what the installer gets)`. Everything after a bare ``--`` is theirs, ``--`` EATEN.

    MEASURED 2026-08-11, and this function exists because of it. The previous version took
    `nargs=argparse.REMAINDER`, which produced three ways to fail and no way to succeed: a leading
    `-e` was an "unrecognized argument", and `--` survived into the child as a package name. Three
    runs, all exit 2, and uv never reached dependency resolution once -- so the canonical install was
    INEXPRESSIBLE through the locked path, which guarantees everybody types the unlocked command
    instead. `REMAINDER` only starts capturing at the first thing argparse does not recognise;
    splitting on `--` ourselves is unambiguous in both directions and needs no argparse feature.
    """
    if '--' not in argv:
        return argv, []
    cut = argv.index('--')
    return argv[:cut], argv[cut + 1 :]


def git_url(requirement: str) -> str | None:
    """The plain URL inside a `name @ git+<url>[@rev]` requirement, or `None` if it is not one."""
    _, separator, target = requirement.partition('@')
    target = target.strip()
    if not separator or not target.startswith('git+'):
        return None
    url = target[len('git+') :].split('#')[0].strip()
    # A trailing `@<rev>` is part of the REQUIREMENT, not of the URL that `ls-remote` takes. It is
    # only a rev if it follows the last `/`; the `@` in a `user@host` userinfo prefix never does.
    head, separator, rev = url.rpartition('@')
    return head if separator and '/' not in rev else url


def host_of(url: str) -> str | None:
    """`host:port`, with any `user@` userinfo stripped.

    THE USERINFO IS WHY THIS IS A FUNCTION. Remote URLs in this fleet carry a baked-in account
    (`http://some-agent@host:9000/...`), so comparing raw netlocs would read two URLs on ONE host as
    two hosts -- and the pre-flight would decline to conclude, silently, on exactly the fleet it was
    written for. A guard that stops guarding without saying so is the worst disguise there is.
    """
    netloc = urlsplit(url).netloc
    return netloc.rpartition('@')[2] or None


def ls_remote(url: str, *, run, timeout: float) -> bool | None:
    """`True` reachable, `False` refused by the server, `None` INDETERMINATE (hung, or no git).

    The three-valued return is the whole point. Collapsing `None` into `False` is what would turn a
    dropped network into an accusation about somebody's permissions.
    """
    try:
        completed = run(
            [_GIT, '-c', 'credential.interactive=never', 'ls-remote', url, 'HEAD'],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, **_NON_INTERACTIVE},
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    return completed.returncode == 0


def stored_username(url: str, *, run, timeout: float) -> str | None:
    """The USERNAME of the credential git would send to this host. Never the secret.

    `git credential fill` writes `username=` and `password=` to stdout together. Only the first line
    is read out of the buffer and nothing else is returned, raised, logged or interpolated -- the
    caller is given a `str | None` and cannot reach the rest even by accident.
    """
    parts = urlsplit(url)
    try:
        completed = run(
            [_GIT, 'credential', 'fill'],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            input=f'protocol={parts.scheme}\nhost={host_of(url)}\n\n',
            env={**os.environ, **_NON_INTERACTIVE},
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    for line in (completed.stdout or '').splitlines():
        if line.startswith('username='):
            return line[len('username=') :].strip() or None
    return None


def origin_url(cwd, *, timeout: float = PROBE_TIMEOUT_S) -> str | None:
    """The URL of `cwd`'s own `origin` -- the remote on this fleet known to have worked."""
    try:
        completed = subprocess.run(
            [_GIT, 'remote', 'get-url', 'origin'],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    return completed.stdout.strip() or None


def preflight(
    requirements: list[str],
    *,
    control_url: str | None,
    run=subprocess.run,
    timeout: float = PROBE_TIMEOUT_S,
) -> str | None:
    """Why the install is about to fail, in one sentence -- or `None`, meaning proceed.

    WHAT THIS REPLACES, MEASURED 2026-08-11. A manifest declared an unpinned git dependency on a
    private repository. The credential stored for that host across the fleet was a role account
    installed by onboarding. It authenticates perfectly -- a sibling repository resolves on the same
    host in the same second -- but holds no read grant, and Gitea answers a private-and-invisible
    repository with 404 rather than 403. So the install died in dependency RESOLUTION on every
    machine, with a 404 buried in it: a message that blames resolution for an access-control fact and
    names no remedy. Hours, on four attempts.

    **THE CONTROL IS THE MECHANISM.** One probe cannot separate `this identity may not see it` from
    `the forge is down` -- both are a non-zero exit. Two probes on the SAME HOST can: if the control
    resolves, the host is up and the credential authenticates, and the only explanation left is the
    grant. If the control fails too, or there is no same-host control, this concludes NOTHING and
    falls through to the installer. That direction is deliberate: an operator whose install would
    have resolved from cache must not be stranded by a guess, and a wrong accusation about
    permissions sends somebody to an admin who cannot help them.

    `control_url` is REQUIRED and may be `None`. Making it explicit is what stops this module from
    reaching for a checkout it was never told about; `origin_url` is offered for callers who want the
    usual answer, and passing `None` means "no same-host control exists", which yields no verdict.

    Returns:
        The refusal text, or `None` when the check is satisfied OR indeterminate. It never
        distinguishes those two in its RETURN, because the caller must do the same thing for both.

    """
    for requirement in requirements:
        url = git_url(requirement)
        # NO PROBE AT ALL when nothing declares a git URL. Cheapness is a property, not an intention.
        if url is None or ls_remote(url, run=run, timeout=timeout) is not False:
            continue

        if (
            control_url is None
            or host_of(control_url) != host_of(url)
            or ls_remote(control_url, run=run, timeout=timeout) is not True
        ):
            return None  # indeterminate: the two explanations do not separate. uv gets its turn.

        who = stored_username(url, run=run, timeout=timeout)
        identity = f'`{who}`' if who else 'the credential stored for this host (username unreadable)'
        return (
            f'[install] REFUSED before uv: a declared dependency is invisible to this machine.\n'
            f'  url:      {url}\n'
            f'  from:     {requirement}\n'
            f'  identity: {identity}\n'
            f'  This is NOT a login failure. That identity AUTHENTICATES -- {control_url} resolves on\n'
            f'  the same host, in the same second. The server reports the repository as absent, which\n'
            f'  is what Gitea answers for a private repo the caller has no read grant on. uv would\n'
            f'  have shown you this as a dependency RESOLUTION error with a 404 inside it.\n'
            f'  REMEDY, and the first is usually the right one:\n'
            f'    1. Grant {identity} READ on that repository, as a collaborator or via an org team.\n'
            f'       That is an admin action on the forge, and a HUMAN one -- the host serves every\n'
            f'       lane at once. It is not fixable from here.\n'
            f'    2. Or store a credential for this host belonging to an identity that can see BOTH\n'
            f'       repositories. Interactive, per machine, and it replaces the key the working half\n'
            f"       already depends on -- so it is the operator's call, not an automatic repair.\n"
        )
    return None
