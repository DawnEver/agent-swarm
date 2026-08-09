"""A :class:`~agent_swarm.store.Store` over any forge. All the logic; no vendor anywhere in it.

THE CLAIM IS A GIT REF PUSH, AND THAT IS NOW A PORTABILITY DECISION
===================================================================

`store.py` specifies `try_claim` as a compare-and-swap and names two plausible backings: "an Issue
assignee set only if unset; a ref created only if absent". Measured by racing
`server.mingyangbao.site:9000` (Gitea 1.26.4) on 2026-08-09 -- raced, not read:

    PATCH /issues/{n} with `assignees`   12 concurrent racers -> 12x 201.
                                         Last-write-wins. Every racer believes it claimed.
    POST /labels with a duplicate name   12 concurrent racers -> 12x 201, twelve identical labels.
                                         Not unique, so not a lock either.
    POST /repos/{o}/{r}/git/refs         405. The ref API is not enabled on this deployment.
    git push <sha>:refs/<new>             8 concurrent racers -> exactly 1 rc=0, 7 rejected.

Only the last refuses a second writer. That alone would justify it, but it is NOT the load-bearing
reason any more. **The system must work on Gitea AND GitHub, and `git push` of a create-only ref is
the only atomic primitive both of them have.** It is git's own compare-and-swap, implemented by the
receiving end of the wire protocol rather than by a vendor's issue tracker, so it behaves
identically against both. GitHub offers no conditional-assignee either -- a claim built on any forge
API field would need TWO correctness arguments for two backends, and only the one we could race
would ever be checked. That is why `Forge` is asked for a URL and nothing else.

WHY THE OWNER TRAVELS IN THE COMMIT, NOT THE REF NAME
=====================================================

A create-only push is atomic over ONE NAME. Put the owner in the name -- `refs/claims/j1/runner-a`
-- and sixteen runners push sixteen different refs, all sixteen succeed, and `try_claim` returns
True to every one of them while still looking like a CAS in the log. That is push-then-arbitrate
wearing the new interface's clothes; sabotaging it exactly this way makes the threaded test report
16 == 1. So the name is spent entirely on being IDENTICAL for every racer, and everything that
varies -- owner, timestamp, lease, nonce -- rides in the commit message of the parentless commit
the winner alone gets to write.

The nonce is not decoration. Two attempts by one owner in the same second would otherwise build a
byte-identical commit, hence the same sha, and `git push` calls pushing a ref to the value it
already holds a SUCCESS -- so the CAS would answer True to the re-claim the contract requires it to
refuse.

WHY THERE IS A LEASE
====================

A ref, once created, has no expiry. A machine that dies mid-run holds its job forever and the job
leaves the fleet permanently while looking perfectly healthy. So the claim carries `claimed_at` and
`lease_seconds`, an expired claim reads as unheld, and taking one over is a `--force-with-lease`
push against the exact sha that was observed to be stale. THE TAKEOVER IS ITSELF A CAS: without the
lease the second write path would be an unconditional force push, and one unconditional write is
all it takes to stop being a compare-and-swap.

THE VERDICT HALF NEEDS NO ATOMICITY, so it goes through the forge: a comment carrying gate.py's
output, one `verdict:*` label, and the item closed. A verdict is written by the runner that already
holds the claim; nobody is racing it. Everything about it -- the title format, the label vocabulary,
the rule that exactly one verdict label may be attached -- is decided here, once, for every vendor.
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
import uuid
from dataclasses import dataclass
from pathlib import Path

from agent_swarm.forge import Forge, ForgeError
from agent_swarm.job import Job
from agent_swarm.store import VERDICTS

#: How long a claim stays valid without being released. Long enough for the longest blocking gate
#: (30 minutes, `AGENTS.md`) plus the slack a shared box costs; short enough that a dead machine
#: does not park a job for a working day.
DEFAULT_LEASE_SECONDS = 3 * 3600.0

#: The label a verdict wears. Lower-case because labels are read by humans on a board; the VALUE is
#: always `store.VERDICTS`' upper-case word, and this mapping is the only translation between them.
#: It lives HERE, not in a forge: both vendors have labels, so the vocabulary is not a vendor's to
#: choose.
VERDICT_LABELS = {
    'PASS': 'verdict:pass',
    'FAIL': 'verdict:fail',
    'INCONCLUSIVE': 'verdict:inconclusive',
}
_LABEL_TO_VERDICT = {label: word for word, label in VERDICT_LABELS.items()}

_CLAIM_REF_ROOT = 'refs/claims'
_ITEM_TITLE_ROOT = '[swarm]'

# Anything outside this becomes `_`. Dots are excluded wholesale rather than filtered: it is the
# cheapest way to be sure of `..`, a leading `.` and a trailing `.lock` at once.
_REF_SAFE = re.compile(r'[^A-Za-z0-9_-]')


# --------------------------------------------------------------------------------------------
# The claim payload -- pure, so it is testable without a network or a forge.
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
# person.
_GIT_ENV = {
    'GIT_AUTHOR_NAME': 'agent-swarm',
    'GIT_AUTHOR_EMAIL': 'agent-swarm@invalid',
    'GIT_COMMITTER_NAME': 'agent-swarm',
    'GIT_COMMITTER_EMAIL': 'agent-swarm@invalid',
    'GIT_TERMINAL_PROMPT': '0',
}


def _scratch_repo(remote_url: str) -> Path:
    """A bare repo, shared per remote, whose only purpose is to hold objects and push them.

    IT IS NOT A CHECKOUT OF ANYTHING. Claim commits are parentless and carry an empty tree, so
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


class ForgeStore:
    """A `Store` whose claims are git refs and whose verdicts are forge work items.

    VENDOR-AGNOSTIC BY CONSTRUCTION: the only vendor-shaped thing it holds is a `Forge`, and the
    only thing it asks that forge for beyond dull CRUD is `git_url()`. Grep this class for 'gitea'
    or 'github' -- an empty result is the property, and `test_forge_store.py` asserts it rather
    than trusting the grep.

    Args:
        namespace: isolates one swarm (or one test run) from another. It prefixes both the claim
            refs and the work-item titles, and `purge_namespace` cannot reach outside it.
        forge: the storage/UI backend. REQUIRED, and deliberately not defaulted -- a default is a
            choice of vendor, and this module is the one that must not make one. Callers take
            `forge.default_forge()`.
        lease_seconds: how long a claim survives without a release.

    Construction performs NO I/O -- not a connection, not a credential read.
    """

    def __init__(self, namespace: str, forge: Forge, *, lease_seconds: float = DEFAULT_LEASE_SECONDS) -> None:
        self.namespace = namespace
        self.forge = forge
        self.lease_seconds = lease_seconds
        self._item_numbers: dict[str, int] = {}

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
        # The takeover, and it is a CAS too: refused unless the ref still holds the exact stale sha
        # we judged expired.
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
            raise ForgeError(msg)
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
                raise ForgeError(msg)
            read = self._git('cat-file', 'commit', sha)
            if read.returncode != 0:
                msg = f'{ref} moved while being read (sha {sha} unavailable)'
                raise ForgeError(msg)
        # A commit object is headers, a blank line, then the message.
        _, _, message = read.stdout.partition('\n\n')
        return message.strip()

    def _write_commit(self, payload: str) -> str:
        tree = self._git('hash-object', '-t', 'tree', '-w', '--stdin', stdin='')
        if tree.returncode != 0:
            msg = f'cannot write tree: {tree.stderr.strip()}'
            raise ForgeError(msg)
        commit = self._git('commit-tree', tree.stdout.strip(), '-m', payload)
        if commit.returncode != 0:
            msg = f'cannot write claim commit: {commit.stderr.strip()}'
            raise ForgeError(msg)
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
            ['git', '--git-dir', str(_scratch_repo(self.forge.git_url())), *args],
            input=stdin,
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, **_GIT_ENV},
        )

    # -- verdicts ----------------------------------------------------------------------------

    def record_verdict(self, job: Job, *, verdict: str, detail: str) -> None:
        if verdict not in VERDICTS:
            # BEFORE any I/O: a store that validated after the comment was posted would leave one
            # behind for a verdict it then rejected.
            msg = f'verdict must be one of {sorted(VERDICTS)}, got {verdict!r}'
            raise ValueError(msg)

        number = self._item_number(job, create=True)
        self.forge.add_comment(number, f'**{verdict}**\n\n```\n{detail}\n```')

        # Exactly one verdict label at a time. A retry after INCONCLUSIVE that merely ADDED `pass`
        # would leave the job both inconclusive and green, and nothing downstream can act on that.
        for existing in self.forge.labels(number):
            if existing in _LABEL_TO_VERDICT:
                self.forge.remove_label(number, existing)
        self.forge.add_label(number, VERDICT_LABELS[verdict])
        self.forge.close_work_item(number)

    def verdict(self, job: Job) -> str | None:
        number = self._item_number(job)
        if number is None:
            return None
        for label in self.forge.labels(number):
            word = _LABEL_TO_VERDICT.get(label)
            if word is not None:
                return word
        return None

    def verdict_detail(self, job: Job) -> str:
        """The evidence behind the verdict -- gate.py's output, as the last comment on the item."""
        number = self._item_number(job)
        if number is None:
            return ''
        comments = self.forge.comments(number)
        return comments[-1] if comments else ''

    def item_state(self, job: Job) -> str | None:
        number = self._item_number(job)
        return None if number is None else self.forge.state(number)

    def _item_title(self, job: Job) -> str:
        return f'{_ITEM_TITLE_ROOT} {self.namespace}/{job.claim_key()}'

    def _item_number(self, job: Job, *, create: bool = False) -> int | None:
        title = self._item_title(job)
        cached = self._item_numbers.get(title)
        if cached is not None:
            return cached
        for item in self.forge.list_work_items():
            if item.title == title:
                self._item_numbers[title] = item.number
                return item.number
        if not create:
            return None
        made = self.forge.create_work_item(title=title, body=f'`{job.claim_key()}`')
        self._item_numbers[title] = made
        return made

    # -- housekeeping ------------------------------------------------------------------------

    def purge_namespace(self) -> None:
        """Remove everything THIS namespace created: its claim refs, and its work items retired.

        SCOPED BY CONSTRUCTION, not by care. The ref glob and the title prefix both begin with this
        namespace, so nothing outside it -- `refs/heads`, `refs/candidates`, `refs/verdicts`,
        `refs/ci`, or another swarm's items -- is reachable from here. HOW an item is retired is the
        forge's business: this deployment closes and retitles, another may delete.
        """
        prefix = f'{_CLAIM_REF_ROOT}/{_ref_component(self.namespace)}/'
        listed = self._git('ls-remote', 'origin', f'{prefix}*')
        for line in listed.stdout.splitlines():
            if not line.strip():
                continue
            ref = line.split()[1]
            if ref.startswith(prefix):
                self._push(f':{ref}')

        title_prefix = f'{_ITEM_TITLE_ROOT} {self.namespace}/'
        for item in self.forge.list_work_items():
            if item.title.startswith(title_prefix):
                self.forge.retire_work_item(item.number)
