"""A Gitea-backed :class:`~agent_swarm.store.Store`. Stdlib only, like everything in this package.

WHY THE CLAIM IS A REF PUSH AND NOT AN ISSUE ASSIGNEE
=====================================================

`store.py` specifies `try_claim` as a compare-and-swap and names two plausible backings: "an Issue
assignee set only if unset; a ref created only if absent". Only ONE of those exists on this
deployment. Measured against `server.mingyangbao.site:9000` (Gitea 1.26.4) on 2026-08-09 by racing
the real server, not by reading its documentation:

    PATCH /issues/{n} with `assignees`   12 concurrent racers -> 12x 201.
                                         Last-write-wins. Every racer believes it claimed.
    POST /labels with a duplicate name   12 concurrent racers -> 12x 201, twelve identical labels.
                                         Not unique, so not a lock either.
    POST /repos/{o}/{r}/git/refs         405. The ref API is not enabled on this deployment.
    git push <sha>:refs/<new>             8 concurrent racers -> exactly 1 rc=0, 7 rejected.

The last line is the only compare-and-swap in the building, so it is the whole claim mechanism. An
implementation built on the assignee would pass every sequential test and duplicate every job --
the precise defect this layer was extracted to remove.

There is a second, blunter reason: this Gitea instance has exactly ONE user. An assignee cannot
distinguish two runners even in principle.

WHY THE OWNER TRAVELS IN THE COMMIT, NOT THE REF NAME
=====================================================

A create-only push is atomic over ONE NAME. Put the owner in the name -- `refs/claims/j1/runner-a`
-- and sixteen runners push sixteen different refs, all sixteen succeed, and `try_claim` returns
True to every one of them while still looking like a CAS in the log. That is push-then-arbitrate
wearing the new interface's clothes. So the name is spent entirely on being IDENTICAL for every
racer, and everything that varies -- owner, timestamp, lease, nonce -- goes into the commit message
of the parentless commit the winner alone gets to write.

The nonce is not decoration. Two attempts by one owner in the same second would otherwise build a
byte-identical commit, hence the same sha, and `git push` calls pushing a ref to the value it
already holds a SUCCESS -- so the CAS would answer True to the re-claim the contract requires it to
refuse.

WHY THERE IS A LEASE
====================

A ref, once created, has no expiry. A machine that dies mid-run holds its job forever and the job
leaves the fleet permanently while looking perfectly healthy. So the claim carries `claimed_at` and
`lease_seconds`, an expired claim reads as unheld, and taking one over is a
`--force-with-lease` push against the exact sha that was observed to be stale. THE TAKEOVER IS
ITSELF A CAS: without the lease the second write path would be an unconditional force push, and one
unconditional write is all it takes to stop being a compare-and-swap.

THE VERDICT HALF NEEDS NO ATOMICITY, so it uses the Issue API: a comment carrying gate.py's output,
one `verdict:*` label, and the issue closed. A verdict is written by the runner that already holds
the claim; nobody is racing it.

A NOTE ON LOOKUP. `GET /issues?q=<title>` returned ZERO results for an issue that demonstrably
existed on this deployment -- the issue indexer is not populated. Anything built on `q` would have
read "no verdict yet" for an answered job, which is the unearned green the vocabulary exists to
prevent, so lookup is a plain listing with an exact client-side title match.

THE TOKEN IS NEVER WRITTEN DOWN. It is read from `git credential fill` at call time, held in memory,
sent only in an `Authorization` header, and never placed in a URL (a URL lands in `.git/config`, in
the process table, and in every git trace).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_swarm.job import Job
from agent_swarm.store import VERDICTS

DEFAULT_BASE_URL = 'http://server.mingyangbao.site:9000'
DEFAULT_REPO = 'Tianjie-Zou-Team/motronics-studio'

#: How long a claim stays valid without being released. Long enough for the longest blocking gate
#: (30 minutes, `AGENTS.md`) plus the slack a shared box costs, short enough that a dead machine
#: does not park a job for a working day.
DEFAULT_LEASE_SECONDS = 3 * 3600.0

#: The label a verdict wears on its issue. Lower-case because labels are read by humans on a board;
#: the VALUE is always `store.VERDICTS`' upper-case word, and this mapping is the only translation.
VERDICT_LABELS = {
    'PASS': 'verdict:pass',
    'FAIL': 'verdict:fail',
    'INCONCLUSIVE': 'verdict:inconclusive',
}
_LABEL_TO_VERDICT = {label: word for word, label in VERDICT_LABELS.items()}
_LABEL_COLORS = {'verdict:pass': '#0e8a16', 'verdict:fail': '#b60205', 'verdict:inconclusive': '#fbca04'}

_CLAIM_REF_ROOT = 'refs/claims'
_ISSUE_TITLE_ROOT = '[swarm]'

# Anything outside this becomes `_`. Dots are excluded wholesale rather than filtered: it is the
# cheapest way to be sure of `..`, a leading `.` and a trailing `.lock` at once.
_REF_SAFE = re.compile(r'[^A-Za-z0-9_-]')

_HTTP_TIMEOUT = 60.0


class GiteaError(RuntimeError):
    """The server refused something. Carries the status and body, never the credential."""


# --------------------------------------------------------------------------------------------
# The claim payload -- pure, so it is testable without a network.
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Claim:
    """What the winning commit says. `nonce` exists only to keep two attempts' shas distinct."""

    owner: str
    claimed_at: float
    lease_seconds: float
    nonce: str = ''

    def is_expired(self, *, now: float) -> bool:
        """Has the lease run out? The boundary instant is still HELD, not free."""
        return now > self.claimed_at + self.lease_seconds


def encode_claim(*, owner: str, claimed_at: float, lease_seconds: float) -> str:
    return json.dumps(
        {
            'owner': owner,
            'claimed_at': claimed_at,
            'lease_seconds': lease_seconds,
            'nonce': uuid.uuid4().hex,
        },
        sort_keys=True,
    )


def decode_claim(text: str) -> Claim:
    """Parse a claim payload.

    Raises:
        ValueError: the payload is not readable as a claim. UNPARSEABLE IS NOT FREE -- reading a
            corrupt claim as "unclaimed" hands a live job to a second runner, and nothing errors.
    """
    try:
        raw = json.loads(text)
        return Claim(
            owner=str(raw['owner']),
            claimed_at=float(raw['claimed_at']),
            lease_seconds=float(raw['lease_seconds']),
            nonce=str(raw.get('nonce', '')),
        )
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        msg = f'unreadable claim payload ({exc})'
        raise ValueError(msg) from exc


def _ref_component(text: str) -> str:
    """A git-legal spelling of `text` that still distinguishes it from its neighbours.

    The hash suffix is what makes the sanitiser injective. Without it `a b` and `a-b` collapse onto
    one ref, so two different jobs share one claim -- each correctly refusing the other, neither
    ever running, and nothing anywhere failing.
    """
    safe = _REF_SAFE.sub('_', text) or '_'
    return f'{safe}-{hashlib.sha1(text.encode()).hexdigest()[:10]}'


def claim_ref(namespace: str, job: Job) -> str:
    """The one ref every racer for `job` contends over. The OWNER IS DELIBERATELY ABSENT."""
    parts = '/'.join(_ref_component(part) for part in job.claim_key().split('/'))
    return f'{_CLAIM_REF_ROOT}/{_ref_component(namespace)}/{parts}'


# --------------------------------------------------------------------------------------------
# The scratch git repository the pushes are driven from.
# --------------------------------------------------------------------------------------------

_SCRATCH_LOCK = threading.Lock()
_SCRATCH: dict[str, Path] = {}

# A commit needs an identity, and taking the operator's would attribute a machine's claim to a
# person. Fixed values also keep the sha a function of the payload alone.
_GIT_ENV = {
    'GIT_AUTHOR_NAME': 'agent-swarm',
    'GIT_AUTHOR_EMAIL': 'agent-swarm@invalid',
    'GIT_COMMITTER_NAME': 'agent-swarm',
    'GIT_COMMITTER_EMAIL': 'agent-swarm@invalid',
    'GIT_TERMINAL_PROMPT': '0',
}


def _scratch_repo(remote_url: str) -> Path:
    """A bare repo, shared per remote, whose only purpose is to hold objects and push them.

    IT IS NOT A CHECKOUT OF ANYTHING. The claim commits are parentless and carry an empty tree, so
    nothing about the target repository's history needs to be present -- which is also why this
    never runs inside, or writes to, a working checkout.
    """
    with _SCRATCH_LOCK:
        found = _SCRATCH.get(remote_url)
        if found is not None:
            return found
        path = Path(tempfile.mkdtemp(prefix='agent-swarm-claims-'))
        subprocess.run(['git', 'init', '-q', '--bare', str(path)], check=True, capture_output=True)
        subprocess.run(
            ['git', '--git-dir', str(path), 'remote', 'add', 'origin', remote_url],
            check=True,
            capture_output=True,
        )
        _SCRATCH[remote_url] = path
        return path


# --------------------------------------------------------------------------------------------


class GiteaStore:
    """A `Store` whose claims are git refs and whose verdicts are issues.

    Args:
        namespace: isolates one swarm (or one test run) from another. It prefixes both the claim
            refs and the issue titles, and `purge_namespace` will not touch anything outside it.
        base_url: the Gitea root, no trailing slash.
        repo: ``owner/name``.
        lease_seconds: how long a claim survives without a release.

    Construction performs NO I/O -- not a connection, not a credential read. A store built against
    an unreachable host must still refuse a bad verdict word, and that refusal must be about the
    word.
    """

    def __init__(
        self,
        namespace: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        repo: str = DEFAULT_REPO,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
    ) -> None:
        self.namespace = namespace
        self.base_url = base_url.rstrip('/')
        self.repo = repo
        self.lease_seconds = lease_seconds
        self._token: str | None = None
        self._token_lock = threading.Lock()
        self._issue_numbers: dict[str, int] = {}
        self._label_ids: dict[str, int] = {}

    # -- claims ------------------------------------------------------------------------------

    def try_claim(self, job: Job, *, owner: str) -> bool:
        """Take `job` atomically, via a create-only push (or a lease-checked takeover)."""
        ref = claim_ref(self.namespace, job)
        held = self._read_claim(ref)
        if held is not None and not held[1].is_expired(now=time.time()):
            # INCLUDING when the holder is `owner`: see the contract. A runner re-taking its own
            # claim resets the lease, and a hung run then keeps the job locked forever.
            return False

        payload = encode_claim(owner=owner, claimed_at=time.time(), lease_seconds=self.lease_seconds)
        commit = self._write_commit(payload)
        if held is None:
            # Create-only. Our commit is parentless, so it can never be a fast-forward of an
            # existing ref -- a concurrent creation is therefore always rejected, never merged.
            return self._push(f'{commit}:{ref}') == 0
        # The takeover, and it is a CAS too: the push is refused unless the ref still holds the
        # exact stale sha we judged expired.
        return self._push(f'--force-with-lease={ref}:{held[0]}', f'{commit}:{ref}') == 0

    def claim_owner(self, job: Job) -> str | None:
        held = self._read_claim(claim_ref(self.namespace, job))
        if held is None or held[1].is_expired(now=time.time()):
            return None
        return held[1].owner

    def release(self, job: Job, *, owner: str) -> None:
        """Drop the claim if `owner` holds it. A stranger's call is a no-op, never a steal."""
        ref = claim_ref(self.namespace, job)
        held = self._read_claim(ref)
        if held is None or held[1].owner != owner:
            return
        # Lease-checked deletion for the same reason as the takeover: between the read and the
        # delete the ref may already have been taken over by someone whose claim we must not drop.
        self._push(f'--force-with-lease={ref}:{held[0]}', f':{ref}')

    def _read_claim(self, ref: str) -> tuple[str, Claim] | None:
        """`(sha, claim)` for `ref`, or ``None`` if it does not exist."""
        listed = self._git('ls-remote', 'origin', ref)
        if listed.returncode != 0:
            msg = f'ls-remote failed for {ref}: {listed.stderr.strip()}'
            raise GiteaError(msg)
        line = listed.stdout.strip()
        if not line:
            return None
        sha = line.split()[0]
        return sha, decode_claim(self._commit_message(ref, sha))

    def _commit_message(self, ref: str, sha: str) -> str:
        """The payload of a remote commit, fetched if this scratch repo has not seen it."""
        read = self._git('cat-file', 'commit', sha)
        if read.returncode != 0:
            fetched = self._git('fetch', '-q', 'origin', ref)
            if fetched.returncode != 0:
                msg = f'cannot fetch {ref}: {fetched.stderr.strip()}'
                raise GiteaError(msg)
            read = self._git('cat-file', 'commit', sha)
            if read.returncode != 0:
                msg = f'{ref} moved while being read (sha {sha} unavailable)'
                raise GiteaError(msg)
        # A commit object is headers, a blank line, then the message.
        _, _, message = read.stdout.partition('\n\n')
        return message.strip()

    def _write_commit(self, payload: str) -> str:
        tree = self._git('hash-object', '-t', 'tree', '-w', '--stdin', stdin='')
        if tree.returncode != 0:
            msg = f'cannot write tree: {tree.stderr.strip()}'
            raise GiteaError(msg)
        commit = self._git('commit-tree', tree.stdout.strip(), '-m', payload)
        if commit.returncode != 0:
            msg = f'cannot write claim commit: {commit.stderr.strip()}'
            raise GiteaError(msg)
        return commit.stdout.strip()

    def _push(self, *args: str) -> int:
        """`git push origin <args>`, returning the exit code.

        A NON-ZERO CODE IS THE ANSWER, NOT AN ERROR: it is how the loser of the compare-and-swap
        finds out it lost. Raising here would turn "someone else has the job" -- the ordinary,
        expected outcome for fifteen of sixteen racers -- into an exception.
        """
        flags = [a for a in args if a.startswith('--')]
        refspecs = [a for a in args if not a.startswith('--')]
        return self._git('push', *flags, 'origin', *refspecs).returncode

    def _git(self, *args: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ['git', '--git-dir', str(_scratch_repo(self._remote_url())), *args],
            input=stdin,
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, **_GIT_ENV},
        )

    def _remote_url(self) -> str:
        """The plain URL. THE TOKEN IS NEVER PUT HERE -- git's credential helper supplies it, and a
        URL with a secret in it is persisted in `.git/config` and echoed by every trace."""
        return f'{self.base_url}/{self.repo}.git'

    # -- verdicts ----------------------------------------------------------------------------

    def record_verdict(self, job: Job, *, verdict: str, detail: str) -> None:
        if verdict not in VERDICTS:
            # BEFORE any I/O: a store that validated after the POST would leave a comment behind
            # for a verdict it then rejected.
            msg = f'verdict must be one of {sorted(VERDICTS)}, got {verdict!r}'
            raise ValueError(msg)

        number = self._issue_number(job, create=True)
        body = f'**{verdict}**\n\n```\n{detail}\n```'
        self._api('POST', f'/repos/{self.repo}/issues/{number}/comments', {'body': body})

        # Exactly one verdict label at a time. A retry after INCONCLUSIVE that merely ADDED `pass`
        # would leave the job both inconclusive and green, and nothing downstream can act on that.
        for existing in self._api('GET', f'/repos/{self.repo}/issues/{number}/labels') or []:
            if existing['name'] in _LABEL_TO_VERDICT:
                self._api('DELETE', f'/repos/{self.repo}/issues/{number}/labels/{existing["id"]}')
        self._api(
            'POST', f'/repos/{self.repo}/issues/{number}/labels', {'labels': [self._label_id(VERDICT_LABELS[verdict])]}
        )
        self._api('PATCH', f'/repos/{self.repo}/issues/{number}', {'state': 'closed'})

    def verdict(self, job: Job) -> str | None:
        number = self._issue_number(job)
        if number is None:
            return None
        for label in self._api('GET', f'/repos/{self.repo}/issues/{number}/labels') or []:
            word = _LABEL_TO_VERDICT.get(label['name'])
            if word is not None:
                return word
        return None

    def verdict_detail(self, job: Job) -> str:
        """The evidence behind the verdict -- gate.py's output, as the last comment on the issue."""
        number = self._issue_number(job)
        if number is None:
            return ''
        comments = self._api('GET', f'/repos/{self.repo}/issues/{number}/comments') or []
        return comments[-1]['body'] if comments else ''

    def issue_state(self, job: Job) -> str | None:
        number = self._issue_number(job)
        if number is None:
            return None
        return self._api('GET', f'/repos/{self.repo}/issues/{number}')['state']

    def _issue_title(self, job: Job) -> str:
        return f'{_ISSUE_TITLE_ROOT} {self.namespace}/{job.claim_key()}'

    def _issue_number(self, job: Job, *, create: bool = False) -> int | None:
        title = self._issue_title(job)
        cached = self._issue_numbers.get(title)
        if cached is not None:
            return cached
        for issue in self._list_issues():
            if issue['title'] == title:
                self._issue_numbers[title] = issue['number']
                return issue['number']
        if not create:
            return None
        made = self._api('POST', f'/repos/{self.repo}/issues', {'title': title, 'body': f'`{job.claim_key()}`'})
        self._issue_numbers[title] = made['number']
        return made['number']

    def _list_issues(self) -> list[dict[str, Any]]:
        """Every issue, open and closed. NOT `?q=` -- this deployment's issue indexer returned zero
        hits for an issue that existed, and a verdict read as absent is an unearned green."""
        out: list[dict[str, Any]] = []
        page, limit = 1, 50
        while True:
            batch = self._api('GET', f'/repos/{self.repo}/issues?state=all&type=issues&limit={limit}&page={page}') or []
            out.extend(batch)
            if len(batch) < limit:
                return out
            page += 1

    def _label_id(self, name: str) -> int:
        """The id of the repo label `name`, created if absent.

        DUPLICATES ARE TOLERATED, NOT PREVENTED: `POST /labels` accepted twelve identical names from
        twelve racers, so this cannot be a lock. Taking the LOWEST id makes every runner converge on
        the same one regardless of who lost the race, and reading is by name anyway.
        """
        cached = self._label_ids.get(name)
        if cached is not None:
            return cached
        matches = [x['id'] for x in self._api('GET', f'/repos/{self.repo}/labels?limit=100') or [] if x['name'] == name]
        found = (
            min(matches)
            if matches
            else self._api('POST', f'/repos/{self.repo}/labels', {'name': name, 'color': _LABEL_COLORS[name]})['id']
        )
        self._label_ids[name] = found
        return found

    # -- housekeeping ------------------------------------------------------------------------

    def purge_namespace(self) -> None:
        """Remove everything THIS namespace created: its claim refs, and its issues closed+retitled.

        SCOPED BY CONSTRUCTION, not by care. The ref glob and the title prefix both start with this
        namespace, so nothing outside it -- `refs/heads`, `refs/candidates`, `refs/verdicts`,
        `refs/ci`, or another swarm's issues -- is reachable from here.
        """
        prefix = f'{_CLAIM_REF_ROOT}/{_ref_component(self.namespace)}/'
        listed = self._git('ls-remote', 'origin', f'{prefix}*')
        refs = [line.split()[1] for line in listed.stdout.splitlines() if line.strip()]
        for ref in refs:
            if not ref.startswith(prefix):  # pragma: no cover -- the server would have to lie
                continue
            self._push(f':{ref}')

        title_prefix = f'{_ISSUE_TITLE_ROOT} {self.namespace}/'
        for issue in self._list_issues():
            if issue['title'].startswith(title_prefix):
                self._api(
                    'PATCH',
                    f'/repos/{self.repo}/issues/{issue["number"]}',
                    {'state': 'closed', 'title': f'{issue["title"]} (purged)'},
                )

    # -- http --------------------------------------------------------------------------------

    def _api(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(f'{self.base_url}/api/v1{path}', data=data, method=method)
        request.add_header('Authorization', f'token {self._credential()}')
        request.add_header('Content-Type', 'application/json')
        try:
            with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            # The body is echoed because a Gitea refusal explains itself there. The REQUEST is not:
            # it carries the credential header.
            msg = f'{method} {path} -> {exc.code}: {exc.read().decode(errors="replace")[:400]}'
            raise GiteaError(msg) from None
        return json.loads(raw) if raw else None

    def _credential(self) -> str:
        """The API token, from `git credential fill`, held in memory only.

        NEVER LOGGED, PRINTED OR PERSISTED -- a hard project invariant. Note what this function does
        NOT do: it does not accept a token argument (which would invite a caller to hard-code one)
        and it does not fall back to an environment variable read at import time.
        """
        with self._token_lock:
            if self._token is not None:
                return self._token
            host = urllib.parse.urlsplit(self.base_url)
            query = f'protocol={host.scheme}\nhost={host.netloc}\n\n'
            filled = subprocess.run(
                ['git', 'credential', 'fill'],
                input=query,
                capture_output=True,
                text=True,
                check=False,
                env={**os.environ, 'GIT_TERMINAL_PROMPT': '0'},
            )
            for line in filled.stdout.splitlines():
                if line.startswith('password='):
                    self._token = line[len('password=') :]
                    return self._token
            msg = f'no stored credential for {host.netloc}; run `git credential fill` to check'
            raise GiteaError(msg)
