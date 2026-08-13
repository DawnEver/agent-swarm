"""Manage the swarm's forge identities and onboard repositories -- Gitea today, GitHub next.

    python -m agent_swarm.swarmctl VERB ...     -- what the `swarmctl` / `swarmctl.cmd` shims run

    swarmctl config ADMIN             remember this machine's settings (see below)
    swarmctl list                     what exists NOW, read from the server
    swarmctl provision                ensure the four users and teams; updates units in place
    swarmctl onboard O/R [-p]         make a repo iterable by the swarm (teams + optional protection)
    swarmctl enroll                   issue this machine's four tokens and store them locally
    swarmctl emit NAME                issue another machine's tokens into a ONE-TIME bundle
    swarmctl consume FILE             on that machine: store the bundle, then delete it
    swarmctl revoke NAME|unmanaged|all   revoke by machine, by origin, or everything
    swarmctl admin-emit NAME          a READ-ONLY admin credential for another machine
    swarmctl verify O/R               read back what the server actually enforces
    swarmctl destroy DESTROY          remove the users and teams (guarded, opt-in, last resort)

Each verb takes ONE positional, and it is the thing that verb is about. The equivalent long flag
always works too; the positional is the shorthand.

SETTINGS RESOLVE built-in < config file < environment < command line. The config file is per user and
per machine (`swarmctl config` prints its path), because the admin account, the Gitea binary and the
machine name differ per host -- values that, kept in a shared file, are overwritten by whoever
commits next.

CROSS-PLATFORM ON PURPOSE: all behaviour lives HERE, in stdlib Python. The `swarmctl` / `swarmctl.cmd`
shims beside this checkout only locate an interpreter, put `src` on `PYTHONPATH` and forward their
arguments to `-m agent_swarm.swarmctl`. A wrapper that also parsed verbs would be the same logic
written twice, in two shells, with only one of them ever tested.

WHY VERBS AND NOT ONE SCRIPT. Provisioning is a one-off; onboarding a repo happens per project;
enrolling happens per machine; revoking happens when a machine is lost. Collapsing them into a single
"init" means the only way to add a repo is to re-run everything, and the only way to lose a token is
to lose all of them.

THE THING THAT BREAKS FIRST IF YOU SKIP IT -- and it is not permissions:

    **Four credentials for one host cannot be told apart without a username.**

Every lookup keys on (protocol, host, USERNAME), and git takes that username from the remote URL.
Four tokens for one host behind a URL with no username is an ambiguous lookup. So every swarm remote
must carry its role:

    http://swarm-agent@server:9000/org/repo.git

`onboard` prints the exact URLs, `enroll` stores credentials under those usernames, and `verify`
checks the pair actually resolves. This is a protocol property, not a Gitea one -- it is identical on
GitHub, and it is the SAME reason the swarm's own store is keyed by username: a store keyed by host
alone can hold one of four, which is what the ambient one did.

PROVIDER SEAM. Everything except user creation is provider-neutral: teams, repo attachment, branch
protection and credential storage all exist on GitHub. User creation does not -- GitHub has no API
for it, machine users are made by hand or replaced by an App. `GitHubProvider` therefore refuses
`provision` by NAME, listing what a human must do instead, rather than pretending.

TOKENS ARE NEVER PRINTED. They go into the SWARM'S OWN owner-only store (`agent_swarm.credentials`),
or into a bundle whose only purpose is one move between machines. Summaries show a truncated sha256
-- a hard project invariant: never log tokens, hashes only.

**NOT THE OPERATOR'S CREDENTIAL STORE, and that is a repair rather than a preference.** This used to
pipe them into `git credential approve`. A credential store holds ONE entry per (protocol, host) and
four role accounts share one host, so enrolment did not merely risk overwriting the human's identity,
it necessarily did -- measured 2026-08-11, four days and two machines from cause to symptom. See
`credentials.py`; a role token is now explicit per invocation, and nothing here writes an ambient
store.

STDLIB ONLY, and it is the whole package's constraint rather than this file's: this has to run on
the Gitea host, where no venv exists and nothing is installed. A source checkout plus any Python 3
is the entire requirement, which is what the shims arrange.
"""

from __future__ import annotations

import argparse
import base64
import datetime
import getpass
import hashlib
import http.client
import json
import os
import secrets
import socket
import stat
import subprocess
import sys
import time
import urllib.parse
from collections.abc import Callable
from pathlib import Path
from typing import Any

from agent_swarm import credentials, roles
from agent_swarm.forge import _BACKOFF_S as BACKOFF_S
from agent_swarm.forge import API_ATTEMPTS, _is_retryable_status

TIMEOUT = 30

#: ONE RETRY POLICY, ONE DEFINITION -- USER DIRECTIVE 2026-08-10: 「所有 gitea 操作都要重试 3 次」.
#:
#: THIS USED TO BE A SECOND COPY of `agent_swarm.forge`'s, with a comment saying it could not import
#: the first because the file was stdlib-only and lived in another repository. The move deleted that
#: reason, so the copy went with it: two spellings of one directive is the duplicated-scheme defect,
#: and a cross-checking test only makes a duplicate honest, never singular.
#:
#: WHAT MUST NOT RETRY: a 4xx. A 401 retried three times is still a 401, and retrying makes a
#: permanent credential problem look flaky -- measured on the fleet host, where token auth on the
#: token-management routes 401s BY DESIGN and the old code would have hammered it. `_is_retryable_status`
#: is imported for the same reason; 429 is deliberately outside it, because Gitea's rate limit wants
#: a longer, header-driven wait and treating it as ordinary transience turns a limit into a ban.
#:
#: WHAT MUST: 5xx and connection errors. The case that matters is a request that SUCCEEDED
#: server-side whose response was lost; from here it is indistinguishable from one that never
#: arrived. `provision` and `onboard` are idempotent by construction ("already present"), which is
#: what makes retrying their writes safe rather than merely hopeful.
#:
#: The backoff is aliased rather than re-declared, for the same reason: `forge` spells it private,
#: and renaming it there belongs to a change that owns that file.

#: Indirection so a test can assert the backoff HAPPENS without waiting for it. Deliberately NOT
#: shared with `forge`'s: a test patching one must not silently mute the other's timing too.
_sleep = time.sleep

#: THE CHECK NAME IS NOT DECLARED HERE, and never will be. This module is fleet INFRASTRUCTURE --
#: it onboards machines and mints credentials for ANY repository -- so a built-in `<project>/gate`
#: would be `forge.DEFAULT_REPO` under a new spelling: a vendor-neutral layer holding one project's
#: fact, invisible exactly because the default works. That constant was removed for that reason and
#: `tests/test_this_package_names_no_specific_project.py` now refuses its return, in this file too.
#:
#: The name belongs to whoever PUBLISHES the status, because that is what configures its own branch
#: protection, and it reaches this CLI the way every other deployment fact does:
#: `SWARM_STATUS_CONTEXT`, the stored config, or `--status-context`. THERE IS NO FALLBACK,
#: deliberately and for the same reason `--repo` has none: a check name defaulted wrong protects a
#: branch against a status nobody publishes, and that reads as a broken gate.

#: role -> (unit permissions, token scopes)
#:
#: THE TEAM NAME IS NOT IN THIS TABLE, and that is deliberate -- see `TEAMS`. It used to be the first
#: element of each tuple, four hand-written literals sitting beside a DERIVED `USERS`. One prefix,
#: two mechanisms, and only one of them could keep a fifth role honest.
#:
#: NOT SERVER-ENFORCED, and stated here so this table is not misread: Gitea has no scope for commit
#: status -- writing one needs repository write. `swarm-agent` must have that to push branches, so
#: it can set a status too. "Only swarm-verifier marks a commit green" is carried by which process
#: holds which credential, NOT by the server. Measured 2026-08-10. GitHub can enforce it (an App may
#: hold statuses:write without contents:write), which is why the role table is data and not code.
ROLES: dict[str, tuple[dict[str, str], list[str]]] = {
    'observer': (
        {'repo.code': 'read', 'repo.issues': 'read', 'repo.pulls': 'read'},
        ['read:repository', 'read:issue', 'read:organization'],
    ),
    'agent': (
        {'repo.code': 'write', 'repo.issues': 'write', 'repo.pulls': 'write'},
        ['write:repository', 'write:issue', 'read:organization'],
    ),
    'verifier': ({'repo.code': 'write', 'repo.issues': 'read'}, ['write:repository']),
    'integrator': (
        {'repo.code': 'write', 'repo.pulls': 'write', 'repo.issues': 'read'},
        ['write:repository'],
    ),
}

#: THE PREFIX MOVED DOWN TO `roles`, because `forge` needs the same fact and is a layer BELOW this
#: one -- it cannot import from here without pointing up the arrow, which is why four literals had
#: grown there instead. Both the account names and the team names are still built from one string,
#: so a fifth role cannot arrive with an unprefixed team; there is no literal to misspell.
_PREFIX = roles.PREFIX

#: RE-EXPORTED, NOT DEFINED. This was a derivation while `forge.ROLE_ACCOUNTS` was four literals of
#: the same fact, and each of the two docstrings called ITSELF the only thing deciding which role a
#: process acts as. Now one owner, two aliases, and no way to edit half the fleet's identity.
USERS = roles.ACCOUNTS

#: role -> team name. DERIVED, exactly like `USERS` and for the same reason.
#:
#: USER DIRECTIVE 2026-08-11: 「teams 命名很容易引起误解 比如 Agents 让人会误解 teams 名也直接叫
#: Swarm-Agents Swarm-xxx 更好」. In a SHARED org a team called `Agents` reads as "the agents", not
#: "this swarm's agents" -- it claims a generic noun the org's humans also use. The prefix is not
#: decoration; it is the difference between naming a thing and claiming a category.
TEAMS = {role: f'{_PREFIX.capitalize()}-{role.capitalize()}s' for role in ROLES}

#: old team name -> role. A RENAME IS AN EVENT; THE ABILITY TO RECOGNISE THE PRE-MIGRATION STATE IS A
#: PROPERTY, so it is registered here rather than remembered.
#:
#: WITHOUT THIS, CHANGING `TEAMS` WOULD NOT HAVE RENAMED ANYTHING. `ensure_team` looks a team up BY
#: NAME, so a fresh constant simply misses the old team and CREATES a new one: eight teams, the live
#: memberships and repo attachments still on the four old ones, the four new ones empty. Every
#: machine keeps working -- its token is already issued -- until someone re-provisions, at which
#: point access moves to teams nobody is in. A silent, delayed failure, which is the same class as
#: the credential clobber removed earlier tonight.
#:
#: It is NOT a compatibility shim and does not belong to a retirement registry: these names are live
#: DATA on running servers, and recognising server state is not the same as keeping a second spelling
#: alive in the code. Once no deployment carries them, this mapping is deleted with its migration.
LEGACY_TEAMS = {f'{role.capitalize()}s': role for role in ROLES}


def _old_name_of(role: str) -> str | None:
    """What this role's team was called before the `Swarm-` prefix, or None if the role is new.

    READ OUT OF `LEGACY_TEAMS` rather than recomputed. A second derivation of the old spelling would
    be free to drift from the registry documenting it, and the drift would be invisible: both would
    produce a plausible name and only one would match what a live server actually holds.
    """
    return next((old for old, owner in LEGACY_TEAMS.items() if owner == role), None)


#: THIS MODULE NO LONGER RUNS `git` AT ALL, and the two resolved executables that used to live here
#: went with the code that needed them: `_GIT` existed only to drive the credential store this tool
#: has stopped writing, and `_ICACLS` moved to `credentials` beside the file it hardens. A constant
#: kept "in case" is a claim that something still uses it.


EXIT_INCOMPLETE = 2
"""`verify` looked at only part of the deployment. NEITHER a pass NOR a failure.

THE THIRD VALUE EXISTS BECAUSE COLLAPSING IT LIES IN BOTH DIRECTIONS. `0` claims a deployment is
correct that nobody read; `1` accuses one that nobody read. The same distinction `gate.py` draws
with INCONCLUSIVE, for the same reason: a run that could not look is evidence about the RUNNER, not
about the subject.

Measured 2026-08-12 on the Gitea host: `verify` hit the no-admin-credential arm and exited 0 with
`configuration problems: none`, having read no team, no membership and no branch protection.
"""


class Fail(RuntimeError):
    """Something the operator must fix. NEVER carries a token."""


def say(msg: str = '') -> None:
    sys.stdout.write(msg + '\n')
    sys.stdout.flush()


def fingerprint(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()[:12]


# --------------------------------------------------------------------------- providers


class GiteaProvider:
    """Gitea. The only provider with a user-creation path, because it has a server-side CLI."""

    name = 'gitea'

    def __init__(
        self,
        base_url: str,
        org: str,
        exe: str | None,
        admin_user: str | None,
        *,
        ask_password: bool = False,
        admin_token_name: str | None = None,
    ) -> None:
        split = urllib.parse.urlsplit(base_url)
        # THE SCHEME IS CHECKED AGAINST AN ALLOWLIST, not merely for being non-empty: it selects
        # the connection class in `_call`, and an unknown scheme must be refused where the operator
        # typed it rather than defaulted to plain HTTP three layers down.
        if split.scheme not in {'http', 'https'} or not split.netloc:
            msg = f'--base-url must look like http://host:port or https://host, got {base_url!r}'
            raise Fail(msg)
        self.base_url = base_url.rstrip('/')
        self.scheme, self.netloc = split.scheme, split.netloc
        # A base URL may carry a path (http://host/gitea). It belongs on every request line, so
        # it is kept rather than dropped -- dropping it fails only against sub-path installs.
        self.prefix = split.path.rstrip('/')
        self.org = org
        # A CONFIGURED PATH THAT DOES NOT EXIST MEANS "not the Gitea host", not "broken install".
        # The wrapper carries the host's binary path on every machine, so absent-here is the normal
        # off-host state; treating it as an error buries the one useful instruction (`admin-emit`)
        # under a path complaint. The path is kept only to say WHERE we looked.
        self.exe_configured = exe
        self.exe = exe if exe and Path(exe).is_file() else None
        self.admin_user = admin_user
        #: The name a CLI-minted admin token is created under. Defaults to this machine's hostname.
        #:
        #: OVERRIDABLE BECAUSE THE DEFAULT IS A WEDGE, MEASURED 2026-08-12 ON THE GITEA HOST. The
        #: name was fixed at `swarmctl-admin@<gethostname()>`, and the local store and the server
        #: can desync -- a cleared store, a re-imaged box, a lost store write, or (what actually
        #: happened) a 403 that made `_forget_stored_admin` erase the local half. From then on every
        #: run tries the same taken name, Gitea refuses it, and the value is unrecoverable because
        #: Gitea shows it once. The ONLY recovery was `revoke`, which needs the operator's PASSWORD
        #: -- so a fleet tool built to keep human credentials out of its own path had a failure mode
        #: whose sole exit was to reach for one.
        #:
        #: AN EXPLICIT OVERRIDE RATHER THAN AN AUTOMATIC SUFFIX. Auto-disambiguating would restore
        #: the N-tokens design this file argued its way out of, silently, one per desync. A flag
        #: keeps the count deliberate: the operator names the token, so they can also revoke it.
        self.admin_token_name = admin_token_name
        self._token: str | None = None
        #: Where `_token` came from. Only a credential READ FROM THE STORE may be forgotten on a
        #: refusal -- one minted THIS run that 401s means something else is wrong, and erasing it
        #: would hide that behind a message about a stale credential.
        self._token_came_from_the_store = False
        self._password: str | None = None
        #: Prompting is opt-in. See `admin_password`: a default of True hangs unattended runs.
        self.ask_password = ask_password

    # ---- CLI (host only) --------------------------------------------------

    def _cli(self, *args: str) -> str:
        if not self.exe:
            where = f' (looked at {self.exe_configured})' if self.exe_configured else ''
            msg = (
                f'this verb needs the Gitea CLI, which only exists on the Gitea host{where}.\n'
                '  Creating users and minting tokens has no API path -- run it there.'
            )
            raise Fail(msg)
        proc = subprocess.run([self.exe, *args], capture_output=True, text=True, check=False, timeout=180)
        if proc.returncode != 0:
            msg = f'gitea {" ".join(args[:3])} failed:\n{(proc.stderr or proc.stdout).strip()[:600]}'
            raise Fail(msg)
        return proc.stdout

    def user_exists(self, username: str) -> bool:
        for line in self._cli('admin', 'user', 'list').splitlines():
            fields = line.split()
            if len(fields) > 1 and fields[1] == username:
                return True
        return False

    def credential_works(self, username: str, owner: str, repo: str) -> bool | None:
        """Can this role actually reach the repo with the credential stored HERE -- None if unknown.

        WHY THIS CHECK EXISTS. Measured on the live host 2026-08-10: the four role accounts had been
        created by hand before this tool existed and carried `must_change_password`, so Gitea
        answered `403 You must change your password` to every token they minted. Everything `verify`
        read -- teams, membership, attachment, units -- was correct, and it printed
        `configuration problems: none` over a swarm that could not authenticate at all.

        THE ENDPOINT IS THE REPO, AND THE FIRST VERSION OF THIS GOT IT WRONG in the way worth
        recording. It asked `GET /user`, which needs `read:user` -- a scope NO role carries. So every
        role 403'd for a reason that has nothing to do with authentication, the code read a 403 as
        "reachable", and `verify` printed four `ok` lines against a server where the probe could
        never have succeeded. A check that passes for all inputs is not a weaker check; it is a
        louder lie than the silence it replaced. Caught by the agent driving the live host, not by
        this suite -- which is why the scope-vs-auth distinction is now asserted below.

        `/repos/{owner}/{repo}` is covered by `read:repository`, which every role in `ROLES` has, so
        a refusal here IS about the identity. Asking the credential to do something rather than
        reading a flag stays the right shape: it also catches a revoked token and a disabled
        account, and it is not tied to Gitea's schema.

        None means UNANSWERABLE -- no stored credential for this user on this machine, or the server
        could not be reached -- and callers must not read that as "fine". A machine that has not
        enrolled cannot answer for the server.
        """
        secret = read_credential(self.scheme, self.netloc, username)
        if secret is None:
            return None
        path = f'/repos/{urllib.parse.quote(owner)}/{urllib.parse.quote(repo)}'
        try:
            payload = self._call('GET', path, auth='raw', allow=(401, 403, 404), raw_token=secret)
        except Fail:
            return None
        # `allow` turns a refusal into None. An ANSWER means the identity was accepted; 404 counts as
        # a refusal here because Gitea hides a repo the caller may not see behind a 404.
        return payload is not None

    def create_user(self, username: str) -> None:
        # A random password, never stored and never printed: these accounts authenticate by token.
        self._cli(
            'admin',
            'user',
            'create',
            '--username',
            username,
            '--password',
            secrets.token_urlsafe(32),
            '--email',
            f'{username}@{urllib.parse.urlsplit(self.base_url).hostname}',
            '--must-change-password=false',
        )

    def delete_user(self, username: str) -> None:
        self._cli('admin', 'user', 'delete', '--username', username, '--purge')

    #: Gitea's refusal when a token of that name already exists on the SERVER. Matched as a
    #: substring of the CLI's stderr, which is the only place it appears.
    _NAME_TAKEN = 'has been used already'

    def _issue_token_remotely(self, username: str, token_name: str, scopes: list[str]) -> str:
        """Mint a token over the API, with no Gitea CLI anywhere. Needs an ADMIN token, no password.

        MEASURED against Gitea 1.27.1 before this was written, because the module's own refusal said
        "minting tokens has no API path" and that was a claim about the world nobody had re-checked:

            admin token -> POST /users/<other>/tokens               401 auth required
            admin token -> POST /users/<other>/tokens?sudo=<other>  401 auth required
            admin token -> PATCH /admin/users/<u> {password}        200
            basic <u>   -> POST /users/<u>/tokens                   201  minted

        So the token-management surface refuses TOKEN auth outright -- 401 rather than 403, and no
        scope message -- which is a circularity guard, not a permission. An admin may still SET a
        password, and that password may then mint.

        THE PASSWORD IS EPHEMERAL AND NEVER LEAVES THIS FRAME. Generated per issue, used in the next
        call, never returned, stored or logged -- so nobody ends up holding a long-lived password for
        a service account, and this needs no human secret at all. The account keeps a strong unknown
        password afterwards; the way back is this same admin route.

        THE TWO FAILURES ARE NAMED SEPARATELY because they are different errands: a refused password
        write is about the ADMIN credential, a refused mint is about the target account.
        """
        password = secrets.token_urlsafe(32)
        quoted = urllib.parse.quote(username)
        try:
            self._call('PATCH', f'/admin/users/{quoted}', {'login_name': username, 'password': password})
        except Exception as exc:
            msg = (
                f'could not set an ephemeral password for {username!r}: the ADMIN credential was '
                f'refused, which is a different problem from the mint below ({exc})'
            )
            raise Fail(msg) from exc

        def mint() -> object:
            return self._call(
                'POST',
                f'/users/{quoted}/tokens',
                {'name': token_name, 'scopes': scopes},
                auth='raw-basic',
                raw_basic=(username, password),
            )

        try:
            got = mint()
        except Exception as exc:
            if self._NAME_TAKEN not in str(exc):
                msg = f'the forge refused to mint {token_name!r} for {username!r} ({exc})'
                raise Fail(msg) from exc
            # REVOKE AND REMINT, WHICH ONLY THIS ROUTE CAN DO BY ITSELF. The state is the one a
            # MIGRATION leaves everywhere: the old forge's tokens arrive with the repositories, so
            # every machine's token NAME is already taken while no machine holds the value -- Gitea
            # shows a token once. The CLI path's own remedy says revoking "needs the operator
            # PASSWORD", because Gitea refuses token auth on that route; here the ephemeral password
            # for exactly this user is still in frame, so the collision needs no human.
            #
            # ONE RETRY, NEVER A LOOP: if the delete succeeded and the name is still taken, the
            # server is telling us something this code does not model, and retrying would spin.
            self._call(
                'DELETE',
                f'/users/{quoted}/tokens/{urllib.parse.quote(token_name)}',
                auth='raw-basic',
                raw_basic=(username, password),
                allow=(404,),
            )
            try:
                got = mint()
            except Exception as retry:
                msg = (
                    f'{token_name!r} was already taken for {username!r}; the old one was revoked and '
                    f'the remint still failed ({retry})'
                )
                raise Fail(msg) from retry
        token = (got or {}).get('sha1') if isinstance(got, dict) else None
        if not token:
            msg = f'the forge accepted the mint for {username!r} but returned no token value'
            raise Fail(msg)
        return token

    def issue_token(self, username: str, token_name: str, scopes: list[str]) -> str:
        """Mint a token, or REFUSE WITH A REMEDY when the name is already taken on the server.

        THE THIRD STATE, MEASURED 2026-08-12 ON THE GITEA HOST. `token()`'s self-heal covers
        "stored locally, revoked server-side": the 401 erases the local copy and the next run mints
        afresh. It does NOT cover the mirror image -- **present on the SERVER, absent from the local
        store** -- which is what a cleared credential store, a re-imaged box, or a mint whose store
        write was lost all leave behind.

        In that state every run tries the same name, Gitea refuses it, and the box is WEDGED
        PERMANENTLY. Nothing in the old message said so: the operator saw a raw CLI error naming a
        token they cannot see, with no way to get from there to a working machine. And the token
        itself is unrecoverable -- Gitea shows a value once -- so "just find it" is not available.

        The remedy has to be revoke-then-remint, and it must be NAMED HERE, because this is the
        only place that knows both the token name and the host it belongs to.
        """
        if not self.exe:
            # NO BINARY, SO THE API ROUTE. Not a degraded fallback -- on a host that runs Gitea and
            # nothing else there is no binary to reach, and the old refusal here told the operator to
            # "run it there", on a machine that by directive runs no code of ours.
            return self._issue_token_remotely(username, token_name, scopes)
        try:
            out = self._cli(
                'admin',
                'user',
                'generate-access-token',
                '--username',
                username,
                '--token-name',
                token_name,
                '--scopes',
                ','.join(scopes),
            )
        except Fail as exc:
            if self._NAME_TAKEN not in str(exc):
                raise
            msg = (
                f'a token named {token_name!r} already exists on the SERVER, but this machine has\n'
                f'  no copy of it. Gitea shows a token value once, so it cannot be recovered -- and\n'
                f'  every run will fail here identically until the server-side one is removed.\n'
                f'  TWO WAYS OUT. The second needs no password and is the one to reach for:\n'
                f'      swarmctl --admin-token-name {token_name}-2 <verb>    (mint under a free name)\n'
                f'      swarmctl revoke --token-name {token_name}            (needs the operator PASSWORD)\n'
                f'  Prefer the first: token management is the one route Gitea refuses to token auth,\n'
                f'  so revoking drags a human credential into a path built to keep them out of it.\n'
                f'  This is NOT a login failure and NOT a scope problem: the credential is fine,\n'
                f'  there are simply two halves of one pairing and only the server has its half.'
            )
            raise Fail(msg) from exc
        for line in out.splitlines():
            if 'successfully created:' in line:
                return line.rsplit(':', 1)[1].strip()
        msg = f'could not read a token for {username} out of the CLI output'
        raise Fail(msg)

    # ---- API --------------------------------------------------------------

    #: The username a stored admin credential lives under. Synthetic on purpose: it names a ROLE
    #: swarmctl plays, not a person, so an operator's own Gitea credential is never picked up by
    #: accident and revoking swarmctl's access does not disturb anyone's login.
    ADMIN_CRED_USER = 'swarmctl-admin'

    def token(self) -> str:
        """An admin token for this run: the credential store first, the CLI second.

        THE STORE COMES FIRST so read-only verbs run from ANY machine. Only the Gitea host has the
        CLI, and requiring it for `list`/`verify` would mean the machines actually running agents
        could never check what they are pointed at.

        A CLI-MINTED TOKEN IS MINTED ONCE AND STORED, so the second run finds it above and no
        further one is ever created. It used to be minted per run and revoked at exit, and that
        design cannot work on Gitea: `GET/DELETE /users/<u>/tokens` answers 401 to TOKEN auth -- a
        token may not enumerate or delete tokens -- so the revoke ALWAYS failed and every run
        deposited a standing `write:admin` credential. Measured 2026-08-10 on the live host: three
        runs, three leftovers, and no `revoke` selector could reach them.

        So the choice is not "ephemeral vs standing"; it is ONE standing admin token or N of them.
        One, named for the machine and stored where `admin-emit`/`consume` already put credentials,
        is revocable by hand (`revoke --token-name`) and is the same mechanism every other machine
        already uses. N is what the ephemeral design actually produced.

        A STORED CREDENTIAL IS NOT VALIDATED HERE, and that is deliberate: proving it would cost a
        round trip on every run to answer a question the very next call answers for free. What the
        store gets instead is a way to be WRONG ONCE -- `_token_came_from_the_store` records where
        this one came from, and a 401/403 on a token-auth call erases it (`_forget_stored_admin`).
        Without that, a credential revoked server-side but still present locally is returned
        forever: measured 2026-08-10, hours after four tokens were deleted by hand, every run 401'd
        on the same routes and recovery took a hand-run `git credential reject` that no message
        offered.
        """
        if self._token is not None:
            return self._token
        # `--admin-token-name` BYPASSES THE STORE, and it must, or it is a flag that lies.
        #
        # MEASURED 2026-08-12: the flag was added as the passwordless exit from the wedge, the
        # operator passed it, and `token()` returned a STORED credential before ever reading it. The
        # unwedge did nothing and said nothing -- and the stored credential was the READ-ONLY one
        # from `admin-emit`, so the run 403'd exactly as it had before the flag existed. A remedy
        # that silently no-ops is worse than an absent one: it spends the operator's one good idea.
        #
        # THE FLAG IS ONLY EVER PASSED BY SOMEONE THE STORED PATH HAS ALREADY FAILED. So it means
        # "mint fresh under this name", and the mint is stored, so the NEXT run needs no flag. It
        # says so loudly rather than silently, because a bypass nobody sees is how a script left
        # carrying this flag would mint a token per run with nobody counting.
        stored = read_credential(self.scheme, self.netloc, self.ADMIN_CRED_USER)
        if stored and self.admin_token_name:
            say(
                f'--admin-token-name given: IGNORING the stored credential and minting fresh as\n'
                f'  {self.admin_token_name!r}. The new one is stored, so drop the flag next run --\n'
                f'  left in a script it mints a token every time.'
            )
            stored = None
        if stored:
            self._token = stored
            self._token_came_from_the_store = True
            return stored
        if not self.exe:
            msg = (
                f'no admin credential stored for {self.ADMIN_CRED_USER}@{self.netloc}, and no Gitea\n'
                '  CLI on this machine. Either run this on the Gitea host with --gitea-exe, or ask\n'
                '  the host to run:  swarmctl admin-emit --machine <this machine>'
            )
            raise Fail(msg)
        if not self.admin_user:
            msg = 'this verb needs --admin-user (an existing Gitea admin) for the API'
            raise Fail(msg)
        token_name = self.admin_token_name or f'{self.ADMIN_CRED_USER}@{socket.gethostname()}'
        self._token = self.issue_token(
            self.admin_user,
            token_name,
            ['write:organization', 'write:repository', 'write:admin', 'write:user'],
        )
        # STORED BEFORE IT IS USED, so a run that crashes mid-verb still leaves the credential
        # findable. The alternative -- store on the way out -- loses it on exactly the runs that
        # fail, and the next run then mints a second token with the same name.
        store_credential(self.scheme, self.netloc, self.ADMIN_CRED_USER, self._token)
        say(f'minted and stored an admin credential for {self.ADMIN_CRED_USER}@{self.netloc}')
        say(f'  sha256={fingerprint(self._token)}  -- revoke with: swarmctl revoke --token-name {token_name}')
        return self._token

    def _forget_stored_admin(self) -> None:
        """Discard a stored admin credential the server just REFUSED. NOT a retry.

        A 401 retried with the same credential is still a 401, so this deliberately does not retry:
        the credential is erased, the call still fails with the server's own message, and the NEXT
        run mints a fresh one because nothing stored shadows it. That is the whole self-heal --
        one failed run, then recovery, instead of every run failing identically forever.

        It SAYS SO, because a credential vanishing from the store silently is indistinguishable
        from a credential that was never there, and the operator is the one who has to know which.
        """
        self._token = None
        self._token_came_from_the_store = False
        erase_credential(self.scheme, self.netloc, self.ADMIN_CRED_USER)
        say(
            f'the stored admin credential for {self.ADMIN_CRED_USER}@{self.netloc} was REFUSED by\n'
            '  the server and has been forgotten. This run still fails; the next one mints a fresh\n'
            '  credential (or, off the Gitea host, needs a new `swarmctl admin-emit` bundle).'
        )

    def admin_password(self) -> str:
        """The admin's PASSWORD, for the routes a token may not use. Never a flag, never stored.

        MEASURED 2026-08-10: `GET /users/<u>/tokens` answers 401 to token auth even when that token
        has just performed admin writes in the same run. Gitea refuses to let a token enumerate or
        delete tokens -- a circularity guard -- so the whole token-management surface (`list`,
        `revoke`, and the old self-revoke) needs the password.

        NOT A `--admin-password` FLAG: a command line is visible to every process on the box and
        lands in shell history. The environment variable is for unattended use, `--ask-password`
        for a human, and neither is written anywhere.

        **IT NEVER PROMPTS UNLESS ASKED, and that is the whole point of the flag.** This used to
        prompt whenever `sys.stdin.isatty()`, which is wrong on Windows for a reason no guard on
        stdin can fix: `getpass` reads the CONSOLE directly, so redirecting stdin to nul does not
        stop it. Measured 2026-08-10 on the fleet host -- `swarmctl list` and `swarmctl revoke`
        HUNG, twice, once even with stdin forced to `/dev/null`.

        A hang is the worst failure a 7x24 fleet can have: no exit code, no log line, no timeout,
        and a scheduler that cannot tell it from slow work. `read_credential` next door carries
        `GIT_TERMINAL_PROMPT=0` and `credential.interactive=never` for exactly this, with a
        docstring saying so -- and this function reintroduced the hazard anyway. Prompting is now
        something the operator TYPED, so an unattended run cannot reach it by accident.
        """
        if self._password is None:
            self._password = os.environ.get('SWARM_ADMIN_PASSWORD') or ''
        if not self._password and self.ask_password:
            self._password = getpass.getpass(f'Gitea password for {self.admin_user}: ')
        if not self._password:
            msg = (
                f"token management needs {self.admin_user}'s PASSWORD, not a token: Gitea answers\n"
                '  401 when a token tries to list or delete tokens. Either set SWARM_ADMIN_PASSWORD,\n'
                '  or pass --ask-password to be prompted. It is never prompted for otherwise:\n'
                '  an unattended run must fail here, not hang waiting for a console nobody is at.'
            )
            raise Fail(msg)
        return self._password

    def _call(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        *,
        allow: tuple[int, ...] = (),
        auth: str = 'token',
        raw_token: str | None = None,
        raw_basic: tuple[str, str] | None = None,
    ) -> object:
        """One HTTP round trip against the forge API.

        `http.client`, NOT `urllib.request`, and the reason is a property rather than a preference:
        urllib dispatches on the URL's SCHEME and honours `file:`, so a `--base-url` of `file:///`
        would turn every API call into a local read that still looks like a forge answering. Here
        the connection class is chosen from an allowlist in code, and there is no scheme handler to
        reach at all -- the failure mode is absent instead of guarded.

        `auth='basic'` IS NOT A PREFERENCE, it is which routes exist for which credential. See
        `admin_password`; the caller names it because the route's requirement is a property of the
        route, and a client that guessed would retry-storm on every 401.
        """
        if auth == 'raw':
            # A credential supplied by the CALLER, used to ask whether that identity can
            # authenticate AT ALL. Never falls back to the run's own token: the whole question is
            # about this one, and answering it with a different credential would always say yes.
            assert raw_token is not None
            authorization = f'token {raw_token}'
        elif auth == 'raw-basic':
            # A CALLER-SUPPLIED BASIC IDENTITY, the sibling of `raw`. Gitea grants token CREATION
            # only to the account that will own the token -- measured 1.27.1: an admin token gets
            # 401 on that route, not 403, so it is a circularity guard rather than a permission.
            # `basic` below authenticates as the ADMIN, which is right for revoking and wrong here.
            assert raw_basic is not None
            raw = f'{raw_basic[0]}:{raw_basic[1]}'.encode()
            authorization = f'Basic {base64.b64encode(raw).decode()}'
        elif auth == 'basic':
            raw = f'{self.admin_user}:{self.admin_password()}'.encode()
            authorization = f'Basic {base64.b64encode(raw).decode()}'
        else:
            authorization = f'token {self.token()}'
        data = json.dumps(body).encode() if body is not None else None
        connect = http.client.HTTPSConnection if self.scheme == 'https' else http.client.HTTPConnection
        last = ''
        for attempt in range(1, API_ATTEMPTS + 1):
            # A FRESH CONNECTION PER ATTEMPT. `http.client` leaves a connection unusable after a
            # failed exchange, so reusing one turns the retry into a second, different error.
            conn = connect(self.netloc, timeout=TIMEOUT)
            try:
                conn.request(
                    method,
                    f'{self.prefix}/api/v1{path}',
                    body=data,
                    headers={
                        'Authorization': authorization,
                        'Content-Type': 'application/json',
                    },
                )
                response = conn.getresponse()
                raw, status = response.read(), response.status
            except OSError as exc:
                last = f'{method} {path} -> unreachable: {exc}'
            else:
                if status < 400:
                    return json.loads(raw) if raw else None
                if status in allow:
                    return None
                last = f'{method} {path} -> {status}: {raw.decode(errors="replace")[:300]}'
                if status in (401, 403) and auth == 'token' and self._token_came_from_the_store:
                    self._forget_stored_admin()
                if not _is_retryable_status(status):
                    raise Fail(last)
            finally:
                conn.close()
            if attempt < API_ATTEMPTS:
                _sleep(BACKOFF_S * attempt)
        msg = f'{last} (gave up after {API_ATTEMPTS} attempts)'
        raise Fail(msg)

    def api(
        self, method: str, path: str, body: dict | None = None, *, allow: tuple[int, ...] = (), auth: str = 'token'
    ) -> None:
        """A call whose response is not read. Typed as None so a caller cannot use one by mistake."""
        self._call(method, path, body, allow=allow, auth=auth)

    def api_obj(self, method: str, path: str, body: dict | None = None) -> dict[str, Any]:
        """A call that must answer with a JSON object -- and RAISES if it does not.

        The shape check is not ceremony. Gitea answers some errors with a bare string and some
        listings with an array; a cast would let that reach a subscript far from here and surface as
        a TypeError naming a variable, not an endpoint.
        """
        payload = self._call(method, path, body)
        if not isinstance(payload, dict):
            msg = f'{method} {path} -> expected a JSON object, got {type(payload).__name__}'
            raise Fail(msg)
        return payload

    def api_list(self, method: str, path: str, *, auth: str = 'token') -> list[dict[str, Any]]:
        """A listing. An empty body and a null both mean "nothing", which is not an error."""
        payload = self._call(method, path, auth=auth)
        if payload is None:
            return []
        if not isinstance(payload, list):
            msg = f'{method} {path} -> expected a JSON array, got {type(payload).__name__}'
            raise Fail(msg)
        return payload

    def tokens_of(self, username: str) -> list[dict]:
        """BASIC AUTH, because Gitea refuses token auth on this route -- see `admin_password`."""
        return self.api_list('GET', f'/users/{urllib.parse.quote(username)}/tokens?limit=100', auth='basic')

    def revoke_token(self, username: str, token_id: int) -> None:
        self.api('DELETE', f'/users/{urllib.parse.quote(username)}/tokens/{token_id}', auth='basic')

    def teams(self) -> list[dict]:
        return self.api_list('GET', f'/orgs/{self.org}/teams?limit=100')

    def ensure_team(self, name: str, units: dict[str, str], *, was_called: str | None = None) -> tuple[int, str]:
        """The team, by id, and what had to happen: `created`, `updated` or `renamed`.

        **`renamed` IS A MIGRATION AND NOT A COSMETIC CASE.** Lookup here is BY NAME, so changing
        `TEAMS` without `was_called` would not rename anything -- it would MISS the old team and
        create a new one beside it. The org would then hold eight teams, with every membership and
        every repo attachment still on the four old ones and the four new ones empty. Nothing breaks
        at that moment, because a machine's token is already issued; it breaks at the NEXT
        provisioning, when access moves to teams nobody is in.

        A `PATCH` of the name PRESERVES THE ID, and the id is what members and repo attachments hang
        off -- which is precisely why this is a rename rather than a create-and-delete. Gitea's team
        membership and team-repo tables key on the team id, so the grants never lapse and no machine
        loses access for a moment in the middle.

        IDEMPOTENT IN BOTH DIRECTIONS: the new name is looked for FIRST, so a second run finds it and
        merely updates; a fresh org matches neither name and creates.
        """
        body = {
            'name': name,
            'description': 'swarm role -- managed by swarmctl',
            'permission': 'write',
            'includes_all_repositories': False,
            'can_create_org_repo': False,
            'units_map': units,
        }
        existing = self.teams()
        for team in existing:
            if team['name'] == name:
                # UPDATED IN PLACE. Without this a change to ROLES silently applies only to new
                # installs, and two deployments drift apart with nothing reporting it.
                self.api('PATCH', f'/teams/{team["id"]}', body)
                return team['id'], 'updated'
        # THE OLD NAME IS ONLY CONSULTED WHEN THE NEW ONE IS ABSENT. Checking it first would rename a
        # leftover old team over an already-migrated one, and Gitea would refuse the duplicate name --
        # turning a no-op second run into a hard failure.
        for team in existing:
            if was_called and team['name'] == was_called:
                self.api('PATCH', f'/teams/{team["id"]}', body)
                return team['id'], 'renamed'
        return self.api_obj('POST', f'/orgs/{self.org}/teams', body)['id'], 'created'

    def team_members(self, team_id: int) -> list[str]:
        return [m['login'] for m in self.api_list('GET', f'/teams/{team_id}/members?limit=100')]

    def team_repos(self, team_id: int) -> list[str]:
        return [r['full_name'] for r in self.api_list('GET', f'/teams/{team_id}/repos?limit=100')]

    def add_member(self, team_id: int, username: str) -> None:
        self.api('PUT', f'/teams/{team_id}/members/{urllib.parse.quote(username)}')

    def attach_repo(self, team_id: int, owner: str, repo: str) -> None:
        self.api('PUT', f'/teams/{team_id}/repos/{owner}/{repo}')

    def protections(self, owner: str, repo: str) -> list[dict]:
        return self.api_list('GET', f'/repos/{owner}/{repo}/branch_protections')

    def protect(self, owner: str, repo: str, branch: str, context: str) -> str:
        """Require the whole per-runner FAMILY of `context`, never the bare name.

        THE BARE NAME WOULD FREEZE `main` PERMANENTLY, and that is measured rather than feared.
        Every verifier now publishes under `<context>/<runner>` so two of them disagreeing about one
        tree cannot overwrite each other on one key -- so NOBODY publishes `<context>` itself.

        Gitea 1.26.4 compiles each required context as a GLOB (`services/pull/commit_status.go:31`,
        via the in-tree `modules/glob`), called with NO separator runes -- so `*` compiles to `.*`
        and DOES cross `/`. Three consequences, all read off that source at tag v1.26.4:

        * `<context>/*` matches `<context>/G-bf92f8b5`, so one entry covers the whole fleet and no
          runner ever needs naming by hand;
        * a required context matched by NOTHING yields Pending, not success -- a fully-down fleet
          BLOCKS, which is the direction a merge gate must fail in, and it is Gitea's behaviour
          rather than our arrangement;
        * a bare `<context>` is a glob with no metacharacters. It matches nothing once the
          per-runner keys landed, blocking every merge forever -- and the symptom, merges hanging,
          reads as a broken gate rather than as a rule naming a check with no producer.

        The third is why this is a suffix in the code and not a comment asking someone to add one.
        """
        body = {
            'rule_name': branch,
            'enable_push': False,
            'enable_force_push': False,
            'enable_merge_whitelist': True,
            'merge_whitelist_usernames': [USERS['integrator']],
            'enable_status_check': True,
            'status_check_contexts': [f'{context}/*'],
            'block_on_outdated_branch': True,
        }
        if any(rule.get('rule_name') == branch for rule in self.protections(owner, repo)):
            self.api('PATCH', f'/repos/{owner}/{repo}/branch_protections/{branch}', body)
            return 'updated'
        self.api('POST', f'/repos/{owner}/{repo}/branch_protections', body)
        return 'created'

    def remote_url(self, owner: str, repo: str, role: str) -> str:
        """The URL an agent must use. THE USERNAME IS LOAD-BEARING -- see the module docstring."""
        return f'{self.scheme}://{USERS[role]}@{self.netloc}/{owner}/{repo}.git'


class GitHubProvider:
    """Refuses by NAME rather than pretending, and says what a human must do instead.

    Everything except user creation transfers: GitHub has org teams, team-repo attachment, branch
    protection with required status contexts, and the same credential-helper protocol. What does not
    transfer is `provision`: **GitHub has no API to create a user.** Machine accounts are created by
    a human, or -- better -- replaced by a GitHub App, which also restores the one property Gitea
    cannot enforce (an App may hold `statuses:write` without `contents:write`).

    Unimplemented rather than half-implemented, on purpose: a partial client would fail rarely and
    silently, and none of the Gitea measurements transfer to it.
    """

    name = 'github'

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        msg = (
            'the GitHub provider is not implemented.\n'
            '  provision : impossible via API -- create the four machine users by hand, or use a\n'
            '              GitHub App (preferred: it can hold statuses:write WITHOUT contents:write,\n'
            '              which is the boundary Gitea cannot enforce).\n'
            '  onboard/verify/enroll : portable in principle, unwritten and UNMEASURED. Nothing\n'
            '              measured against Gitea carries over -- re-measure before depending on it.'
        )
        raise Fail(msg)


def build_provider(args: argparse.Namespace) -> GiteaProvider:
    if args.provider == 'github':
        GitHubProvider()
    return GiteaProvider(
        args.base_url,
        args.org,
        getattr(args, 'gitea_exe', None),
        getattr(args, 'admin_user', None),
        ask_password=getattr(args, 'ask_password', False),
        admin_token_name=getattr(args, 'admin_token_name', None),
    )


# --------------------------------------------------------------------------- credentials


def read_credential(scheme: str, host: str, username: str) -> str | None:
    """This role's token, from the ENVIRONMENT or the swarm's own store. Never the operator's vault.

    THE VAULT IS NOT CONSULTED, and that is the repair rather than an omission. See
    `agent_swarm.credentials`: a credential store holds one entry per host, four roles share one
    host, so this tool's writes NECESSARILY overwrote whatever the human had -- measured 2026-08-11
    as a fleet-wide install failure four days and two machines away from its cause.

    A missing credential is a QUIET absence -- the caller decides what to do about it -- and it is
    never a prompt.
    """
    return credentials.resolve_token(scheme, host, username)


def store_credential(scheme: str, host: str, username: str, token: str) -> None:
    """Persist a role token into the SWARM's store, read it back, and refuse if it did not keep.

    THIS NO LONGER TOUCHES `git credential approve`. The read-back it used to justify survives the
    move intact and for the same measured reason (2026-08-10: Git Credential Manager silently
    dropped two of four credentials while `enroll` reported all four stored, and Gitea keeps only a
    hash, so the plaintext was gone permanently and un-remintable). A write that cannot fail is
    indistinguishable from one that works, and a file write can fail too.
    """
    try:
        credentials.store_token(scheme, host, username, token)
    except (PermissionError, RuntimeError) as exc:
        raise Fail(str(exc)) from None


def erase_credential(scheme: str, host: str, username: str) -> None:
    credentials.forget_token(scheme, host, username)


# --------------------------------------------------------------------------- verbs


def cmd_list(provider: GiteaProvider, args: argparse.Namespace) -> int:
    say(f'{provider.name}  {provider.base_url}  org={provider.org}')
    say('\nUSERS')
    for username in USERS.values():
        if provider.exe:
            present = 'present' if provider.user_exists(username) else 'MISSING'
        else:
            present = '(needs --gitea-exe to check)'
        # UNKNOWN IS NOT EMPTY. Token listing needs the admin's PASSWORD (see `admin_password`), and
        # `list` is otherwise a read-only overview that must still work without one. Printing '-'
        # when we could not look would say "this user has no tokens" -- which is what an operator
        # would act on before re-issuing credentials that already exist.
        try:
            tokens = provider.tokens_of(username) if provider.admin_user else []
            names = ', '.join(sorted(t['name'] for t in tokens)) or '-'
        except Fail:
            names = '(not read: needs SWARM_ADMIN_PASSWORD)'
        say(f'  {username:<18} {present:<28} tokens: {names}')

    say('\nTEAMS')
    by_name = {t['name']: t for t in provider.teams()}
    for role, (units, _scopes) in ROLES.items():
        team_name = TEAMS[role]
        team = by_name.get(team_name)
        if not team:
            say(f'  {team_name:<18} MISSING')
            continue
        members = provider.team_members(team['id'])
        repos = provider.team_repos(team['id'])
        drift = {k: v for k, v in units.items() if (team.get('units_map') or {}).get(k) != v}
        say(
            f'  {team_name:<18} id={team["id"]:<4} members={members or "-"}  repos={repos or "-"}'
            f'{"  UNIT DRIFT: " + json.dumps(drift) if drift else ""}'
        )

    if args.repo:
        owner, repo = split_repo(args.repo)
        say(f'\nPROTECTION on {owner}/{repo}')
        rules = provider.protections(owner, repo)
        if not rules:
            say('  none')
        for rule in rules:
            say(
                f'  {rule.get("rule_name")}: push={rule.get("enable_push")} '
                f'force={rule.get("enable_force_push")} '
                f'merge_whitelist={rule.get("merge_whitelist_usernames")} '
                f'status_check={rule.get("enable_status_check")} '
                f'contexts={rule.get("status_check_contexts")}'
            )
    return 0


def cmd_provision(provider: GiteaProvider, _args: argparse.Namespace) -> int:
    say('users')
    for username in USERS.values():
        if provider.user_exists(username):
            say(f'  {username:<18} already present')
        else:
            provider.create_user(username)
            say(f'  {username:<18} created')

    say('\nteams (units updated in place, so a ROLES change reaches existing installs; a team still')
    say('carrying its pre-`Swarm-` name is RENAMED, keeping its id, its members and its repos)')
    for role, (units, _scopes) in ROLES.items():
        team_name = TEAMS[role]
        team_id, what = provider.ensure_team(team_name, units, was_called=_old_name_of(role))
        if USERS[role] not in provider.team_members(team_id):
            provider.add_member(team_id, USERS[role])
            what += ', member added'
        say(f'  {team_name:<18} id={team_id:<4} {what}')
    say('\nNo repository is attached yet -- run `onboard --repo OWNER/NAME`.')
    return 0


def cmd_onboard(provider: GiteaProvider, args: argparse.Namespace) -> int:
    # BEFORE ANYTHING TOUCHES THE SERVER. With the built-in check name gone, an unset context would
    # reach `protect` as an empty required status -- protection that looks enabled and requires no
    # check, the silent-permission shape the branch-protection block below argues against. Refusing
    # here rather than there also stops a half-onboarded repo: teams are attached first, so a late
    # failure leaves server state nobody asked for.
    if getattr(args, 'protect', False) and not getattr(args, 'status_context', ''):
        msg = (
            'no status context: --protect needs the name of the check a merge waits on. Set '
            '--status-context, SWARM_STATUS_CONTEXT, or `swarmctl config --status-context <name>`. '
            'It is whatever the VERIFIER publishes; the project that publishes it owns the name.'
        )
        raise SystemExit(msg)
    owner, repo = split_repo(args.repo)
    say(f'onboarding {owner}/{repo}')
    by_name = {t['name']: t for t in provider.teams()}
    for team_name in TEAMS.values():
        team = by_name.get(team_name)
        if not team:
            msg = f'team {team_name} does not exist -- run `provision` first'
            raise Fail(msg)
        if f'{owner}/{repo}' in provider.team_repos(team['id']):
            say(f'  {team_name:<18} already attached')
        else:
            provider.attach_repo(team['id'], owner, repo)
            say(f'  {team_name:<18} attached')

    say('\nremote URLs -- THE USERNAME IS REQUIRED, not decoration:')
    for role in ('agent', 'verifier', 'integrator'):
        say(f'  {role:<11} {provider.remote_url(owner, repo, role)}')
    say('  Four credentials share one host; git picks between them on the username in the URL.')

    say('\nbranch protection')
    if args.protect:
        what = provider.protect(owner, repo, args.branch, args.status_context)
        say(
            f'  {args.branch}: {what} -- no direct push, no force push, '
            f'merge={USERS["integrator"]}, required status "{args.status_context}"'
        )
        say('  A REQUIRED context makes an ABSENT status block a merge. A rule that only rejects a')
        say('  FAILING check reads "the verifier never ran" as permission to proceed.')
    else:
        say(f'  SKIPPED (no --protect). Enabling it before anything publishes "{args.status_context}"')
        say(f'  freezes {args.branch}: every merge waits on a check nothing produces.')
    return 0


def _enroll_tokens(provider: GiteaProvider, machine: str) -> dict[str, str]:
    return {
        USERS[role]: provider.issue_token(USERS[role], f'{USERS[role]}@{machine}', scopes)
        for role, (_units, scopes) in ROLES.items()
    }


def required_repositories(args: argparse.Namespace) -> list[str]:
    """Which repositories every issued identity MUST be able to read.

    IT IS THE CALLER'S DATA, NEVER A CONSTANT HERE. This package onboards machines for ANY project;
    a built-in list would be `DEFAULT_REPO` under a new spelling -- a vendor-neutral layer holding
    one project's fact, invisible exactly because the default works.
    `tests/test_this_package_names_no_specific_project.py` refuses it, and would be right to.

    It reaches this CLI the way every other deployment fact does: `--require-repo` (repeatable),
    `SWARM_REQUIRED_REPOS`, or the stored config, comma-separated.

    THE FALLBACK IS THE REPO BEING ONBOARDED, not an empty list. An identity minted for a repository
    it cannot read is the exact defect this check exists to catch, so the one repository already
    named on the command line is the minimum honest requirement -- and it means the check has teeth
    on day one, before anybody has configured anything.
    """
    declared = list(getattr(args, 'require_repo', None) or ())
    if not declared and getattr(args, 'required_repos', ''):
        declared = [part.strip() for part in args.required_repos.split(',') if part.strip()]
    if not declared and getattr(args, 'repo', ''):
        declared = [args.repo]
    return declared


def _refuse_identities_without_grants(provider: GiteaProvider, issued: dict[str, str], required: list[str]) -> None:
    """VERIFY AT ONBOARDING. A missing grant must fail HERE, in the second it is created.

    USER DIRECTIVE 2026-08-11, and it is the systemic half of a diagnosis rather than a new idea:
    「onboarding 时验证,而不是安装时发现」. Which repositories an identity must be able to read is
    DATA, and it was nothing at all -- so a role account minted without a grant looked perfectly
    healthy, and the absence surfaced MONTHS LATER, on a different machine, as a dependency
    RESOLUTION error with a 404 buried in it. The install-side pre-flight catches it at the far end;
    this catches it at the near one, which is the second where it is cheap and where the person who
    can fix it is already at a keyboard.

    UNANSWERABLE IS NOT REFUSED, and that separation is the property this must not lose. A forge that
    is down produces the same non-success as a missing grant, and an onboarding that fails closed on
    transience is one an operator learns to bypass -- so it is REPORTED and does not fail the run.

    THE TOKENS ARE ALREADY STORED WHEN THIS RUNS, deliberately. A token exists in plaintext exactly
    once (Gitea keeps only a hash), and its name cannot be reused, so discarding one to signal a
    permissions problem would destroy something unrecoverable to report something fixable. Refuse
    loudly, keep the credential.

    Raises:
        Fail: at least one identity was REFUSED read on at least one required repository.

    """
    if not required:
        return
    say(f'\ngrants -- every identity must READ: {", ".join(required)}')
    problems = []
    for username, token in issued.items():
        refused, unanswerable = credentials.unreadable_repositories(required, token, call=provider._call)
        if refused:
            say(f'  FAIL {username:<18} refused on {", ".join(refused)}')
            problems += [f'{username} cannot read {name}' for name in refused]
        if unanswerable:
            # NOT a problem, and the wording says which. Reporting "not checked" as a pass is how a
            # silent 403 stayed invisible once; reporting it as a failure is how a check gets
            # bypassed. It is neither.
            say(f'  ?    {username:<18} not answered for {", ".join(unanswerable)} (forge unreachable)')
        if not refused and not unanswerable:
            say(f'  ok   {username}')
    if problems:
        msg = (
            'these identities were created WITHOUT the read access they are required to have:\n'
            + '\n'.join(f'  - {problem}' for problem in problems)
            + '\n  The credentials ARE stored -- a token exists in plaintext once, so none was\n'
            '  discarded. Grant the missing access on the forge (a team, or a collaborator entry)\n'
            '  and re-run `verify`. Refusing HERE is the point: found months later, this same gap\n'
            '  surfaces on another machine as a dependency resolution error nobody can read.'
        )
        raise Fail(msg)


def cmd_enroll(provider: GiteaProvider, args: argparse.Namespace) -> int:
    issued = _enroll_tokens(provider, args.machine)
    for username, token in issued.items():
        store_credential(provider.scheme, provider.netloc, username, token)
        say(f'  {username:<18} stored  sha256={fingerprint(token)}')
    say(f'\n{len(issued)} credentials stored on this machine for {provider.netloc}.')
    say('Losing this machine means revoking exactly these four: `revoke --machine ' + args.machine + '`.')
    _refuse_identities_without_grants(provider, issued, required_repositories(args))
    return 0


def write_secret_file(path: Path, payload: dict) -> None:
    """A bundle of live plaintext credentials, owner-only. ONE DEFINITION, in `credentials`.

    The hardening -- and the measurement behind it, where NTFS quietly ignored a POSIX mode and left
    four live role credentials readable under a line asserting the opposite -- lives with the store
    that needs the same guarantee. Two copies of a permission scheme is the duplicated-scheme defect,
    and the copy that drifts is the one holding secrets.
    """
    try:
        credentials.write_secret_file(path, payload)
    except PermissionError as exc:
        raise Fail(str(exc)) from None


def cmd_emit(provider: GiteaProvider, args: argparse.Namespace) -> int:
    """A bundle exists because the transfer CANNOT be eliminated -- only named and minimised.

    Issuing a token needs either this host's CLI or the user's password; Gitea's token endpoint does
    not accept an admin acting as another user. So a workstation that is not the Gitea host cannot
    mint its own. The bundle is one file, for one machine, consumed once and deleted by `consume`.
    """
    issued = _enroll_tokens(provider, args.machine)
    # CHECKED ON THE HOST, WHERE THE FIX IS. `emit` runs where the Gitea CLI is, so a missing grant
    # is refused beside the administrator who can grant it -- rather than on the far machine, which
    # has the bundle and no way to change a permission.
    _refuse_identities_without_grants(provider, issued, required_repositories(args))
    path = Path(args.out or f'swarm-enroll-{args.machine}.json').resolve()
    payload = {'scheme': provider.scheme, 'host': provider.netloc, 'machine': args.machine, 'credentials': issued}
    write_secret_file(path, payload)
    say(f'wrote {path} (owner-readable only)')
    for username, token in issued.items():
        say(f'  {username:<18} sha256={fingerprint(token)}')
    say('\nMove it to ' + args.machine + ' and run:  swarmctl consume --bundle <file>')
    say('It contains live credentials in plaintext. `consume` deletes it; if you abandon the')
    say('transfer, run `revoke --machine ' + args.machine + '` rather than leaving it on disk.')
    return 0


def cmd_consume(_provider: GiteaProvider | None, args: argparse.Namespace) -> int:
    with Path(args.bundle).open(encoding='utf-8') as handle:
        payload = json.load(handle)
    for username, token in payload['credentials'].items():
        store_credential(payload['scheme'], payload['host'], username, token)
        say(f'  {username:<18} stored  sha256={fingerprint(token)}')
    Path(args.bundle).unlink()
    say(f'\nstored {len(payload["credentials"])} credentials; bundle deleted.')
    return 0


def _revocable_users(provider: GiteaProvider) -> list[str]:
    """The four role users AND the admin, because swarmctl issues tokens under BOTH.

    MEASURED 2026-08-10 on the live host: three `swarmctl-ephemeral@DUIPEZZTZ-*` admin tokens were
    standing, and NO selector of `revoke` could reach them -- the loop walked `USERS.values()` only,
    so the one account this tool mints admin credentials under was the one account it never swept.
    A tool that creates a class of credential it cannot clean up leaves that class to accumulate,
    and these are ADMIN tokens.

    The admin is appended, never substituted, and only when one is configured: an off-host run has
    no admin user and must still be able to revoke role tokens.
    """
    users = list(USERS.values())
    if provider.admin_user and provider.admin_user not in users:
        users.append(provider.admin_user)
    return users


def cmd_revoke(provider: GiteaProvider, args: argparse.Namespace) -> int:
    """Revoke by machine, by exact name, by leftover ephemeral, or everything swarmctl did NOT issue.

    THE SELECTOR MATTERS BECAUSE THE DANGEROUS TOKENS ARE THE ONES WITHOUT A MACHINE. Every token
    swarmctl issues is named `user@machine`; anything else was created by hand and belongs to nobody
    in particular -- including the four created before this tool existed, whose plaintext ended up
    in a chat transcript. `--unmanaged` is exactly that set, and it is a rule rather than a list, so
    it stays correct as tokens come and go.

    `--ephemeral` CLEANS UP AFTER A DESIGN THAT IS GONE. swarmctl used to mint one admin token per
    run and revoke it at exit; the revoke went through `GET/DELETE /users/<u>/tokens`, and Gitea
    answers 401 to TOKEN auth on those routes -- a token may not enumerate or delete tokens -- so it
    ALWAYS failed and every run deposited a standing `write:admin` credential. `token()` now mints
    ONE and stores it, so no new leftovers are produced; this selector exists for the servers that
    already carry them, and nothing else reaches them (they are named `user@machine`, so
    `--unmanaged` calls them managed, and their machine half carries a random suffix, so `--machine`
    does not match).

    It is not retirement-registry material: the SPELLING is gone from the code, but the leftovers are data
    on live servers, and a cleanup for existing state is not a compatibility shim.

    Refuses to run with no selector. A revoke that defaults to "everything" is a foot-gun; one that
    defaults to "nothing" wastes a command. Neither is as good as saying so.
    """
    selectors = [
        bool(args.machine_given),
        bool(args.token_name),
        bool(args.unmanaged),
        bool(args.ephemeral),
        bool(args.all_tokens),
    ]
    if sum(selectors) != 1:
        msg = (
            'pick exactly one selector:\n'
            '  --machine NAME      tokens named *@NAME  (one machine, no other touched)\n'
            '  --token-name NAME   one exact name\n'
            '  --unmanaged         every token NOT named user@machine -- i.e. not issued by swarmctl\n'
            '  --ephemeral         leftover swarmctl-ephemeral@* admin tokens (see below)\n'
            '  --all --confirm REVOKE-ALL   every token of all four users'
        )
        raise Fail(msg)
    if args.all_tokens and args.confirm != 'REVOKE-ALL':
        msg = '--all needs --confirm REVOKE-ALL: it locks out every machine at once.'
        raise Fail(msg)

    def wanted(name: str) -> bool:
        if args.machine_given:
            return name.endswith(f'@{args.machine}')
        if args.token_name:
            return name == args.token_name
        if args.unmanaged:
            return '@' not in name
        if args.ephemeral:
            return name.startswith('swarmctl-ephemeral@')
        return True

    # THE CREDENTIAL THIS RUN IS USING IS SPARED unless it was named EXACTLY.
    #
    # `--machine NAME` matches `*@NAME`, and this machine's own admin credential is
    # `swarmctl-admin@NAME` -- so `revoke --machine <this box>`, the obvious way to retire a
    # machine's stale role tokens, also revokes the credential the revoke is authenticating with.
    # It would self-destruct part way through, leaving the rest of the sweep 401 and the operator
    # locked out of the verb they were running.
    #
    # Spared rather than refused: retiring a machine's role tokens is a legitimate, common act and
    # should not need a flag. `--token-name <that exact name>` still revokes it, because naming it
    # is unambiguous intent -- an operator retiring the whole machine says so precisely.
    self_named = f'{provider.ADMIN_CRED_USER}@{socket.gethostname()}'
    revoked = 0
    for username in _revocable_users(provider):
        for token in provider.tokens_of(username):
            if not wanted(token['name']):
                continue
            if token['name'] == self_named and args.token_name != self_named:
                say(f'  {username:<18} SPARED {token["name"]} -- this run is using it')
                say('         name it exactly with --token-name to revoke it anyway')
                continue
            provider.revoke_token(username, token['id'])
            say(f'  {username:<18} revoked {token["name"]}')
            revoked += 1
        if args.erase_local:
            erase_credential(provider.scheme, provider.netloc, username)
    say(f'\nrevoked {revoked} token(s).')
    if not revoked:
        say('Nothing matched. `list` shows the names actually on the server.')
    elif args.machine_given:
        say('Other machines are untouched -- that is the point of one token per (role, machine).')
    return 0


def cmd_admin_emit(provider: GiteaProvider, args: argparse.Namespace) -> int:
    """A READ-ONLY admin credential for another machine, so `list`/`verify` work off the host.

    READ-ONLY ON PURPOSE. `verify` needs to read teams, members, repo attachment and branch
    protection -- nothing that changes them. Shipping a write-capable admin token to every
    workstation would mean any of them could rewrite permissions, which is a far larger blast radius
    than the four role tokens it is meant to sit beside.

    So provisioning stays on the host and only the ABILITY TO LOOK travels.
    """
    token = provider.issue_token(
        provider.admin_user or '',
        f'{GiteaProvider.ADMIN_CRED_USER}@{args.machine}',
        ['read:organization', 'read:repository', 'read:issue', 'read:user'],
    )
    payload = {
        'scheme': provider.scheme,
        'host': provider.netloc,
        'machine': args.machine,
        'credentials': {GiteaProvider.ADMIN_CRED_USER: token},
    }
    path = Path(args.out or f'swarm-admin-{args.machine}.json').resolve()
    write_secret_file(path, payload)
    say(f'wrote {path} (owner-readable only)  sha256={fingerprint(token)}')
    say(f'On {args.machine}:  swarmctl consume --bundle <file>')
    say('It is READ-ONLY: enough for `list` and `verify`, not enough to change any permission.')
    return 0


def cmd_prune_issues(provider: GiteaProvider, args: argparse.Namespace) -> int:
    """Delete CLOSED work items older than a cutoff. Dry-run unless `--yes`.

    WHY DELETING IS SAFE, AND WHY THE CODE SAYS IT RATHER THAN A COMMENT. A work item carries WORK
    STATE -- claimed by whom, answered how -- and work state is the one fact that must be contended,
    which is why it lives on a server at all. It is NOT the record: a verdict's record is the
    consumer's, immutable and keyed by environment, and the narrative's record is the operator's
    notes. A CLOSED item's fact has already expired, so removing it destroys nothing -- and that is the
    architecture's own claim ("a projection never writes back, the record lives elsewhere") paying
    for itself for the first time.

    MEASURED 2026-08-10: 1934 items, of which ONE was open. The queue was never the problem; the
    ARCHIVE was, and an archive is precisely what the two homes above already are.

    IT WRITES DOWN WHAT IT DELETED, and that is not bookkeeping. "Nothing of value is lost" is a
    claim, and a claim the tool can make checkable for the cost of one file should be. The manifest
    holds number, title, closed-at and labels for every item removed, so a later question -- was
    THAT one really worthless? -- has an answer that is not someone's memory.

    DRY-RUN IS THE DEFAULT because deletion is irreversible on a shared host serving every lane at
    once. `--yes` is the whole confirmation, and the count and the oldest/newest number are printed
    before it is honoured, so the operator confirms a MEASUREMENT rather than an intention.

    OPEN ITEMS ARE NEVER TOUCHED, whatever their age. An open item is either live work or a leaked
    claim, and both are things to look at rather than to delete: the first is someone's job in
    flight, the second is a defect this would hide.
    """
    cutoff = time.time() - args.older_than_days * 86400.0
    keep = frozenset(args.keep_label or ())
    doomed, kept_by_label, too_young = [], 0, 0
    page = 1
    while True:
        batch = provider.api_list('GET', f'/repos/{args.repo}/issues?state=closed&limit=50&page={page}')
        if not batch:
            break
        for item in batch:
            if any(label.get('name') in keep for label in item.get('labels') or ()):
                kept_by_label += 1
                continue
            closed_at = item.get('closed_at') or ''
            stamp = _parse_iso8601(closed_at)
            if stamp is None or stamp > cutoff:
                too_young += 1
                continue
            doomed.append(item)
        page += 1
    say(
        f'{len(doomed)} closed item(s) older than {args.older_than_days}d; {too_young} newer, {kept_by_label} label-kept'
    )
    if not doomed:
        return 0
    numbers = sorted(i['number'] for i in doomed)
    say(f'  #{numbers[0]} .. #{numbers[-1]}')
    if not args.yes:
        say('  DRY RUN. Re-run with --yes to delete, after reading the two numbers above.')
        return 0
    manifest = Path(args.manifest) if args.manifest else Path('output') / f'pruned-issues-{int(time.time())}.jsonl'
    manifest.parent.mkdir(parents=True, exist_ok=True)
    with manifest.open('w', encoding='utf-8') as handle:
        for item in doomed:
            handle.write(
                json.dumps(
                    {
                        'number': item['number'],
                        'title': item.get('title', ''),
                        'closed_at': item.get('closed_at'),
                        'labels': [label.get('name') for label in item.get('labels') or ()],
                    },
                    ensure_ascii=False,
                )
                + chr(10)
            )
    say(f'  wrote {manifest}')
    gone, refused = 0, []
    for item in doomed:
        try:
            provider.api('DELETE', f'/repos/{args.repo}/issues/{item["number"]}')
            gone += 1
        except Fail as exc:
            refused.append(f'#{item["number"]}: {exc}')
            if len(refused) >= 3:
                break
    say(f'  deleted {gone}')
    if refused:
        # NOT swallowed: deletion needs OWNER or ADMIN -- measured, the four role credentials get
        # 403 -- and a run that deleted nothing while printing a count is the shape this repo hunts.
        for line in refused:
            say(f'  REFUSED {line}')
        msg = f'{len(refused)} deletion(s) refused; the manifest at {manifest} lists what was selected'
        raise Fail(msg)
    return 0


def _parse_iso8601(text: str) -> float | None:
    """Gitea's timestamp as an epoch, or None. RETURNS None RATHER THAN GUESSING a time.

    A missing or unparseable `closed_at` must not read as "long ago" -- that is unknown becoming
    old enough to delete, and it fails in the irreversible direction.
    """
    if not text:
        return None
    try:
        return datetime.datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def cmd_verify(provider: GiteaProvider, args: argparse.Namespace) -> int:
    """Read back what the server ACTUALLY says, and name what this cannot check from here.

    THE CREDENTIALS SECTION RUNS FIRST, AND NEEDS NO ADMIN CREDENTIAL. It used to run last, after
    the team and membership reads -- which do need one. Measured 2026-08-10 on the fleet's SECOND
    machine, minutes after enrolment: `verify` aborted with "no admin credential stored", so the one
    question a freshly enrolled machine actually has -- do MY four credentials work? -- was the one
    question it could not ask.

    The ordering is not cosmetic; it decides whether this verb is usable on the machines the fleet
    is made of. Org-shaped checks are the HOST's business and degrade with a reason; the credential
    check is the machine's own and needs nothing but the credentials.
    """
    owner, repo = split_repo(args.repo)
    problems = []

    # ASKED FIRST because it is the only part a non-host machine can answer, and the only part it
    # needs. Everything below reads CONFIGURATION -- teams, membership, attachment -- all of which
    # was green on 2026-08-10 while every role account was refused by the server.
    say('CREDENTIALS on this machine')
    for username in USERS.values():
        works = provider.credential_works(username, owner, repo)
        if works is None:
            # UNKNOWN, NOT OK. No credential stored here, so this machine cannot answer for the
            # server -- and reporting absence as health is how a silent 403 stayed invisible once.
            say(f'  {username:<18} not stored here -- cannot be checked from this machine')
        elif works:
            say(f'  ok   {username}')
        else:
            say(f'  FAIL {username} -- the server refuses this token')
            problems.append(f'{username} cannot authenticate (its token is refused)')

    try:
        by_name = {t['name']: t for t in provider.teams()}
    except Fail as failure:
        # NOT counted as a configuration problem: it is not a fault in the deployment, it is this
        # machine lacking an admin credential -- the NORMAL state everywhere except the host.
        #
        # BUT IT IS NOT A PASS EITHER, AND THAT WAS THE BUG. This arm used to print
        # `configuration problems: none` and return 0 -- so `verify` answered "the fleet is
        # configured correctly" on a run where it never looked at a single team. The `not checked
        # from here` line above was already printed, and it changed nothing: A LOG IS NOT A SIGNAL,
        # and the test is whether the CALLER can tell. A caller reads the exit code.
        #
        # MEASURED 2026-08-12 on the Gitea host: exactly this arm fired (the admin mint failed on a
        # taken token name), and `verify` exited 0 saying `configuration problems: none`. Three
        # quarters of what it claims to verify -- teams, membership, branch protection -- had not
        # been read at all.
        #
        # THREE-VALUED, because "could not check" is not "checked and fine" and not "checked and
        # broken". Collapsing it into either one is a lie in a different direction: 0 says the
        # deployment is good, 1 accuses a deployment nobody looked at.
        say()
        say(f'TEAMS and PROTECTION: not checked from here -- {failure}')
        say()
        say(f'configuration problems: {len(problems) if problems else "none"} AMONG WHAT WAS CHECKED')
        say('  INCOMPLETE: teams, membership and branch protection were NOT read. This is not a pass.')
        say('  Run this on the Gitea host, or get a credential with:  swarmctl admin-emit --machine <this box>')
        return 1 if problems else EXIT_INCOMPLETE

    for role, (units, _scopes) in ROLES.items():
        team_name = TEAMS[role]
        team = by_name.get(team_name)
        if not team:
            problems.append(f'team {team_name} missing')
            continue
        if USERS[role] not in provider.team_members(team['id']):
            problems.append(f'{USERS[role]} is not in {team_name}')
        if f'{owner}/{repo}' not in provider.team_repos(team['id']):
            problems.append(f'{owner}/{repo} not attached to {team_name}')
        for unit, want in units.items():
            got = (team.get('units_map') or {}).get(unit)
            if got != want:
                problems.append(f'{team_name} {unit}={got!r}, expected {want!r}')

    rules = {r.get('rule_name'): r for r in provider.protections(owner, repo)}
    rule = rules.get(args.branch)
    if not rule:
        say(f'protection: NONE on {args.branch} (expected before step 4 of the landing order)')
    else:
        checks = {
            'direct push blocked': rule.get('enable_push') is False,
            'force push blocked': rule.get('enable_force_push') is False,
            'merge whitelist is the integrator': rule.get('merge_whitelist_usernames') == [USERS['integrator']],
            'status check required': rule.get('enable_status_check') is True,
            f'context {args.status_context!r} required': args.status_context
            in (rule.get('status_check_contexts') or []),
        }
        say(f'protection on {args.branch}:')
        for label, ok in checks.items():
            say(f'  {"ok  " if ok else "FAIL"} {label}')
        problems += [label for label, ok in checks.items() if not ok]

    say('\nconfiguration problems: ' + (str(len(problems)) if problems else 'none'))
    for problem in problems:
        say(f'  - {problem}')

    say('\nNOT CHECKED FROM HERE, and each needs a real attempt rather than a config read:')
    say('  * does an ABSENT status actually block a merge (not merely a failing one)')
    # DERIVED, not typed: operator output naming an account is a spelling like any other, and this
    # one would have gone on naming `swarm-agent` after a prefix change -- telling the human to test
    # an account that no longer exists. The literal scanner found this; I had counted four.
    say(f'  * is {roles.ACCOUNTS["agent"]} actually refused a merge')
    say('  * do the issued token scopes match what was requested')
    say('  A config that LOOKS right is the state this whole design exists to distrust.')
    return 1 if problems else 0


def cmd_destroy(provider: GiteaProvider, args: argparse.Namespace) -> int:
    if args.confirm != 'DESTROY':
        msg = 'refusing: pass --confirm DESTROY. This purges four users and their history.'
        raise Fail(msg)
    for team in provider.teams():
        if team['name'] in set(TEAMS.values()) | set(LEGACY_TEAMS):
            provider.api('DELETE', f'/teams/{team["id"]}', allow=(404,))
            say(f'  team {team["name"]} deleted')
    for username in USERS.values():
        if provider.user_exists(username):
            provider.delete_user(username)
            say(f'  user {username} deleted')
        erase_credential(provider.scheme, provider.netloc, username)
    say('\ndone. Local credentials for this host were erased too.')
    return 0


def split_repo(value: str) -> tuple[str, str]:
    if value.count('/') != 1:
        msg = f'--repo must be OWNER/NAME, got {value!r}'
        raise Fail(msg)
    owner, name = value.split('/')
    return owner, name


# --------------------------------------------------------------------------- settings

#: Config keys, in the same spelling as the CLI flags they back. Anything not here cannot be
#: persisted -- `--confirm` or `--all` remembered across runs would be a loaded gun.
CONFIG_KEYS = (
    'provider',
    'base_url',
    'org',
    'repo',
    'gitea_exe',
    'admin_user',
    'branch',
    'status_context',
    'required_repos',
)


def config_path() -> str:
    """Per-user, per-machine, and NOT next to the script.

    A file beside the script travels with the checkout: clone the repo on a second box and you
    inherit the first box's admin account and Gitea path. These values are properties of the
    MACHINE, so they live where the machine keeps its user config.
    """
    if os.name == 'nt':
        base = Path(os.environ.get('APPDATA') or Path.home())
    else:
        base = Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config')
    return str(base / 'swarmctl' / 'config.json')


def load_config() -> dict[str, str]:
    try:
        with Path(config_path()).open(encoding='utf-8') as handle:
            loaded = json.load(handle)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        msg = f'{config_path()} is unreadable: {exc}'
        raise Fail(msg) from None
    return {k: str(v) for k, v in loaded.items() if k in CONFIG_KEYS}


def save_config(values: dict[str, str]) -> None:
    path = config_path()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with Path(path).open('w', encoding='utf-8') as handle:
        json.dump(values, handle, indent=2, sort_keys=True)
        handle.write('\n')
    # No token is ever written here, but the file names an admin account; keep it owner-only where
    # the platform has the concept.
    if os.name != 'nt':
        Path(path).chmod(stat.S_IRUSR | stat.S_IWUSR)


def cmd_config(_provider: GiteaProvider | None, args: argparse.Namespace) -> int:
    """Persist whatever settings were TYPED. Nothing else -- a config verb with its own vocabulary
    would drift from the flags it configures; here they are the same names by construction.
    """
    stored = load_config()
    typed = {key: getattr(args, key) for key in CONFIG_KEYS if key in args.typed}
    if args.target:
        typed['admin_user'] = args.target  # `swarmctl config MingyangBao`, the overwhelmingly common case
    if typed:
        stored.update(typed)
        save_config(stored)
    say(f'config  {config_path()}')
    for key in CONFIG_KEYS:
        value = stored.get(key)
        marker = ' <- set now' if key in typed else ''
        say(f'  {key:<15} {value or "(unset)"}{marker}')
    if not typed:
        say('\nnothing changed. set values by typing them:  swarmctl config <admin-user>')
        say('or any flag:  swarmctl config --base-url http://host:9000 --org MyOrg')
    return 0


#: verb -> the attribute its single positional fills. `revoke` is absent: its positional is a
#: SELECTOR with three meanings, handled explicitly below rather than smuggled through this table.
POSITIONAL = {
    'onboard': 'repo',
    'verify': 'repo',
    'prune-issues': 'repo',
    'emit': 'machine',
    'admin-emit': 'machine',
    'consume': 'bundle',
    'destroy': 'confirm',
}


def apply_positional(args: argparse.Namespace) -> None:
    if args.verb == 'revoke' and args.target:
        if args.target == 'unmanaged':
            args.unmanaged = True
        elif args.target == 'all':
            args.all_tokens = True
            # NOT auto-confirmed. The positional says WHAT to revoke; --confirm says you meant it,
            # and a shorthand that supplies its own confirmation confirms nothing.
        else:
            args.machine, args.machine_given = args.target, True
        return
    key = POSITIONAL.get(args.verb)
    if key and args.target:
        setattr(args, key, args.target)
        if key == 'machine':
            args.machine_given = True


# --------------------------------------------------------------------------- main

#: verb -> (handler, one-line help). The provider parameter is OPTIONAL for every handler because
#: two verbs genuinely have no forge to talk to: `config` writes a local file and `consume` stores a
#: bundle this machine was handed. Declaring that in the type is what lets `main` pass None without
#: a suppression -- the alternative spelling asserted a provider exists and then said "ignore this".
VERBS: dict[str, tuple[Callable[[GiteaProvider | None, argparse.Namespace], int], str]] = {
    'config': (cmd_config, "show or set this machine's settings"),
    'list': (cmd_list, 'what exists now, read from the server'),
    'provision': (cmd_provision, 'ensure the four users and teams'),
    'onboard': (cmd_onboard, 'make a repo iterable by the swarm'),
    'enroll': (cmd_enroll, "issue and store THIS machine's tokens"),
    'emit': (cmd_emit, "issue another machine's tokens into a one-time bundle"),
    'consume': (cmd_consume, 'store a bundle on this machine, then delete it'),
    'revoke': (cmd_revoke, 'revoke by machine, by name, or everything swarmctl did not issue'),
    'admin-emit': (cmd_admin_emit, 'a READ-ONLY admin credential for another machine'),
    'verify': (cmd_verify, 'read back what the server enforces'),
    'prune-issues': (cmd_prune_issues, 'delete CLOSED work items older than a cutoff'),
    'destroy': (cmd_destroy, 'remove the users and teams (guarded)'),
}


def parse_argv(argv: list[str] | None = None) -> argparse.Namespace:
    """Everything that decides WHAT a run will do, with nothing that reaches the network.

    Separated from `main` so the settings precedence and the verb shorthands are testable directly.
    They were previously assembled by a shell wrapper, where they could only be exercised by running
    the real thing -- and three defects lived there undisturbed as a result.
    """
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    # built-in < config file < environment. The command line beats all three by being parsed last.
    stored = load_config()

    def setting(key: str, fallback: str | None = None) -> str | None:
        return os.environ.get(f'SWARM_{key.upper()}') or stored.get(key) or fallback

    parser.add_argument('verb', choices=sorted(VERBS))
    parser.add_argument(
        'target',
        nargs='?',
        help='the one thing this verb is about: a repo, a machine, a bundle, '
        'a selector. The equivalent long flag also works.',
    )
    parser.add_argument('--provider', default=setting('provider', 'gitea'), choices=['gitea', 'github'])
    parser.add_argument('--base-url', default=setting('base_url', ''))
    parser.add_argument('--org', default=setting('org', ''))
    parser.add_argument('--repo', default=setting('repo', ''))
    parser.add_argument('--gitea-exe', default=setting('gitea_exe'))
    parser.add_argument('--admin-user', default=setting('admin_user'))
    parser.add_argument('--machine', default=socket.gethostname())
    parser.add_argument('--branch', default=setting('branch', 'main'))
    parser.add_argument('--status-context', default=setting('status_context'))
    parser.add_argument('--required-repos', default=setting('required_repos', ''))
    parser.add_argument(
        '--require-repo',
        action='append',
        help='enroll/emit: a repository every issued identity must be able to READ. Repeatable. '
        'Defaults to --repo, so the check has teeth before anything is configured.',
    )
    parser.add_argument('-p', '--protect', action='store_true', help='onboard: also enable branch protection')
    # prune-issues. THE DEFAULTS ARE THE SAFETY: a week's grace, dry-run, and open items untouchable
    # at any age. `--yes` is the entire confirmation, and it is honoured only AFTER the count and the
    # oldest/newest number have been printed, so what is confirmed is a measurement.
    parser.add_argument('--older-than-days', type=float, default=7.0, help='prune-issues: closed-at cutoff')
    parser.add_argument('--keep-label', action='append', help='prune-issues: never delete items carrying this label')
    parser.add_argument('--manifest', help='prune-issues: where to record what was deleted')
    parser.add_argument('--yes', action='store_true', help='prune-issues: actually delete (default is a dry run)')
    parser.add_argument('--out', help='emit/admin-emit: bundle path')
    parser.add_argument('--bundle', help='consume: bundle path')
    parser.add_argument('--erase-local', action='store_true', help='revoke: also erase local credentials')
    parser.add_argument('--token-name', help='revoke: one exact token name')
    parser.add_argument(
        '--unmanaged',
        action='store_true',
        help='revoke: every token NOT named user@machine (i.e. not issued by swarmctl)',
    )
    parser.add_argument(
        '--ask-password',
        action='store_true',
        help='prompt for the admin password (token management needs it; never prompted for otherwise)',
    )
    parser.add_argument(
        '--admin-token-name',
        metavar='NAME',
        help=(
            "mint this machine's admin token under NAME instead of swarmctl-admin@<hostname>. "
            'THE UNWEDGE: when the hostname-derived name is already taken on the server and this '
            'machine holds no copy, that value is unrecoverable and every run fails identically. '
            'This is the way out that does NOT need the operator password'
        ),
    )
    parser.add_argument(
        '--ephemeral',
        action='store_true',
        help='revoke: leftover swarmctl-ephemeral@* admin tokens (a CONCURRENT run holds one too)',
    )
    parser.add_argument(
        '--all',
        dest='all_tokens',
        action='store_true',
        help='revoke: every token of all four users (needs --confirm REVOKE-ALL)',
    )
    parser.add_argument('--confirm', help='destroy: DESTROY   revoke --all: REVOKE-ALL')
    raw = list(sys.argv[1:] if argv is None else argv)
    args = parser.parse_args(raw)
    # WHICH FLAGS WERE TYPED, not which have values. Two things need it: `--machine` defaults to
    # this hostname, so a bare `revoke` would otherwise silently mean "revoke this machine" -- a
    # selector nobody chose; and `config` must persist only what was asked for, never the defaults
    # it happened to resolve this run.
    args.typed = {a.split('=', 1)[0].lstrip('-').replace('-', '_') for a in raw if a.startswith('--')}
    args.machine_given = 'machine' in args.typed
    apply_positional(args)
    return args


def main() -> int:
    args = parse_argv()
    handler, _help = VERBS[args.verb]
    if args.verb == 'config':
        return handler(None, args)
    if args.verb == 'consume':
        if not args.bundle:
            msg = 'consume needs --bundle FILE'
            raise Fail(msg)
        return handler(None, args)
    if not args.base_url or not args.org:
        msg = '--base-url and --org are required (or SWARM_BASE_URL / SWARM_ORG)'
        raise Fail(msg)
    if args.verb in {'onboard', 'verify'} and not args.repo:
        msg = f'{args.verb} needs --repo OWNER/NAME'
        raise Fail(msg)
    provider = build_provider(args)
    return handler(provider, args)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Fail as failure:
        sys.stderr.write(f'\nFAILED: {failure}\n')
        sys.exit(1)
