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

import json
import os
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

_HTTP_TIMEOUT = 60.0

#: This project's forge. It lives in the VENDOR module, not in the store: a default is a choice of
#: vendor, and a store holding one would name a vendor in the one file that must not.
DEFAULT_GITEA_BASE_URL = 'http://server.mingyangbao.site:9000'
DEFAULT_REPO = 'Tianjie-Zou-Team/motronics-studio'

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


@dataclass(frozen=True, slots=True)
class WorkItem:
    """One issue, in vendor-neutral terms. `state` is 'open' or 'closed' and nothing else."""

    number: int
    title: str
    state: str


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

    def list_work_items(self) -> list[WorkItem]:
        """Every item, open and closed. Paginated by the vendor client, not by the caller."""
        ...

    def create_work_item(self, *, title: str, body: str) -> int: ...

    def add_comment(self, number: int, body: str) -> int:
        """Post a comment, returning the SERVER-ASSIGNED id.

        Returning the id is not a convenience. It is the claim protocol's ordering key, and a
        vendor that could not supply one at insert could not host a claim at all.
        """
        ...

    def comments(self, number: int) -> list[Comment]:
        """Every comment, oldest first, carrying its server id."""
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

    def close_work_item(self, number: int) -> None: ...

    def state(self, number: int) -> str: ...

    def retire_work_item(self, number: int) -> None:
        """Make the item stop counting, by whatever means the vendor allows.

        THE VERB IS DELIBERATELY NOT "DELETE". Gitea 1.26.4 on our deployment closes-and-retitles;
        another forge may hard-delete. The store must not care which, or it would grow a vendor
        conditional in exactly the place -- cleanup -- where nobody would ever notice it was wrong.
        """
        ...


class GiteaForge:
    """Gitea over its REST API. Measured against `server.mingyangbao.site:9000`, Gitea 1.26.4.

    Args:
        base_url: the Gitea root, no trailing slash.
        repo: ``owner/name``.

    MEASURED, not assumed: `POST .../comments` returns a server-assigned id; three successive posts
    came back 595, 596, 597 -- monotonic and unique -- and `DELETE .../issues/comments/{id}` removed
    the middle one (204) leaving [595, 597]. The sixteen-way behaviour the claim protocol needs is
    recorded in `forge_store`'s docstring.

    Construction performs NO I/O -- not a connection, not a credential read. A store built against
    an unreachable host must still refuse a bad verdict word, and that refusal must be about the
    word rather than about the network.
    """

    def __init__(self, base_url: str, repo: str) -> None:
        self.base_url = base_url.rstrip('/')
        self.repo = repo
        self._token: str | None = None
        self._label_ids: dict[str, int] = {}

    def list_work_items(self) -> list[WorkItem]:
        """A plain listing, NOT `?q=`.

        `GET /issues?q=<title>` returned ZERO hits for an issue that demonstrably existed on this
        deployment -- the issue indexer is not populated. A lookup built on it would read "no such
        work item" for one that exists, so two runners would create two items for one job and each
        would claim its own. Vendor defect, absorbed here: the store just asks for the list.
        """
        out: list[WorkItem] = []
        page, limit = 1, 50
        while True:
            batch = self._api('GET', f'/repos/{self.repo}/issues?state=all&type=issues&limit={limit}&page={page}') or []
            out.extend(WorkItem(number=x['number'], title=x['title'], state=x['state']) for x in batch)
            if len(batch) < limit:
                return out
            page += 1

    def create_work_item(self, *, title: str, body: str) -> int:
        return self._api('POST', f'/repos/{self.repo}/issues', {'title': title, 'body': body})['number']

    def add_comment(self, number: int, body: str) -> int:
        return self._api('POST', f'/repos/{self.repo}/issues/{number}/comments', {'body': body})['id']

    def comments(self, number: int) -> list[Comment]:
        raw = self._api('GET', f'/repos/{self.repo}/issues/{number}/comments') or []
        return [Comment(id=c['id'], body=c['body']) for c in raw]

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
        self._api('DELETE', f'/repos/{self.repo}/issues/{number}/labels/{self._label_id(name)}')

    def close_work_item(self, number: int) -> None:
        self._api('PATCH', f'/repos/{self.repo}/issues/{number}', {'state': 'closed'})

    def state(self, number: int) -> str:
        return self._api('GET', f'/repos/{self.repo}/issues/{number}')['state']

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
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(f'{self.base_url}/api/v1{path}', data=data, method=method)
        request.add_header('Authorization', f'token {self._credential()}')
        request.add_header('Content-Type', 'application/json')
        try:
            with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            # The RESPONSE body is echoed because a refusal explains itself there. The REQUEST is
            # never echoed: it carries the credential header.
            msg = f'{method} {path} -> {exc.code}: {exc.read().decode(errors="replace")[:400]}'
            raise ForgeError(msg) from None
        return json.loads(raw) if raw else None

    def _credential(self) -> str:
        """The API token, from `git credential fill`, held in memory only.

        NEVER LOGGED, PRINTED OR PERSISTED -- a hard project invariant. Note what this does NOT do:
        it takes no token argument (which would invite a caller to hard-code one) and it reads no
        environment variable at import time.
        """
        if self._token is not None:
            return self._token
        host = urllib.parse.urlsplit(self.base_url)
        filled = subprocess.run(
            ['git', 'credential', 'fill'],
            input=f'protocol={host.scheme}\nhost={host.netloc}\n\n',
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, 'GIT_TERMINAL_PROMPT': '0'},
        )
        for line in filled.stdout.splitlines():
            if line.startswith('password='):
                self._token = line[len('password=') :]
                return self._token
        msg = f'no stored credential for {host.netloc}'
        raise ForgeError(msg)


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

    def list_work_items(self) -> list[WorkItem]:
        raise self._unmeasured('list_work_items')

    def create_work_item(self, *, title: str, body: str) -> int:
        raise self._unmeasured('create_work_item')

    def add_comment(self, number: int, body: str) -> int:
        raise self._unmeasured('add_comment')

    def comments(self, number: int) -> list[Comment]:
        raise self._unmeasured('comments')

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

    def retire_work_item(self, number: int) -> None:
        raise self._unmeasured('retire_work_item')


def default_forge() -> Forge:
    """The forge this project actually uses. One place to change when a second one is measured."""
    return GiteaForge(DEFAULT_GITEA_BASE_URL, DEFAULT_REPO)
