"""Test doubles that are SHIPPED, because they carry measured properties worth reusing.

WHY THIS IS IN THE PACKAGE AND NOT IN `tests/`. A consumer needing an in-memory forge would
otherwise write a second one -- a second implementation of one contract, by someone with failing
tests and every incentive to make them pass. That is the "two implementations, only one covered"
defect this package found in `remove_label` on 2026-08-10, recreated deliberately.

AND THE NEW ONE WOULD START WITH NONE OF THE HARDENINGS BELOW, each of which was paid for by being
wrong first:

* **List order is adversarial by construction.** Insertion order made "first match" and
  "lowest-numbered match" the same function, hiding a tie-break defect. Reversing it made "first"
  and "highest" the same function, hiding the next one -- the fix relocated the hole. Rotate-by-half
  leads with neither extreme for three or more items.
* **A label is `(id, name)` and a name maps to SEVERAL ids.** `POST /labels` accepted twelve
  identical names from twelve racers on the measured deployment, so a name-keyed double asserts an
  impossibility. `remove_label` detaching one id left items carrying two verdicts at once.
* **The model is PINNED by tests**, because collapsing `labels()` back to a set made every test pass
  again with the modelled reality gone.
* **A forge CARRIES AN IDENTITY.** `GiteaForge` requires a `username` and the whole
  "only the verifier marks a commit green" boundary rests on it, since Gitea has no scope for commit
  status. A double with no identity cannot be on the wrong side of that boundary, so every test
  written through it agreed the boundary held -- an identity-free double is an assertion that a
  role confusion is impossible.

A double that cannot represent a failure is not a neutral simplification; it is an assertion that
the failure is impossible. These three were all such assertions, and all three were refuted by
measurement against the real server.

VERSIONED, and the version is not decoration -- see :data:`DOUBLE_MODEL_VERSION`.
"""

from __future__ import annotations

import fnmatch
import threading
from collections.abc import Sequence

from agent_swarm.forge import ROLE_ACCOUNTS, STATUS_STATES, Comment, CommentGone, ForgeError, WorkItem
from agent_swarm.refstore import RefUnreachable

#: Bumped whenever this double's MODEL of the forge changes -- a new adverse property, a corrected
#: identity, a refuted simplification.
#:
#: WHY A CONSUMER SHOULD ASSERT IT. A downstream repo imports this from a PINNED `agent_swarm`, not
#: from this working tree, so a hardening added here does not reach that repo's tests until the pin
#: moves. The failure is quiet and it is the bad direction: the consumer's suite passes against an
#: older, gentler double while this repo's suite passes against the newer one -- two repos, one
#: contract, two versions of the instrument. Asserting this constant makes a stale pin RED instead
#: of quietly agreeable.
DOUBLE_MODEL_VERSION = 6


class RecordingForge:
    """An in-memory `Forge` that MODELS THE PRECONDITION, and says so.

    It assigns comment ids from a single counter under a lock -- server-assigned, monotonic, unique
    -- and every read sees every completed write. Those are precisely the two properties the claim
    protocol requires of a deployment, and they are here by CONSTRUCTION.

    So: a green run against this forge is evidence that `ForgeStore`'s arbitration is correct. It is
    NOT evidence that any real forge has these properties, and no amount of it ever will be. That is
    what the `live_forge` tests are for, and why they were measured before this was written.

    It is not a mock of the code under test -- the store's logic runs unmodified against it -- and
    it is a second genuine backend, which is what makes the vendor-neutrality claim checkable.
    """

    def __init__(self, *, username: str = ROLE_ACCOUNTS['agent']) -> None:
        #: WHICH ROLE this forge acts as, mirroring `GiteaForge.username`. It defaults to the LEAST
        #: PRIVILEGED account, as `default_forge` does: a double that defaulted to the verifier would
        #: let every test through the one seam where the role is checked at all, and the seam would
        #: be green because the instrument was privileged rather than because the code was right.
        self.username = username
        self._lock = threading.Lock()
        self._next_id = 1
        self.items: dict[int, WorkItem] = {}
        #: (sha, context) -> (state, description). Keyed, not appended: a status is the
        #: CURRENT answer for one context, and a list would let a test read a green that a
        #: later failure had already replaced.
        self.statuses: dict[tuple[str, str], tuple[str, str]] = {}
        self.bodies: dict[int, str] = {}
        self._comments: dict[int, list[Comment]] = {}
        # LABELS ARE (id, name) PAIRS, and the id is what a real removal targets.
        #
        # This double used to key labels by NAME, which made it gentler than reality on exactly the
        # axis reality has already been MEASURED to differ: `POST /labels` accepted twelve identical
        # names from twelve concurrent racers on this Gitea, so a name maps to a LIST of ids. A
        # name-keyed double cannot express a duplicate at all, so every test written through it
        # agreed that removal by name removes everything -- which is what `remove_label` does here
        # and is NOT what the vendor does.
        self.item_labels: dict[int, list[tuple[int, str]]] = {}
        #: Repo-level label definitions: name -> the ids sharing it. Non-unique BY MEASUREMENT.
        self.repo_labels: dict[str, list[int]] = {}
        self._next_label_id = 1000
        self.retired: list[int] = []

    def list_work_items(self, *, state: str = 'all') -> list[WorkItem]:
        """DETERMINISTICALLY SCRAMBLED. Neither first nor last correlates with the number.

        A real forge promises no ordering on a list endpoint -- Gitea and GitHub both default to
        recently-updated first, and either may change -- so a double that returns a MEANINGFUL order
        is better behaved than reality, and every ordering rule tested through it is really testing
        the double.

        This has now cost two holes, and the second is the more instructive:

        * **Insertion order** made "first match" and "LOWEST-numbered match" the same function, so
          `_lowest_numbered`'s tie-break was untestable.
        * Fixing that by REVERSING made "first match" and "HIGHEST-numbered match" the same
          function, so `newest_open`'s rule was untestable. **The fix relocated the hole rather than
          closing it**, which is this suite's own law about elimination fixes, firing on the repair
          for the previous instance of that law.

        The order here is a ROTATION by half the list, which for three or more items puts neither
        the lowest nor the highest number first -- so it is adverse to both `min` and `max` at once,
        by construction rather than by luck. A digest-based scramble was tried first and is worse:
        deterministic, but whether it happens to lead with the maximum is a property of the
        particular numbers, so it discriminated `min` and not `max`. **Fewer than three items cannot
        discriminate either way** -- with two, "first" IS one of min and max -- which is why the
        crashed-supersede case is also tested at six.

        Not `random.shuffle` and not `hash()`: an intermittent double fails intermittently, which
        reads as flakiness and gets retried away, and `hash()` is salted per process.

        FILTERING HAPPENS HERE, not in the caller, because the whole point of the parameter is that
        the VENDOR does not send what was not asked for. A double that returned everything and let
        the store filter would make a cost control that transmits nothing look like it works.
        """
        with self._lock:
            # Labels attached HERE, as the real listing endpoint returns them. A double that
            # omitted them would leave the store's per-item fetch looking necessary, and the N+1
            # this parameter removes would be invisible to every test using it.
            items = [
                WorkItem(
                    number=x.number,
                    title=x.title,
                    state=x.state,
                    labels=tuple(name for _id, name in self.item_labels.get(x.number, [])),
                )
                for x in self.items.values()
                if state == 'all' or x.state == state
            ]
        half = len(items) // 2
        return items[half:] + items[:half]

    def work_item(self, number: int) -> WorkItem | None:
        """BY NUMBER, and therefore fresh even in `StaleListForge` -- which is the measured shape."""
        with self._lock:
            return self.items.get(number)

    def create_work_item(self, *, title: str, body: str, labels: Sequence[str] = ()) -> int:
        # Ids resolved BEFORE the lock: `label_id` takes it too, and the real client also resolves
        # them before its single POST.
        resolved = [(self.label_id(name), name) for name in labels]
        with self._lock:
            number = len(self.items) + 1
            self.items[number] = WorkItem(number=number, title=title, state='open')
            self.bodies[number] = body
            self._comments[number] = []
            # SET DIRECTLY, not via `add_label`: that would re-enter `self._lock`, and it would
            # also model two operations where the real client now performs one -- a double that
            # needs two calls to do what the client does in one would hide exactly the write
            # amplification this parameter was added to remove.
            self.item_labels[number] = resolved
            return number

    def add_comment(self, number: int, body: str) -> int:
        with self._lock:
            # THE COUNTER IS THE POINT. A per-issue index, or a timestamp, would not be a
            # server-assigned monotonic key and the store's arbitration would be untested.
            comment_id = self._next_id
            self._next_id += 1
            self._comments[number].append(Comment(id=comment_id, body=body))
            return comment_id

    def comments(self, number: int) -> list[Comment]:
        with self._lock:
            return list(self._comments[number])

    def update_comment(self, number: int, comment_id: int, body: str) -> None:
        with self._lock:
            existing = self._comments[number]
            if not any(c.id == comment_id for c in existing):
                # THE DOUBLE MUST BE AS UNFORGIVING AS THE FORGE HERE. Gitea answers 404 for an edit
                # of a pruned comment, and a fake that silently no-op'd would let a runner believe
                # it had beaten -- the exact failure the distinction exists to prevent.
                msg = f'comment {comment_id} on item {number} no longer exists; re-create it'
                raise CommentGone(msg)
            self._comments[number] = [Comment(id=c.id, body=body) if c.id == comment_id else c for c in existing]

    def delete_comment(self, number: int, comment_id: int) -> None:
        with self._lock:
            self._comments[number] = [c for c in self._comments[number] if c.id != comment_id]

    def labels(self, number: int) -> list[str]:
        """NAMES, because that is what the `Forge` protocol promises. Ids stop at this boundary --
        and the fact that a name can appear TWICE in this list is the whole point of the change."""
        with self._lock:
            return [name for _id, name in self.item_labels[number]]

    def label_id(self, name: str) -> int:
        """The LOWEST id for `name`, created if absent -- `GiteaForge._label_id`'s rule.

        Mirrored deliberately rather than simplified: it is the rule that makes every runner
        converge on one id without coordinating, and a double that picked differently would make
        the store's convergence untestable.
        """
        with self._lock:
            ids = self.repo_labels.get(name)
            if not ids:
                self._next_label_id += 1
                self.repo_labels[name] = [self._next_label_id]
                return self._next_label_id
            return min(ids)

    def define_duplicate_label(self, name: str) -> int:
        """Create a SECOND repo label with an existing name and a higher id, and return it.

        NOT PART OF THE `Forge` PROTOCOL. It reproduces a state the vendor reaches and our code
        cannot: a losing racer's label, a human adding one in the web UI, an older client. Twelve
        of these were created on the real server in the CAS measurement, so this is reproduction,
        not invention.
        """
        with self._lock:
            self._next_label_id += 1
            self.repo_labels.setdefault(name, []).append(self._next_label_id)
            return self._next_label_id

    def attach_label_id(self, number: int, label_id: int, name: str) -> None:
        """Attach a SPECIFIC label id. The escape hatch for planting the duplicate above."""
        with self._lock:
            self.item_labels[number].append((label_id, name))

    def add_label(self, number: int, name: str) -> None:
        label_id = self.label_id(name)
        with self._lock:
            self.item_labels[number].append((label_id, name))

    def remove_label(self, number: int, name: str) -> None:
        """Detaches EVERY id sharing this name -- mirroring `GiteaForge.remove_label`.

        Until 2026-08-10 both this and the vendor wrapper removed only the ONE id `_label_id`
        converges on, so a same-named label attached under a higher id survived and left an item
        carrying two verdicts at once. The double could not express that at all while it keyed
        labels by name, which is why the bug lived behind a green suite.
        """
        with self._lock:
            ids = set(self.repo_labels.get(name, []))
            self.item_labels[number] = [(i, n) for i, n in self.item_labels[number] if i not in ids]

    def close_work_item(self, number: int) -> None:
        """Closes. TOUCHES NO LABELS, because the measured server does not.

        This used to take a `labels` replacement set and apply it -- modelling Gitea's issue edit as
        applying `state` and `labels` in one PATCH. Measured against the live server 2026-08-10: the
        PATCH returns 200, the item closes, and the labels are NOT applied. So the double was better
        behaved than reality on the exact axis a cost claim rested on, and every test through it
        agreed that a verdict cost 3 round trips when it costs 4.
        """
        with self._lock:
            item = self.items[number]
            self.items[number] = WorkItem(number=number, title=item.title, state='closed')

    def reopen_work_item(self, number: int) -> None:
        """NOT part of the `Forge` protocol -- deliberately.

        Reopening is something a HUMAN does in the web UI, or a retry policy that lives above this
        layer; no code here needs to do it, and adding it to the protocol would oblige every
        backend to implement an operation nobody calls. This double grows it because a double's job
        is to reproduce states the real world can reach, not only the ones our code writes.
        """
        with self._lock:
            self.items[number] = WorkItem(number=number, title=self.items[number].title, state='open')

    def state(self, number: int) -> str:
        with self._lock:
            return self.items[number].state

    def set_status(self, sha: str, *, state: str, context: str, description: str) -> None:
        """Recorded per (sha, context), LAST WRITE WINS -- which is what a real forge does.

        A double that appended would let a test assert a commit is green while a later `failure` is
        sitting behind it in a list nobody reads. The whole value of a status is that it is the
        CURRENT answer for one context.
        """
        if state not in STATUS_STATES:
            msg = f'state must be one of {sorted(STATUS_STATES)}, got {state!r}'
            raise ForgeError(msg)
        with self._lock:
            self.statuses[sha, context] = (state, description)

    def retire_work_item(self, number: int) -> None:
        """Closes AND retitles, because a retired item must stop matching its title.

        The fake used only to close, which made it gentler than every real forge: retired items went
        on answering title lookups, so `reconcile_duplicates` would have retired the same ones
        forever and the test that counts survivors could never pass. Exactly the audit question from
        the double note -- is this double better-behaved than reality, and does the difference hide
        a failure class -- caught here by the sweep rather than in production.
        """
        self.retired.append(number)
        with self._lock:
            current = self.items[number]
            suffix = '' if current.title.endswith(' (retired)') else ' (retired)'
            self.items[number] = WorkItem(number=number, title=f'{current.title}{suffix}', state='closed')


class InMemoryRefStore:
    """An :class:`agent_swarm.refstore.RefStore` that behaves like git, including where git is
    inconvenient.

    THE ADVERSARIAL PROPERTY THAT MATTERS MOST IS THE GLOB, AND IT IS NOT THE ONE EVERYONE WRITES
    DOWN. This double was FIRST WRITTEN with segment-bounded matching -- `*` stops at a `/` -- on
    the strength of a sentence repeated in two repositories' comments. The audit below
    (`test_the_double_has_the_SAME_glob_semantics`) ran it against a real bare remote and refuted
    it. MEASURED 2026-08-12, git 2.x, `ls-remote`:

        pattern                        refs/ci/heartbeat/boxA/17
        refs/ci/heartbeat/*            MATCHES     -- `*` crosses `/`
        refs/ci/heartbeat/*/*          matches
        refs/ci/*                      MATCHES     -- and crosses two
        boxA/17                        MATCHES     -- a TAIL, at a `/` boundary
        17                             MATCHES     -- likewise
        eartbeat/*                     no          -- a tail must start at a boundary
        refs/ci/heartbeat/boxA         no          -- a prefix is not a tail

    So the rule is: the pattern is fnmatch-ed (with `*` crossing `/`) against the whole refname OR
    against any tail of it beginning after a `/`. A segment-bounded double is STRICTER than git,
    which is the dangerous direction here -- it would have let a test assert that a pattern one
    wildcard short finds nothing, when against the real remote it finds everything.

    THE OTHER THREE, each paid for by being wrong first somewhere in this design:

    * **Listing order is rotated**, not insertion order, so "the first one back" and "the newest
      one" can never be the same function by accident -- the rule that made a string-sorted maximum
      look correct until a digit count changed.
    * **A write can be made to FAIL** (`fail_writes`), because the code path that matters most in
      liveness is the one after a failed publish: pruning there empties the namespace and a healthy
      box reads as dead.
    * **A listing can be made UNREACHABLE** (`unreachable`), because "I could not ask" and "there is
      nothing there" are different answers and the whole seam exists to keep them apart.

    Deleting an absent ref is NOT an error, matching git: a prune that raced another prune has still
    achieved what it wanted.
    """

    def __init__(self, *, head: str = 'a' * 40) -> None:
        self.refs: dict[str, str] = {}
        self._head = head
        #: Set to a string to make every `write` fail with it as the transport's words.
        self.fail_writes: str | None = None
        #: Set to make every `list` raise, as an offline box does.
        self.unreachable = False
        #: Every write and delete, in order, so a test can assert the ORDER of a publish and its
        #: prune -- which is the property that matters and is invisible in the final state.
        self.log: list[str] = []

    def head(self) -> str:
        return self._head

    @staticmethod
    def _matches(ref: str, pattern: str) -> bool:
        """Git's `ls-remote` glob, as MEASURED rather than as remembered -- see the class docstring.

        The whole refname, or any tail of it starting after a `/`, fnmatch-ed against the pattern.
        `fnmatch` is the right primitive precisely because its `*` crosses `/`, which git's does.
        """
        candidates = [ref, *(ref[i + 1 :] for i, ch in enumerate(ref) if ch == '/')]
        return any(fnmatch.fnmatchcase(candidate, pattern) for candidate in candidates)

    def list(self, pattern: str) -> dict[str, str]:
        if self.unreachable:
            msg = f'cannot list {pattern!r}: the double was told the remote is unreachable'
            raise RefUnreachable(msg)
        found = [(ref, sha) for ref, sha in self.refs.items() if self._matches(ref, pattern)]
        half = len(found) // 2
        return dict(found[half:] + found[:half])

    def write(self, ref: str, commit: str) -> tuple[bool, str]:
        if self.fail_writes is not None:
            self.log.append(f'FAILED write {ref}')
            return False, self.fail_writes
        self.refs[ref] = commit
        self.log.append(f'write {ref}')
        return True, ''

    def delete(self, ref: str) -> bool:
        """Reports whether the DELETE SUCCEEDED, which for git is true even when nothing was there.

        MEASURED: `git push --delete` of a non-existent ref warns and exits 0. A double that
        returned False for an absent ref would be reporting a distinction the real transport does
        not make, and a caller written against it would branch on a value that is always True in
        production.
        """
        self.log.append(f'delete {ref}')
        self.refs.pop(ref, None)
        return True
