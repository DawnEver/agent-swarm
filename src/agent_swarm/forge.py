"""The forge: pure storage and a UI, and nothing else.

WHAT A FORGE IS FOR, AND WHAT IT IS NOT FOR
===========================================

Gitea and GitHub are demoted here to storage plus a human-readable surface (user directive:
"把 Gitea/GitHub 降级为单纯的纯粹存储与 UI 界面, 业务逻辑和调度机制 100% 掌握在自己的 CLI 工具里").
Every decision -- what a claim key is, how long a lease lasts, which words a verdict may take, when
an item is retired -- lives in `forge_store.ForgeStore` and in `admission`. This file holds only the
I/O, and it holds it once per vendor.

THE SEAM IS DRAWN WHERE THE VENDORS DIFFER, which is why these methods are so small and so dull.
Anything two forges do differently -- label identity (Gitea labels are ids, so a name must be
resolved and possibly created), pagination limits, state vocabulary, whether an item can be DELETED
or only closed -- must be absorbed HERE. A vendor conditional in the store would mean the scheduler
has two behaviours, and only one of them would ever be tested.

WHAT IS DELIBERATELY ABSENT: the claim. `try_claim` is a compare-and-swap and no forge API offers
one -- see `forge_store` for the measurements. The claim is a git ref push, git is the same program
against both forges, and so the atomic primitive never enters this file at all. A forge is asked
only for the URL to push at.
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


@runtime_checkable
class Forge(Protocol):
    """Storage and UI. Every method is I/O; not one of them decides anything.

    THE TEST FOR WHETHER SOMETHING BELONGS HERE: could two vendors answer it differently? If yes it
    is a forge method. If no -- a lease expiry, a verdict vocabulary, a title format -- it belongs
    in the store, where it is written once and tested once.
    """

    def git_url(self) -> str:
        """Where claim refs are pushed. The ONLY thing the claim mechanism asks of a forge."""
        ...

    def list_work_items(self) -> list[WorkItem]:
        """Every item, open and closed. Paginated by the vendor client, not by the caller."""
        ...

    def create_work_item(self, *, title: str, body: str) -> int: ...

    def add_comment(self, number: int, body: str) -> None: ...

    def comments(self, number: int) -> list[str]:
        """Comment bodies, oldest first."""
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

    Construction performs NO I/O -- not a connection, not a credential read. A store built against
    an unreachable host must still refuse a bad verdict word, and that refusal must be about the
    word rather than about the network.
    """

    def __init__(self, base_url: str, repo: str) -> None:
        self.base_url = base_url.rstrip('/')
        self.repo = repo
        self._token: str | None = None
        self._label_ids: dict[str, int] = {}

    def git_url(self) -> str:
        """The plain URL. THE TOKEN IS NEVER PUT HERE -- git's credential helper supplies it, and a
        URL carrying a secret is persisted into `.git/config` and echoed by every git trace."""
        return f'{self.base_url}/{self.repo}.git'

    def list_work_items(self) -> list[WorkItem]:
        """A plain listing, NOT `?q=`.

        `GET /issues?q=<title>` returned ZERO hits for an issue that demonstrably existed on this
        deployment -- the issue indexer is not populated. A verdict lookup built on it would read
        "not answered yet" for an answered job, which is the unearned green the vocabulary exists
        to prevent. Vendor defect, absorbed here: the store just asks for the list.
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

    def add_comment(self, number: int, body: str) -> None:
        self._api('POST', f'/repos/{self.repo}/issues/{number}/comments', {'body': body})

    def comments(self, number: int) -> list[str]:
        return [c['body'] for c in self._api('GET', f'/repos/{self.repo}/issues/{number}/comments') or []]

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
        item = self._api('GET', f'/repos/{self.repo}/issues/{number}')
        title = item['title']
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


#: What must be MEASURED before `GitHubForge` can be written. Not read in a document -- raced, the
#: way the Gitea numbers in `forge_store` were, because every one of these was a surprise there.
GITHUB_UNMEASURED = (
    'label identity: are GitHub labels addressed by name (no id lookup, no create step)?',
    'label creation: does adding an unknown label to an issue create it, or 422?',
    'pagination: the per_page ceiling, and whether `state=all` is the same spelling',
    'search: is the issues index reliable, or does it lag as Gitea 1.26.4 does?',
    'deletion: can an issue be deleted at all, or is close-and-retitle the only retirement?',
    'the git URL and how the credential helper is keyed for github.com',
    'rate limiting: what a runner fleet does to the hourly budget, and what a 403 looks like',
)


class GitHubForge:
    """GitHub. **NOT IMPLEMENTED, ON PURPOSE**, and this refusal is the honest deliverable.

    The user's constraint is that the system work on BOTH forges, and the seam for it is `Forge`.
    What is NOT available is a GitHub instance to race, and every single Gitea behaviour this
    package depends on was a SURPRISE when measured: the assignee is not a CAS, `POST /labels`
    accepts duplicates, `POST /git/refs` is 405, and `?q=` returns nothing for issues that exist. A
    GitHub client written from the documentation would be four more guesses wearing the same
    interface -- a declaration that lies, which this project treats as its dominant defect class.

    So the class exists, satisfies the protocol structurally, and refuses at the call. See
    :data:`GITHUB_UNMEASURED` for the list; each line is one experiment.
    """

    def __init__(self, repo: str, base_url: str = 'https://api.github.com') -> None:
        self.repo = repo
        self.base_url = base_url

    def _unmeasured(self, what: str) -> NotImplementedError:
        lines = '\n  - '.join(GITHUB_UNMEASURED)
        return NotImplementedError(
            f'GitHubForge.{what} is unwritten because GitHub has not been measured. '
            f'Race these against a real repo first, then write it:\n  - {lines}'
        )

    def git_url(self) -> str:
        raise self._unmeasured('git_url')

    def list_work_items(self) -> list[WorkItem]:
        raise self._unmeasured('list_work_items')

    def create_work_item(self, *, title: str, body: str) -> int:
        raise self._unmeasured('create_work_item')

    def add_comment(self, number: int, body: str) -> None:
        raise self._unmeasured('add_comment')

    def comments(self, number: int) -> list[str]:
        raise self._unmeasured('comments')

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
