"""The forge: pure storage and a UI, and nothing else.

WHAT A FORGE IS FOR, AND WHAT IT IS NOT FOR
===========================================

Gitea and GitHub are demoted here to storage plus a human-readable surface (user directive:
"把 Gitea/GitHub 降级为单纯的纯粹存储与 UI 界面, 业务逻辑和调度机制 100% 掌握在自己的 CLI 工具里").
Every decision -- what a claim key is, how long a lease lasts, who wins a contested claim, which
words a verdict may take, when an item is retired -- lives in `forge_store.ForgeStore` and in
`admission`. This file holds only the I/O, and it holds it once per vendor.

THE SEAM IS DRAWN WHERE THE VENDORS DIFFER, which is why these methods are so small and so dull.
Anything two forges do differently -- label identity (Gitea labels are ids, so a name must be
resolved and possibly created), pagination limits, state vocabulary, whether an item can be DELETED
or only closed -- must be absorbed HERE. A vendor conditional in the store would mean the scheduler
has two behaviours, and only one of them would ever be tested.

NO REFS ANYWHERE. Issues and their comments are the whole storage layer (user directive 2026-08-09:
彻底废弃 ref, 后面基于 issue 和 project 迭代). The claim protocol that replaces the ref push is
built entirely from `add_comment` / `comments` / `delete_comment` -- see `forge_store` for why a
server-assigned comment id is a sound ordering key and a client-chosen one is not.

THE ONE PROPERTY THE STORE DEPENDS ON, and the reason `add_comment` returns an id rather than
`None`: **the id must be assigned by the SERVER, monotonically increasing, at insert.** A forge that
let a client choose it, or that reused ids, would break the claim protocol silently -- every runner
would still get an answer, and two of them would sometimes be "yes". Any new vendor client must
prove this by measurement before it is written.
"""

from __future__ import annotations

from collections.abc import Sequence

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from agent_swarm import credentials, roles

_HTTP_TIMEOUT = 60.0

#: How many times an API call is attempted in total. USER DIRECTIVE 2026-08-10:
#: 「所有 gitea 操作都要重试 3 次」.
#:
#: WHAT IS **NOT** RETRIED, because that is where the decisions are:
#:
#: * **A 4xx.** A 401 retried three times is still a 401. Retrying costs three backoffs and makes a
#:   PERMANENT condition look transient, which is the reading that sends someone hunting for a flake
#:   instead of reissuing a token. Measured 2026-08-10: the stored Gitea credential stopped being
#:   accepted mid-session and every call returned 401 -- exactly the shape a blanket retry hides.
#:
#: WHAT IS RETRIED AND WHAT IT COSTS: 5xx and connection failures, including POST. A create whose
#: response was lost is indistinguishable from one that never arrived, so retrying it is the whole
#: point -- and the price is that a create which SUCCEEDED can be issued twice. That duplicate is
#: ACCEPTED, not prevented: work items resolve by lowest number, claims by lowest comment id, and
#: `ForgeStore.reconcile_duplicates` retires the losers off the hot path. A retry that refused POST
#: would be useless for exactly the calls that matter; a retry that claimed it could not duplicate
#: would be a declaration that lies.
API_ATTEMPTS = 3

#: Seconds of backoff, multiplied by the attempt number. Immediate retries against a struggling
#: server are three requests, not one retry.
_BACKOFF_S = 1.0

#: Indirected so a test can assert the BOUND without paying it. Patched by name, never passed in:
#: a sleep argument would let a caller set it to zero in production.
_sleep = time.sleep


def _is_retryable_status(code: int) -> bool:
    """5xx yes, 4xx no. Stated as a function so the rule has one definition and a test can call it."""
    return code >= 500


#: This project's forge. It lives in the VENDOR module, not in the store: a default is a choice of
#: vendor, and a store holding one would name a vendor in the one file that must not.
#: The vendor-neutral status words. Gitea uses these verbatim; another forge maps them.
STATUS_STATES = frozenset({'pending', 'success', 'failure', 'error'})

#: Gitea rejects a description past this length. Truncated rather than refused -- see `set_status`.
_STATUS_DESCRIPTION_LIMIT = 255

#: The DEPLOYMENT this swarm runs on, and the reason there is still a default here when `repo` has
#: none: a host is where the swarm itself lives, while a repo is WHICH PROJECT it schedules. A
#: package that defaulted the project would silently write to a stranger's issue tracker; one that
#: defaults its own host merely saves an argument. Overridable, so a second deployment is
#: configuration rather than a source edit.
DEFAULT_GITEA_BASE_URL = 'http://server.mingyangbao.site:9000'

#: Presentation, and therefore the vendor's business rather than the store's. A forge that needs a
#: colour to create a label reads it here; a forge that does not, ignores it.
LABEL_COLORS = {
    'verdict:pass': '#0e8a16',
    'verdict:fail': '#b60205',
    'verdict:inconclusive': '#fbca04',
}
_DEFAULT_LABEL_COLOR = '#ededed'


class ForgeError(RuntimeError):
    """The forge refused something. Carries the status and body, NEVER the credential."""


class CommentGone(ForgeError):
    """The comment being edited no longer exists.

    A SEPARATE TYPE BECAUSE THE TWO OUTCOMES NEED DIFFERENT RESPONSES, and telling them apart is the
    whole reason `update_comment` is not allowed to be quiet. A heartbeat is one comment per runner
    EDITED IN PLACE; if that comment has been pruned, an edit that failed silently would leave the
    runner believing it had beaten while the fleet saw it as dead. It must learn to RE-CREATE.

    Measured on Gitea 1.26.4: `PATCH /issues/comments/{id}` for a deleted comment returns 404 with
    `comment does not exist [id: ...]`, so the distinction is available at the wire and does not
    have to be inferred from a later read.
    """


@dataclass(frozen=True, slots=True)
class WorkItem:
    """One issue, in vendor-neutral terms. `state` is 'open' or 'closed' and nothing else.

    `labels` CARRIES WHAT THE LIST ENDPOINT ALREADY RETURNED. Before it existed, a sweep read the
    list and then fetched labels per item -- N+1 round trips, MEASURED at 101 calls for 100 open
    items, per runner, per sweep. Open items grow with fleet size, so at a hundred agents that is
    ten thousand calls a round to answer a question the first response already contained.

    Names only: label ids are a Gitea concept and stop at this boundary. Empty means "the vendor
    sent none", which for a listing endpoint is a fact, not an unknown -- an implementation that
    cannot supply them must fetch them rather than report absence.
    """

    number: int
    title: str
    state: str
    labels: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Comment:
    """One comment. THE ID IS THE LOAD-BEARING FIELD, not the body.

    It is the ordering key the claim protocol arbitrates on, so it must be exactly what the server
    assigned -- never a synthesised index, never a position in a list. A client that renumbered
    these while paginating would hand the store a key with none of the properties it relies on.
    """

    id: int
    body: str


@runtime_checkable
class Forge(Protocol):
    """Storage and UI. Every method is I/O; not one of them decides anything.

    THE TEST FOR WHETHER SOMETHING BELONGS HERE: could two vendors answer it differently? If yes it
    is a forge method. If no -- a lease expiry, a verdict vocabulary, a title format, who wins a
    contested claim -- it belongs in the store, where it is written once and tested once.
    """

    def list_work_items(self, *, state: str = 'all') -> list[WorkItem]:
        """Items in `state` (`'all'` or `'open'`). Paginated by the vendor client, not the caller.

        THE PARAMETER IS A COST CONTROL, not a convenience, and the cost it controls GROWS WITHOUT
        BOUND. A closed item is never deleted -- this deployment's API refuses it and GitHub has no
        endpoint at all -- so `'all'` pages through every job the swarm has ever run, on every
        sweep, on every runner. At a hundred agents that is the dominant read, and it gets slower
        every day the system works correctly.

        Most sweeps only act on open items and say so in their own filters; they must not pay for
        history. `'all'` stays the DEFAULT because a caller that needs closed items and forgets to
        ask would silently conclude "no such item" -- the expensive direction is slow, the cheap
        direction is wrong.
        """
        ...

    def work_item(self, number: int) -> WorkItem | None:
        """One item BY NUMBER, or ``None`` if there is no such number.

        THE ONLY READ MEASURED FRESH ON BOTH FORGES -- a primary-key read rather than a filter
        (GitHub 0/22 stale, Gitea fresh). It is what lets `item_index` turn a remembered number into
        an authoritative answer, and it is the reason a "does not exist" conclusion is legitimate
        here where it never is from a list.
        """
        ...

    def create_work_item(self, *, title: str, body: str, labels: Sequence[str] = ()) -> int:
        """Create an item, with its labels applied IN THE SAME CALL.

        `labels` is not a convenience. Applying them afterwards costs a second round trip per item,
        and item creation is the write path the whole fleet shares -- MEASURED at 2.0 calls per
        registered job before this parameter existed, against a create-only cost of 1.0. At the
        aggregate write rate this deployment sustains, that is half the registration throughput
        spent on a label.

        It also removes a window: between a create and a separate label call the item exists WITHOUT
        the label that makes it claimable, so a sweep landing in between sees work nobody handed
        over. Harmless today because the sweep simply skips it, but only by luck of which direction
        the missing label points.
        """
        ...

    def add_comment(self, number: int, body: str) -> int:
        """Post a comment, returning the SERVER-ASSIGNED id.

        Returning the id is not a convenience. It is the claim protocol's ordering key, and a
        vendor that could not supply one at insert could not host a claim at all.
        """
        ...

    def comments(self, number: int) -> list[Comment]:
        """Every comment, oldest first, carrying its server id."""
        ...

    def update_comment(self, number: int, comment_id: int, body: str) -> None:
        """Replace one comment's body IN PLACE.

        EDITED, NOT APPENDED, and that is a requirement rather than a preference. A heartbeat is one
        comment per runner: an appended beat keeps advertising a capability that has been WITHDRAWN
        (a vendor tool uninstalled between beats stays visible forever), and an append-only stream
        grows without bound against the 500-comment recycle limit. One shared body for the whole
        fleet is the other alternative and is worse still -- a shared mutable slot on an API with no
        compare-and-swap, where at a hundred runners one beat erases another.

        Raises:
            CommentGone: the comment no longer exists. NOT swallowed: a runner whose comment was
                pruned must re-create it rather than silently believe it beat.
        """
        ...

    def delete_comment(self, number: int, comment_id: int) -> None:
        """Remove one comment. How a claim is released, and how a loser withdraws."""
        ...

    def labels(self, number: int) -> list[str]:
        """Label NAMES. Ids are a Gitea concept and stop at this boundary."""
        ...

    def add_label(self, number: int, name: str) -> None:
        """Attach `name`, creating the repo label if the vendor requires one to exist."""
        ...

    def remove_label(self, number: int, name: str) -> None: ...

    def close_work_item(self, number: int) -> None:
        """Close the item. LABELS ARE NOT PART OF THIS, and that is a measurement.

        This method took a `labels` replacement set, on the belief that Gitea's issue edit applies
        `state` and `labels` in one PATCH -- the "verdict costs 3 round trips instead of 4" claim.
        REFUTED against the live server 2026-08-10: the PATCH carrying both returned 200, the item
        came out closed, and the verdict label was never attached. Confirmed in the server's own
        database: the item held only the handover label.

        A parameter the server ignores is worse than no parameter. It reads as a saved round trip,
        every test agreed with it because the in-memory double implemented it, and the cost of the
        error was a verdict that silently never landed.
        """
        ...

    def state(self, number: int) -> str: ...

    def set_status(self, sha: str, *, state: str, context: str, description: str) -> None:
        """Publish a commit status -- the check a protected branch waits on.

        THIS IS WHAT TURNS "gate green before main moves" FROM A CONVENTION INTO A MECHANISM. Today
        that rule is carried by whoever remembers it, and this session alone has the receipts on how
        that goes. A required status check is the server refusing the merge instead.

        `state` is the vendor-neutral word, one of `pending` / `success` / `failure` / `error`; the
        implementation maps it. `context` NAMES the check and must match the branch rule exactly --
        a rule waiting on a context nobody publishes freezes the branch, which is why enabling
        protection before this exists is the wrong order.

        **NOT SERVER-ENFORCEABLE ON EVERY FORGE, and stated here rather than implied.** Gitea has no
        scope for commit status: writing one requires repository write, which any identity that
        pushes branches must already have. So "only the verifier marks a commit green" is carried by
        credential distribution, not by the server. GitHub CAN enforce it (an App may hold
        statuses:write without contents:write). The strongest available check on our side is an
        architecture test that `set_status` is reached only through the verifier-role forge -- the
        server cannot stop another identity, but a test can stop OUR code from becoming one.
        """
        ...

    def retire_work_item(self, number: int) -> None:
        """Make the item stop counting, by whatever means the vendor allows.

        THE VERB IS DELIBERATELY NOT "DELETE". Gitea 1.26.4 on our deployment closes-and-retitles;
        another forge may hard-delete. The store must not care which, or it would grow a vendor
        conditional in exactly the place -- cleanup -- where nobody would ever notice it was wrong.

        **THE ONE THING EVERY IMPLEMENTATION MUST GUARANTEE: a retired item stops matching its
        title, and is no longer open.** Deleting achieves both; closing-and-retitling achieves both.
        An implementation that only CLOSED would leave a retired duplicate answering title lookups,
        so `_lowest_numbered` could resolve a live job to an item nobody is working. This is the
        closest thing to a behavioural requirement in this protocol, and it is stated here because
        it cannot be checked from outside the implementation.
        """
        ...


class GiteaForge:
    """Gitea over its REST API. Measured against `server.mingyangbao.site:9000`, Gitea 1.26.4.

    Args:
        base_url: the Gitea root, no trailing slash.
        repo: ``owner/name``.
        username: WHICH ROLE this forge acts as. Required, and it is not decoration -- see
            `_credential`.

    MEASURED, not assumed: `POST .../comments` returns a server-assigned id; three successive posts
    came back 595, 596, 597 -- monotonic and unique -- and `DELETE .../issues/comments/{id}` removed
    the middle one (204) leaving [595, 597]. The sixteen-way behaviour the claim protocol needs is
    recorded in `forge_store`'s docstring.

    Construction performs NO I/O -- not a connection, not a credential read. A store built against
    an unreachable host must still refuse a bad verdict word, and that refusal must be about the
    word rather than about the network.
    """

    def __init__(self, base_url: str, repo: str, *, username: str) -> None:
        # THE SCHEME IS ALLOWLISTED, and the reason is not tidiness. `urllib.request` dispatches on
        # the URL's scheme and honours `file:`, so a base URL of `file:///etc` would turn every API
        # call into a LOCAL READ that still looks like a forge answering -- claims, verdicts and
        # work items all fabricated from disk, with the retry loop faithfully retrying them. The
        # value reaches here from the environment, so it is operator input, and this is the one
        # place it can be refused before anything is built from it.
        split = urllib.parse.urlsplit(base_url)
        if split.scheme not in {'http', 'https'} or not split.netloc:
            msg = f'base_url must be http:// or https:// with a host, got {base_url!r}'
            raise ForgeError(msg)
        if not username:
            msg = 'username is required: four role credentials share one host and git tells them apart by it'
            raise ForgeError(msg)
        self.base_url = base_url.rstrip('/')
        self.repo = repo
        self.username = username
        self._token: str | None = None
        self._label_ids: dict[str, int] = {}

    def list_work_items(self, *, state: str = 'all') -> list[WorkItem]:
        """A plain listing, NOT `?q=`.

        `GET /issues?q=<title>` returned ZERO hits for an issue that demonstrably existed on this
        deployment -- the issue indexer is not populated. A lookup built on it would read "no such
        work item" for one that exists, so two runners would create two items for one job and each
        would claim its own. Vendor defect, absorbed here: the store just asks for the list.
        """
        out: list[WorkItem] = []
        page, limit = 1, 50
        while True:
            batch = (
                self._api('GET', f'/repos/{self.repo}/issues?state={state}&type=issues&limit={limit}&page={page}') or []
            )
            # LABELS COME FREE HERE. Gitea's issue objects carry them inline, so taking them
            # costs nothing and removes the per-item fetch the store used to do.
            out.extend(
                WorkItem(
                    number=x['number'],
                    title=x['title'],
                    state=x['state'],
                    labels=tuple(lb['name'] for lb in x.get('labels') or ()),
                )
                for x in batch
            )
            if len(batch) < limit:
                return out
            page += 1

    def work_item(self, number: int) -> WorkItem | None:
        try:
            raw = self._api('GET', f'/repos/{self.repo}/issues/{number}')
        except ForgeError as exc:
            if ' -> 404' in str(exc):
                return None
            raise
        return WorkItem(number=raw['number'], title=raw['title'], state=raw['state'])

    def create_work_item(self, *, title: str, body: str, labels: Sequence[str] = ()) -> int:
        """One POST. Label IDS are a Gitea concept that stops at this boundary, so names are
        resolved here -- and `_label_id` caches, so the resolution is paid once per process rather
        than once per item."""
        payload: dict[str, Any] = {'title': title, 'body': body}
        if labels:
            payload['labels'] = [self._label_id(name) for name in labels]
        return self._api('POST', f'/repos/{self.repo}/issues', payload)['number']

    def add_comment(self, number: int, body: str) -> int:
        return self._api('POST', f'/repos/{self.repo}/issues/{number}/comments', {'body': body})['id']

    def comments(self, number: int) -> list[Comment]:
        raw = self._api('GET', f'/repos/{self.repo}/issues/{number}/comments') or []
        return [Comment(id=c['id'], body=c['body']) for c in raw]

    def update_comment(self, number: int, comment_id: int, body: str) -> None:
        """MEASURED: 200 and the comment count is unchanged (a real in-place edit, not a
        replace-by-delete); 37-57 ms per beat on this deployment, against the 2510 ms ref push it
        replaces. A deleted comment answers 404 `comment does not exist`."""
        try:
            self._api('PATCH', f'/repos/{self.repo}/issues/comments/{comment_id}', {'body': body})
        except ForgeError as exc:
            if ' -> 404' in str(exc):
                msg = f'comment {comment_id} on item {number} no longer exists; re-create it'
                raise CommentGone(msg) from None
            raise

    def delete_comment(self, number: int, comment_id: int) -> None:
        # Comment ids are repo-scoped here, so the issue number is not in the path. The store still
        # passes it: a vendor that scopes ids per issue must be expressible without changing the
        # protocol.
        self._api('DELETE', f'/repos/{self.repo}/issues/comments/{comment_id}')

    def labels(self, number: int) -> list[str]:
        return [x['name'] for x in self._api('GET', f'/repos/{self.repo}/issues/{number}/labels') or []]

    def add_label(self, number: int, name: str) -> None:
        self._api('POST', f'/repos/{self.repo}/issues/{number}/labels', {'labels': [self._label_id(name)]})

    def remove_label(self, number: int, name: str) -> None:
        """Detach EVERY label id sharing this name, not just the one `_label_id` converges on.

        MEASURED 2026-08-10 -- this removed one id and the docstring on `_label_id` claimed the
        layer above "addresses labels by name anyway". False for removal, and it is a claim about a
        GUARD'S SCOPE: `ForgeStore.record_verdict` strips existing verdict labels before adding the
        new one precisely so a retry after INCONCLUSIVE cannot leave a job both inconclusive and
        green. With a duplicate definition attached under a higher id, the strip missed it and
        produced exactly that state -- a verdict reading green on an item still carrying
        INCONCLUSIVE, or the reverse, decided by whichever order the list endpoint returned.

        Duplicates are not hypothetical here: `POST /labels` accepted twelve identical names from
        twelve concurrent racers on this deployment. Our own writer always attaches the lowest id,
        so the higher one arrives from a human in the web UI, an older client, or the racer that
        lost -- the discriminating state is one our code cannot produce, which is why no test found
        this until the offline double stopped modelling labels as name-keyed.

        DELETING EVERY MATCH RATHER THAN THE LOWEST: the caller asked for the NAME to be gone, and
        leaving a same-named id attached does not satisfy that under any reading. `_label_id` still
        converges on the lowest for ATTACHMENT, which is what keeps runners agreeing.
        """
        for label_id in self._label_ids_for(name):
            self._api('DELETE', f'/repos/{self.repo}/issues/{number}/labels/{label_id}')

    def _label_ids_for(self, name: str) -> list[int]:
        """EVERY repo label id carrying `name`, or empty. Never creates one -- a removal that
        created the label it was asked to remove would be a write on a read-only intent."""
        return [x['id'] for x in self._api('GET', f'/repos/{self.repo}/labels?limit=100') or [] if x['name'] == name]

    def close_work_item(self, number: int) -> None:
        """One PATCH, carrying `state` and nothing else -- see the protocol's docstring for why."""
        self._api('PATCH', f'/repos/{self.repo}/issues/{number}', {'state': 'closed'})

    def state(self, number: int) -> str:
        return self._api('GET', f'/repos/{self.repo}/issues/{number}')['state']

    def set_status(self, sha: str, *, state: str, context: str, description: str) -> None:
        """POST a commit status. Gitea's states are the neutral words already, so nothing is mapped.

        The description is TRUNCATED rather than rejected: a gate summary is long, and a status that
        fails to publish because its prose was verbose would block a merge for a reason no reader
        would connect to the text.
        """
        if state not in STATUS_STATES:
            msg = f'state must be one of {sorted(STATUS_STATES)}, got {state!r}'
            raise ForgeError(msg)
        self._api(
            'POST',
            f'/repos/{self.repo}/statuses/{sha}',
            {
                'state': state,
                'context': context,
                'description': description[:_STATUS_DESCRIPTION_LIMIT],
            },
        )

    def retire_work_item(self, number: int) -> None:
        """Close and retitle. This deployment is not configured to let the API delete an issue, and
        an unavailable capability is not one to paper over -- the item stays, visibly spent."""
        title = self._api('GET', f'/repos/{self.repo}/issues/{number}')['title']
        suffix = '' if title.endswith(' (retired)') else ' (retired)'
        self._api('PATCH', f'/repos/{self.repo}/issues/{number}', {'state': 'closed', 'title': f'{title}{suffix}'})

    def _label_id(self, name: str) -> int:
        """The repo label id for `name`, created if absent.

        DUPLICATES ARE TOLERATED, NOT PREVENTED. `POST /labels` accepted twelve identical names from
        twelve concurrent racers on this deployment, so this is not, and cannot be made into, a
        lock. Taking the LOWEST id makes every runner converge on one regardless of who lost the
        race, and everything above this line addresses labels by name anyway.
        """
        cached = self._label_ids.get(name)
        if cached is not None:
            return cached
        matches = [x['id'] for x in self._api('GET', f'/repos/{self.repo}/labels?limit=100') or [] if x['name'] == name]
        if matches:
            found = min(matches)
        else:
            color = LABEL_COLORS.get(name, _DEFAULT_LABEL_COLOR)
            found = self._api('POST', f'/repos/{self.repo}/labels', {'name': name, 'color': color})['id']
        self._label_ids[name] = found
        return found

    def _api(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        """One API call, retried per `API_ATTEMPTS`. See that constant for what is NOT retried.

        A NEW REQUEST OBJECT PER ATTEMPT, deliberately: a `Request` carries its body as a consumed
        stream and its headers include the credential, so reusing one across attempts is both
        fragile and a second lifetime for the token.
        """
        last: ForgeError | None = None
        for attempt in range(1, API_ATTEMPTS + 1):
            data = json.dumps(body).encode() if body is not None else None
            request = urllib.request.Request(f'{self.base_url}/api/v1{path}', data=data, method=method)
            request.add_header('Authorization', f'token {self._credential()}')
            request.add_header('Content-Type', 'application/json')
            try:
                with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT) as response:
                    raw = response.read()
            except urllib.error.HTTPError as exc:
                # The RESPONSE body is echoed because a refusal explains itself there. The REQUEST
                # is never echoed: it carries the credential header.
                detail = exc.read().decode(errors='replace')[:400]
                msg = f'{method} {path} -> {exc.code}: {detail}'
                if not _is_retryable_status(exc.code):
                    raise ForgeError(msg) from None
                last = ForgeError(msg)
            except urllib.error.URLError as exc:
                # No response at all. Indistinguishable, from here, from a request that ARRIVED and
                # whose response was lost -- which is why a retried create can duplicate.
                last = ForgeError(f'{method} {path} -> unreachable: {exc.reason}')
            else:
                return json.loads(raw) if raw else None
            if attempt < API_ATTEMPTS:
                _sleep(_BACKOFF_S * attempt)
        msg = f'{last} (gave up after {API_ATTEMPTS} attempts)'
        raise ForgeError(msg)

    def _credential(self) -> str:
        """The API token for THIS ROLE, from the SWARM'S OWN store, held in memory only.

        NEVER LOGGED, PRINTED OR PERSISTED -- a hard project invariant. Note what this does NOT do:
        it takes no token argument (which would invite a caller to hard-code one) and it reads no
        environment variable at import time.

        THE USERNAME IS LOAD-BEARING, and omitting it was a defect rather than a simplification.
        Resolution keys on (protocol, host, USERNAME). Four role credentials share one host, so a
        query with no username returns whichever entry the store happens to hold -- measured on the
        live host 2026-08-10, where the bare-host key belonged to `OAUTH_USER`, an entry nothing in
        this system issued.

        WHAT THAT SILENTLY DEFEATED. Gitea has no scope for commit status, so "only the verifier
        marks a commit green" is carried by WHICH PROCESS HOLDS WHICH CREDENTIAL -- that is stated
        in `swarmctl`'s role table and it is the only thing separating the roles at all. A client
        that cannot select its role does not weaken that boundary; it removes it, while every call
        still succeeds and every test still passes.

        **IT NO LONGER READS THE OPERATOR'S CREDENTIAL STORE, and that is the last half of the
        2026-08-11 repair.** `swarmctl` stopped WRITING that store; this was still READING it, which
        is the more dangerous direction of the two. A write clobbers an identity and is at least a
        change somebody could notice; a read SUCCEEDS as whoever the vault happens to hold. The
        `OAUTH_USER` measurement above is exactly that failure, and it stayed invisible because
        every call worked.

        THERE IS NO FALLBACK TO THE AMBIENT STORE, deliberately. A fallback would preserve precisely
        the hazard being removed -- authenticating, plausibly and successfully, as the wrong
        identity -- and would announce it in a log nobody reads on a run that returned 200. An
        un-enrolled machine RAISES here, naming the role and the remedy.
        """
        if self._token is not None:
            return self._token
        host = urllib.parse.urlsplit(self.base_url)
        token = self._resolve_token(host.scheme, host.netloc, self.username)
        if token is None:
            msg = (
                f'no stored credential for {self.username}@{host.netloc} -- run `swarmctl enroll` on '
                f"this machine, or set {credentials.env_var_for(self.username)}. The operator's git "
                f'credential store is deliberately NOT consulted.'
            )
            raise ForgeError(msg)
        self._token = token
        return self._token

    def _resolve_token(self, scheme: str, netloc: str, username: str) -> str | None:
        """Ask `credentials` for this role's token. A SEAM, so the cache above it is testable.

        The caching in `_credential` exists ONLY to avoid repeating this lookup -- it is a pure cost
        optimisation -- and with the call inlined there was no way to observe whether it worked. A
        component whose entire reason to exist is COST, with no test that can see cost, is green
        whether it works or is inert; that is the class the index bug belonged to. This seam is what
        lets a test COUNT the calls instead of trusting the code.

        IT IS NO LONGER A SUBPROCESS, and the cost argument survives that unchanged: it was never
        specifically about `fork`, it was about a repeated lookup on a 7x24 fleet. What DID change is
        that the hazard the old implementation needed guarding against -- `git credential fill`
        falling back to an interactive prompt, on Windows a GUI dialog that hangs an unattended
        runner -- is now structurally absent rather than suppressed by an environment variable. No
        git is executed, so there is nothing that can prompt.
        """
        return credentials.resolve_token(scheme, netloc, username)


#: MEASURED on a real GitHub repo (`DawnEver/optimi-lab`, 2026-08-09). What SURVIVED the second
#: forge, so a reader knows which parts of this design are portable and which were luck.
GITHUB_CONFIRMED = (
    (
        'the claim protocol holds: 16 racers x 4 rounds, exactly one winner each, ~820 ms against '
        "Gitea's ~280 ms. All 16 comments were visible to all 16 racers in 64/64 reads, so "
        'read-after-write on the comment list -- the precondition that gated everything -- holds '
        'on GitHub too'
    ),
    'comment ids are server-assigned, unique and monotonic within a round',
    'multi-label `labels=A,B` means AND on both forges',
    (
        'issues CAN be hard-deleted on GitHub via GraphQL deleteIssue (49/49), so retirement may '
        'genuinely delete there -- which is why the store never spells out HOW an item is retired'
    ),
)

#: MEASURED DIFFERENCES a GitHub client must absorb and must NOT abstract over. Each one silently
#: breaks something that works on Gitea.
GITHUB_DIVERGENCES = (
    (
        'FRESHNESS IS INVERTED. On Gitea `?labels=` is exact (0/25 stale) and TEXT search lags '
        '~2.1 s (21/25). On GitHub the two swap: `?labels=` is stale 20/20 with a 4.0-6.6 s lag '
        'while text search is fresh 0/20. "Key on labels, never on text" is therefore '
        'GITEA-SPECIFIC and actively wrong on GitHub. NO query path is fresh on both forges; the '
        'only read that is, is a direct GET by issue number (GitHub 0/10 stale) -- a primary-key '
        'read rather than a filter'
    ),
    (
        'COMMENT LISTS PAGINATE on GitHub (`per_page` honoured, `Link` header); Gitea ignores '
        'per_page and returns everything. A client that does not follow `Link` truncates silently, '
        'and a truncated comment list is a claim protocol that cannot see a claim'
    ),
    (
        'A WRITE-SIDE SECONDARY RATE LIMIT exists on GitHub: 403 after ~24 creates in ~25 s with '
        'the primary quota still at 96 %. Gitea has none. Sixteen racers posting claim comments is '
        '~16 creates in about a second, so the fleet size at which claiming starts to fail is a '
        'GitHub number with no Gitea equivalent'
    ),
)

#: What is STILL unmeasured, and blocking. Each line is one experiment; documentation would be
#: evidence about intent, not about behaviour.
GITHUB_UNMEASURED = (
    (
        'is the PLAIN issues list read-after-write fresh on GitHub? `_item_number` concludes "no '
        'such work item" from a list query and then CREATES one, and the measured rule is that no '
        '"does not exist" conclusion may come from a list query on either forge. If that list lags '
        'the way `?labels=` does, two runners create two items and the 16-winner bug returns'
    ),
    (
        'the pagination probe: with a comment list past one page, is the LOWEST id still on page '
        'one, and does the spool marker scan need every page? Truncation is safe for arbitration '
        'and unsafe for idempotence, so those need separate answers'
    ),
    'the secondary-limit backoff: what a 403 looks like, and what retry policy a fleet needs',
    (
        'whether retirement should delete (GraphQL) or close-and-retitle on GitHub, and what the '
        'CLI still expects to be able to read afterwards'
    ),
    'Projects v2: scope-blocked on the probe token (needs read:project), so still unusable',
)


class GitHubForge:
    """GitHub. **NOT IMPLEMENTED, ON PURPOSE**, and this refusal is the honest deliverable.

    THE BLOCKING UNKNOWN IS NOW ANSWERED, AND IT IS GOOD NEWS. The claim protocol passed 4/4 rounds
    on a real GitHub repo -- 16 racers each round, exactly one winner, all 16 comments visible in
    64/64 reads. The zero-ref design is not Gitea-only. See :data:`GITHUB_CONFIRMED`.

    IT IS STILL NOT WRITTEN, AND THE REASON HAS CHANGED RATHER THAN GONE AWAY. The same probes found
    three differences a client cannot paper over (:data:`GITHUB_DIVERGENCES`), and the sharpest is
    that FRESHNESS IS INVERTED: `?labels=` is exact on Gitea and stale for up to 6.6 s on GitHub,
    while text search is stale on Gitea and fresh on GitHub. No query path is fresh on both. That is
    not a client detail -- it refutes a rule this codebase already leans on, and `forge_store`'s
    `_item_number` still draws a "does not exist" conclusion from a list query, which is exactly the
    shape the measurement forbids. Writing this client before settling :data:`GITHUB_UNMEASURED`
    would ship the sixteen-winner bug to the second forge.

    Every Gitea behaviour here was a surprise when raced -- the assignee is not a CAS, duplicate
    labels are accepted, `POST /git/refs` is 405, `?q=` misses issues that exist -- and GitHub then
    inverted one of the conclusions drawn from them. Guessing has a perfect record of being wrong.

    So the class exists, satisfies the protocol structurally, and refuses at the call.
    """

    def __init__(self, repo: str, base_url: str = 'https://api.github.com') -> None:
        self.repo = repo
        self.base_url = base_url

    def _unmeasured(self, what: str) -> NotImplementedError:
        blocking = '\n  - '.join(GITHUB_UNMEASURED)
        differences = '\n  - '.join(GITHUB_DIVERGENCES)
        return NotImplementedError(
            f'GitHubForge.{what} is unwritten. The claim protocol itself IS measured on GitHub and '
            f'passes (4/4 rounds, one winner each), but these differences must be absorbed HERE '
            f'rather than abstracted over:\n  - {differences}\n'
            f'and these remain unmeasured and blocking:\n  - {blocking}'
        )

    def list_work_items(self, *, state: str = 'all') -> list[WorkItem]:
        raise self._unmeasured(f'list_work_items(state={state})')

    def work_item(self, number: int) -> WorkItem | None:
        raise self._unmeasured('work_item')

    def create_work_item(self, *, title: str, body: str, labels: Sequence[str] = ()) -> int:
        raise self._unmeasured(f'create_work_item(labels={list(labels)})')

    def add_comment(self, number: int, body: str) -> int:
        raise self._unmeasured('add_comment')

    def comments(self, number: int) -> list[Comment]:
        raise self._unmeasured('comments')

    def update_comment(self, number: int, comment_id: int, body: str) -> None:
        raise self._unmeasured('update_comment')

    def delete_comment(self, number: int, comment_id: int) -> None:
        raise self._unmeasured('delete_comment')

    def labels(self, number: int) -> list[str]:
        raise self._unmeasured('labels')

    def add_label(self, number: int, name: str) -> None:
        raise self._unmeasured('add_label')

    def remove_label(self, number: int, name: str) -> None:
        raise self._unmeasured('remove_label')

    def close_work_item(self, number: int) -> None:
        raise self._unmeasured('close_work_item')

    def state(self, number: int) -> str:
        raise self._unmeasured('state')

    def set_status(self, sha: str, *, state: str, context: str, description: str) -> None:
        raise self._unmeasured(f'set_status({context}={state})')

    def retire_work_item(self, number: int) -> None:
        raise self._unmeasured('retire_work_item')


#: role -> the forge account it authenticates as. RE-EXPORTED FROM `roles`, NOT DEFINED HERE: this
#: was four literals while `swarmctl.USERS` was a derivation of the same fact, and each of the two
#: docstrings declared ITSELF the only thing deciding a process's identity. A reader believing
#: either would edit one and ship a fleet whose halves disagree about who they are. The name stays
#: because callers import it; it is now an alias rather than a second copy.
ROLE_ACCOUNTS = roles.ACCOUNTS


def default_forge(role: str = 'agent', *, repo: str, base_url: str = DEFAULT_GITEA_BASE_URL) -> Forge:
    """The vendor this swarm uses, acting as `role`, against the repo the CALLER names.

    THE ROLE IS A PARAMETER WITH A DEFAULT, not a constant, and the default is the least privileged
    one that can do the common thing. A no-argument version returned a client whose identity was
    whatever the credential helper happened to hold for the host -- on the measured machine, an
    `OAUTH_USER` entry nothing in this system issued.

    `repo` HAS NO DEFAULT, AND THAT IS THE POINT. It held one project's path, so every caller that
    omitted the argument silently scheduled that project and nothing ever failed to reveal it -- the
    coupling was invisible precisely because the default worked. A second consumer would have found
    out by writing to somebody else's issue tracker. Removing the CONSTANT without removing the
    DEFAULT would have fixed the grep and not the defect.

    Raises:
        ForgeError: `role` is not one of `ROLE_ACCOUNTS`, or `base_url` is not an http(s) URL.
    """
    if role not in ROLE_ACCOUNTS:
        msg = f'role must be one of {sorted(ROLE_ACCOUNTS)}, got {role!r}'
        raise ForgeError(msg)
    return GiteaForge(base_url, repo, username=ROLE_ACCOUNTS[role])
