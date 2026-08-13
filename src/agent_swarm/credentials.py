"""Role credentials the fleet owns, kept OUT of the credential store a human is also using.

======================================================================================
WHY THIS FILE EXISTS -- measured 2026-08-11, and it cost four days of invisible breakage
======================================================================================

`swarmctl enroll` piped four role tokens into `git credential approve`. That is the OPERATOR'S
credential store: the same vault their own `git push` authenticates from, on Windows the same Windows
Credential Manager entry their IDE and their browser-driven OAuth flow write to.

**A credential store holds ONE entry per (protocol, host).** Four role accounts share one host. So
onboarding did not merely RISK overwriting the human's identity, it NECESSARILY did -- the last
`approve` wins and every earlier one, role or human, is gone. That is not an operator error to warn
about; it is an arrangement that cannot be operated correctly.

WHAT IT ACTUALLY COST. On one fleet machine the ambient credential for the forge became
`swarm-verifier`, a role account with read on one repository and no grant on another. Months later,
on a DIFFERENT machine, `uv pip install -e .[all,dev]` failed in dependency RESOLUTION with a 404
buried inside it -- because a `git+http://` dependency resolved through the ambient credential, which
was by then a service account nobody remembered installing. The distance between the cause and the
symptom is the whole point: four days, two machines, and a diagnosis that needed a control probe to
reach.

**THE RULE THIS BELONGS TO WAS ALREADY WRITTEN.** "Never silently mutate a shared resource the other
party will learn about by accident." The credential vault is exactly that resource; it simply was
never enumerated as one, because it is not a file in the repository and so did not look shared.

======================================================================================
THE MECHANISM: explicit per invocation, never ambient
======================================================================================

A role token now resolves from, in order:

1. **The environment** -- `SWARM_TOKEN_<ROLE>`. Explicit, per invocation, inherited only by the
   children a caller chooses. Nothing else on the box can observe it and nothing persists it.
2. **The swarm's OWN store** -- one owner-only JSON file next to `swarmctl`'s config, keyed by
   `(scheme, host, username)`, so four roles on one host coexist rather than overwrite. It is the
   fleet's file: deleting it affects the fleet and nothing else.

And from nowhere else. **The ambient git credential store is never written and never read.** A
machine that has one is unaffected by anything here, which is the property under test.

WHY `GIT_ASKPASS` FOR THE GIT SIDE, and it is the load-bearing choice rather than a preference. Some
fleet operations are real `git` invocations (clone, fetch, push as `swarm-agent`), and they need the
role token without the vault. Four mechanisms were available and three are worse on a property, not
on taste:

* **userinfo in the URL** (`http://user:token@host/...`) -- the token lands in `.git/config`, in
  reflogs, in `pip`'s `direct_url.json` (this package already documents that leak in
  `provenance.py`), and in the process table. It also teaches a helper to CACHE it, which is the
  defect being removed, reintroduced by the repair.
* **`-c credential.helper='!f(){ ...; }; f'`** -- a `!`-prefixed helper is run through a SHELL. Git
  for Windows ships one, a bare Windows Python plus system git need not, and the fleet is about to be
  mixed. It also puts the token on a command line.
* **`git credential approve` into a scoped store** -- still a write to a store, still one entry per
  host. The scope moved; the collision did not.
* **`GIT_ASKPASS`** -- git EXECUTES it directly, so no shell is required and the behaviour is
  identical on Windows and POSIX. The token travels in the child's ENVIRONMENT, never on a command
  line, and the launcher file written to make it executable holds no secret.

Paired with clearing `credential.helper` for that one invocation (via `GIT_CONFIG_*`, which is
per-process and cannot leak into a config file), git has no helper to consult and none to store into.
**That is what makes the ambient credential provably unchanged rather than merely untouched by our
code**: even if git decided to cache, there is nothing to cache into.

NO TOKEN VALUE IS EVER RETURNED IN AN ERROR, LOGGED, OR WRITTEN ANYWHERE BUT THE OWNER-ONLY STORE.
Summaries use a truncated sha256, as the rest of this package does.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import urllib.parse
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from agent_swarm import roles

#: The environment variable a caller sets to supply one role's token explicitly. `swarm-agent` ->
#: `SWARM_TOKEN_AGENT`; a non-role username is normalised the same way so the scheme has no hole.
_ENV_PREFIX = 'SWARM_TOKEN_'

#: Deliberately NOT `os.environ` at import time. A test, and a caller composing an environment for a
#: child, must both be able to hand this module the mapping it should read.
_DEFAULT_ENV = os.environ


def env_var_for(username: str) -> str:
    """`swarm-agent` -> `SWARM_TOKEN_AGENT`. The one spelling, so nothing composes it by hand."""
    bare = roles.strip_prefix(username)  # the prefix was inlined here -- the third of four spellings
    return _ENV_PREFIX + ''.join(c if c.isalnum() else '_' for c in bare).upper()


def store_path() -> Path:
    """The fleet's own credential file. Beside `swarmctl`'s config, and owner-only.

    NOT the git credential store, NOT the OS keychain, and that is the entire point: this file
    belongs to the swarm, so removing it removes the swarm's access and nobody else's. A shared
    store cannot express that, because it is keyed by host and a host has one entry.
    """
    if os.name == 'nt':
        base = Path(os.environ.get('APPDATA') or Path.home())
    else:
        base = Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config')
    return base / 'swarmctl' / 'credentials.json'


def write_secret_file(path: Path, payload: dict) -> None:
    """Write live plaintext credentials so that ONLY THE OWNER CAN READ THEM.

    MEASURED 2026-08-10 on the fleet host. This was `os.open(..., S_IRUSR | S_IWUSR)` plus a line of
    output saying "owner-readable only". **On Windows the POSIX mode is essentially ignored** -- NTFS
    gives the new file its parent directory's INHERITED ACL, and the one measured read granted
    `Authenticated Users` Modify and `Users` Read.

    So every authenticated user on the box could modify a file holding four live role credentials,
    and every user could read it, under a line asserting the opposite. The declaration was the only
    thing that was owner-only.

    The permission is therefore APPLIED per platform and then READ BACK, and a failure RAISES after
    deleting the file rather than printing a warning: a caller told it is protected will choose a
    transport on that belief. A warning on a file that still exists is the forbidden shape.

    Raises:
        PermissionError: the file could not be made owner-only. It has been DELETED first, so no
            credential is left readable by the failure path.

    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(handle, 'w', encoding='utf-8') as out:
        json.dump(payload, out)
    if os.name == 'nt':
        # `icacls` rather than a mode, and `/inheritance:r` is the half that matters: granting the
        # owner full control while leaving the inherited ACEs in place changes nothing at all.
        _harden_on_windows(path)
    elif path.stat().st_mode & 0o077:
        path.unlink(missing_ok=True)
        msg = f'{path.name} is group- or world-readable; deleted rather than left holding credentials'
        raise PermissionError(msg)


def _harden_on_windows(path: Path) -> None:
    import shutil  # noqa: PLC0415 -- only reached under `os.name == 'nt'`; see `_ICACLS` below

    icacls = shutil.which('icacls') or 'icacls'
    owner = os.environ.get('USERNAME') or ''
    subprocess.run(
        [icacls, str(path), '/inheritance:r', '/grant:r', f'{owner}:F'],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    shown = subprocess.run([icacls, str(path)], capture_output=True, text=True, check=False, timeout=60).stdout
    others = [
        line
        for line in shown.splitlines()
        if ':(' in line and owner.lower() not in line.lower() and 'Successfully' not in line
    ]
    if others:
        path.unlink(missing_ok=True)
        listed = '\n'.join(f'  {line.strip()}' for line in others)
        msg = (
            f'could not make {path.name} owner-only; it still granted:\n{listed}\n'
            '  It held live credentials, so it has been DELETED rather than left readable.\n'
            '  Write it to a directory you control, or move the credentials another way.'
        )
        raise PermissionError(msg)


def _key(scheme: str, host: str, username: str) -> str:
    """THE USERNAME IS IN THE KEY, and that is the defect this file repairs, stated as data.

    A store keyed by `(scheme, host)` alone can hold one of four role credentials. Keying by the
    username is what lets four roles share a host WITHOUT the last write erasing the rest -- and
    what lets a human's own credential live somewhere this never reaches.
    """
    return f'{scheme}://{username}@{host}'


def _load(path: Path | None = None) -> dict[str, str]:
    try:
        with (path or store_path()).open(encoding='utf-8') as handle:
            loaded = json.load(handle)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        # A CORRUPT STORE IS AN EMPTY STORE, never an exception carrying its contents. The contents
        # are tokens; an error message that quoted the unparseable text would be the leak.
        return {}
    return {str(k): str(v) for k, v in loaded.items()} if isinstance(loaded, dict) else {}


def resolve_token(
    scheme: str,
    host: str,
    username: str,
    *,
    env: dict[str, str] | None = None,
    path: Path | None = None,
) -> str | None:
    """This role's token: the ENVIRONMENT first, then the swarm's own store. Never the git vault.

    Returns None when neither has one -- a QUIET absence, because the caller decides what an
    un-enrolled machine means. It is never a prompt: `git credential fill` would fall back to an
    interactive dialog, which on Windows is a GUI that hangs an unattended run and invites somebody
    to type a human credential into a fleet process.
    """
    environ = _DEFAULT_ENV if env is None else env
    from_env = environ.get(env_var_for(username))
    if from_env:
        return from_env
    return _load(path).get(_key(scheme, host, username)) or None


def store_token(scheme: str, host: str, username: str, token: str, *, path: Path | None = None) -> None:
    """Persist one role token into the SWARM'S store, then read it back and refuse if it did not keep.

    THE READ-BACK IS INHERITED FROM A MEASURED LOSS, 2026-08-10, and it survives the move here
    unchanged in spirit. `git credential approve` exits 0 whether or not the helper kept anything;
    Git Credential Manager silently DROPPED two of four credentials for a plain-HTTP remote while
    `enroll` printed `stored` for all four. Gitea keeps only a HASH of a token, so the plaintext
    existed exactly once and those two were unrecoverable -- and un-remintable, because Gitea refuses
    a duplicate token name.

    A write that cannot fail is indistinguishable from one that works. That reasoning was never about
    GCM specifically; a file write can fail too -- a full disk, a roaming profile, an ACL that
    silently discards. So the read-back stays.

    Raises:
        PermissionError: the store could not be made owner-only (see `write_secret_file`).
        RuntimeError: the store accepted the token and did not keep it. NEVER carries the token.

    """
    target = path or store_path()
    everything = _load(target)
    everything[_key(scheme, host, username)] = token
    write_secret_file(target, everything)
    if resolve_token(scheme, host, username, env={}, path=target) != token:
        msg = (
            f'the swarm credential store accepted {username}@{host} and did not keep it.\n'
            f'  store: {target}\n'
            f'  NOTHING ELSE HOLDS THIS SECRET -- the forge stores only a hash, so it is gone now and\n'
            f'  its name cannot be reused. Re-run with --machine <a different name>.'
        )
        raise RuntimeError(msg)


def forget_token(scheme: str, host: str, username: str, *, path: Path | None = None) -> None:
    """Drop one role's token from the swarm's store. Silent when there was none."""
    target = path or store_path()
    everything = _load(target)
    if everything.pop(_key(scheme, host, username), None) is not None:
        write_secret_file(target, everything)


# --------------------------------------------------------------------------- the git side


#: The launcher git executes. It holds NO SECRET -- the token reaches the child through the
#: environment -- so its only requirement is to be executable, which is why it can be written to a
#: temp directory without the owner-only dance `write_secret_file` performs.
_ASKPASS_POSIX = (
    '#!/bin/sh\nexec "$SWARM_ASKPASS_PYTHON" -c "import os,sys;sys.stdout.write(os.environ[\'SWARM_ASKPASS_TOKEN\'])"\n'
)
_ASKPASS_WINDOWS = (
    '@echo off\r\n"%SWARM_ASKPASS_PYTHON%" -c "import os,sys;sys.stdout.write(os.environ[\'SWARM_ASKPASS_TOKEN\'])"\r\n'
)


#: WHAT MAKES A GIT CALL UNABLE TO ASK A HUMAN ANYTHING. THE ONE SPELLING.
#:
#: MEASURED TWICE, in two modules, before it was shared. 2026-08-11: an interactive helper raised a
#: window and the command hung until it was killed. 2026-08-13, on a fresh fleet box: `git fetch`
#: -- a READ -- raised a Git Credential Manager window on a human's desktop asking for a service
#: account's password, and the step reported `TimeoutExpired`, which reads as an unreachable forge.
#:
#: A PROMPT IS NOT A SLOWER FAILURE, IT IS A WORSE OUTCOME. The OS credential store is keyed on HOST
#: alone while several role identities may share one forge, so a human who answers displaces their
#: OWN credential for that host and finds out days later, elsewhere, from a 404 that reads as a
#: deleted repository.
#:
#: `GIT_TERMINAL_PROMPT=0` suppresses the TERMINAL prompt and NOT the GUI one -- which is the whole
#: reason the other two entries exist. None of them binds an unknown helper: what actually holds is
#: the caller's timeout, and these ask every helper we know of to stay silent.
NON_INTERACTIVE = {
    'GIT_TERMINAL_PROMPT': '0',
    'GCM_INTERACTIVE': 'never',
    'GCM_PROVIDER': 'generic',
}


@contextmanager
def git_env_for(
    scheme: str,
    host: str,
    username: str,
    *,
    env: dict[str, str] | None = None,
    path: Path | None = None,
) -> Iterator[dict[str, str]]:
    """An environment in which `git` authenticates as ONE ROLE and cannot touch the ambient store.

    Use it around a real git invocation::

        with git_env_for('http', 'forge:9000', 'swarm-agent') as environ:
            subprocess.run([git, 'push', url, 'HEAD'], env=environ, check=True)

    TWO HALVES, and only together do they hold:

    * `GIT_ASKPASS` supplies the token for THIS invocation. Git executes it directly -- no shell, so
      Windows and POSIX behave identically -- and reads the token off its stdout. The token is in the
      child's environment, never on a command line, so it is not visible in the process table.
    * `credential.helper` is cleared through `GIT_CONFIG_COUNT`/`_KEY_`/`_VALUE_`, which is
      per-process and cannot reach a config file. **An empty value RESETS the helper list**, so this
      invocation has no helper to read the operator's vault with and none to write it back into.

    The second half is what makes the property provable rather than merely intended: the ambient
    credential is unchanged not because we avoided writing it, but because git had no helper.

    Raises:
        LookupError: no token for that role. NAMED rather than falling through to git, which would
            prompt, hang, or silently authenticate as whoever the vault holds -- the exact confusion
            this module exists to end.

    """
    token = resolve_token(scheme, host, username, env=env, path=path)
    if token is None:
        msg = (
            f'no token for {username}@{host} in this process.\n'
            f'  Set {env_var_for(username)}, or run `swarmctl enroll` / `swarmctl consume` on this\n'
            f'  machine. The ambient git credential store is deliberately NOT consulted.'
        )
        raise LookupError(msg)
    base = dict(_DEFAULT_ENV if env is None else env)
    with tempfile.TemporaryDirectory(prefix='swarm-askpass-') as scratch:
        launcher = Path(scratch) / ('askpass.cmd' if os.name == 'nt' else 'askpass.sh')
        launcher.write_text(_ASKPASS_WINDOWS if os.name == 'nt' else _ASKPASS_POSIX, encoding='utf-8')
        launcher.chmod(0o700)
        base.update(
            {
                'GIT_ASKPASS': str(launcher),
                'SWARM_ASKPASS_PYTHON': sys.executable,
                'SWARM_ASKPASS_TOKEN': token,
                # No terminal prompt and no helper UI, for the same reason the token is explicit: an
                # unattended run must FAIL rather than wait at a console nobody is at.
                **NON_INTERACTIVE,
                # THE HELPER LIST, CLEARED. An empty `credential.helper` resets it, so nothing in
                # this invocation can read or write the operator's store.
                'GIT_CONFIG_COUNT': '1',
                'GIT_CONFIG_KEY_0': 'credential.helper',
                'GIT_CONFIG_VALUE_0': '',
            }
        )
        yield base


# --------------------------------------------------------------------------- onboarding checks


def probe_readable(
    owner: str,
    repo: str,
    token: str,
    *,
    call,
) -> bool | None:
    """Can this token READ that repository? `True` yes, `False` refused, `None` UNANSWERABLE.

    THREE-VALUED, AND COLLAPSING IT IS THE DEFECT. A forge that is down, a DNS failure and a dropped
    packet all produce "not a success", and reading any of them as "this identity lacks a grant"
    turns a network hiccup into an accusation about somebody's permissions -- which sends an operator
    to an administrator who cannot help them. `None` means the question was not answered and the
    caller must say so rather than guess.

    404 COUNTS AS A REFUSAL, not as absence, and that is measured rather than assumed: Gitea answers
    `404 Repository not found` -- deliberately, as information hiding -- for a private repository the
    authenticated caller may not see. From the client, "deleted" and "invisible to this identity" are
    indistinguishable, which is exactly why the CALLER pairs this with a control (see
    `unreadable_repositories`).

    `call` is the seam: a callable performing one authenticated GET and returning a payload, `None`
    for an allowed refusal, or raising for anything unreachable. It is injected rather than built
    here so this stays free of a transport and a test needs no server.
    """
    path = f'/repos/{urllib.parse.quote(owner)}/{urllib.parse.quote(repo)}'
    try:
        payload = call('GET', path, auth='raw', allow=(401, 403, 404), raw_token=token)
    except Exception:  # noqa: BLE001 -- any transport failure is UNANSWERABLE, never a verdict
        return None
    return payload is not None


def unreadable_repositories(
    required: list[str],
    token: str,
    *,
    call,
) -> tuple[list[str], list[str]]:
    """`(refused, unanswerable)` over the repositories this identity MUST be able to read.

    TWO BUCKETS, NEVER ONE, and it is the property that carried over from the install pre-flight
    built the same night: a single non-success cannot separate "this identity has no grant" from
    "the forge is unreachable". So they are returned SEPARATELY and the caller must treat them
    differently -- refuse on the first, degrade on the second. Merging them would make onboarding
    fail closed on a network hiccup, and an onboarding that does that is one an operator learns to
    bypass.

    **THE CONTROL IS BUILT INTO THE THREE-VALUED PROBE, which is why no separate control request is
    made here.** A `False` means the server ANSWERED and refused -- so the host was up, the token
    authenticated, and the transport worked. A refusal is therefore self-controlling: it is already
    evidence of everything a control probe would have been asked to establish. `None` is the only
    value that proves nothing, and it never enters `refused`.
    """
    refused, unanswerable = [], []
    for full_name in required:
        owner, _, repo = full_name.partition('/')
        if not owner or not repo:
            msg = f'a required repository must be OWNER/NAME, got {full_name!r}'
            raise ValueError(msg)
        answer = probe_readable(owner, repo, token, call=call)
        if answer is None:
            unanswerable.append(full_name)
        elif not answer:
            refused.append(full_name)
    return refused, unanswerable
