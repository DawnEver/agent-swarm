"""A durable verdict spool: record to disk BEFORE publishing, replay what did not land.

WHY THIS EXISTS AT ALL
======================

The ref design got this for free and nobody had to think about it. `_write_ref` deliberately KEEPS
the local ref when the push fails, so a later tick republishes rather than re-runs -- git's object
store is a local mirror of everything you are about to send. **An Issue has no local mirror.** Under
the zero-ref schema a gate that ran for 25 minutes and then failed its `POST` has produced nothing
at all, and nothing anywhere says so. Re-running it is not a recovery; it is another 25 minutes and
another chance to lose the answer.

So the order is inverted and that inversion IS the design: the verdict lands on disk first, and
publishing is a separate step that merely marks it sent. The window in which a verdict exists only
in a Python object is the duration of one `os.replace`.

WHY REPLAY NEEDS AN ID, AND WHY THE FORGE HAS TO CARRY IT
=========================================================

Publishing is not atomic and the forge offers no conditional write, so there is an instant where the
comment exists and the spool still says pending: **crash-after-POST-before-mark**. A replay that
trusted the spool alone would post the verdict twice -- one job, two verdict comments, and a reader
with no way to tell which is current.

The fix is that the spool assigns an id at record time and echoes it into the published comment as
`[spool:<id>]`. Replay asks the forge, not its own bookkeeping. The id is per-ENTRY and not per-job
on purpose: a retry after INCONCLUSIVE is a legitimate second verdict, and keying idempotence on the
job would silently drop it -- the job would keep its stale INCONCLUSIVE and the retry that fixed it
would look published.

PUBLISHING CONVERGES, IT DOES NOT REPEAT
========================================

`ForgeStore.record_verdict` is comment -> label -> close, three calls, not atomic. A crash between
the comment and the label leaves the marker present and `verdict()` answering `None` -- so a
publisher that treated "marker present" as "done" would leave the job permanently unanswered while
reporting success. `ForgePublisher.publish` therefore drives the item to the DESIRED STATE: if the
marker is already there it finishes the label and the close; only if it is absent does it post.

WHAT THIS DOES NOT PROTECT AGAINST -- named, because a durability claim that overstates itself is
worse than none:

* **Loss of the disk.** This is local durability. A box that dies with its filesystem takes its
  unpublished verdicts with it.
* **Power loss, as distinct from process death.** Entries are written with `fsync` and swapped with
  `os.replace`, which closes `kill -9` completely. Surviving a power cut additionally requires the
  directory entry itself to be durable, and Windows exposes no directory `fsync`; the call is
  best-effort and silently unavailable there.
* **Two boxes spooling the same job.** The claim protocol is what prevents that. If it is bypassed,
  two spools will each publish, and the later drain wins with no arbitration.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from agent_swarm.forge_store import VERDICT_LABELS, ForgeStore
from agent_swarm.job import Job, JobKind
from agent_swarm.store import VERDICTS

#: How long an entry may sit unpublished before the spool starts refusing to be quiet about it.
#: Long enough that a forge blip fixed by the next tick is not an incident; short enough that
#: "no verdicts since Tuesday" cannot happen.
DEFAULT_STALE_AFTER_SECONDS = 900.0

_ENTRY_SUFFIX = '.json'
_SCRATCH_SUFFIX = '.json.tmp'


class SpoolError(RuntimeError):
    """Something is wrong with the spool itself, as opposed to with a publish."""


class SpoolCorruptError(SpoolError):
    """An entry on disk cannot be read as a verdict.

    RAISED RATHER THAN SKIPPED. A reader that quietly ignored unparseable files would turn a damaged
    verdict into no verdict at all, and the spool would then look empty and healthy -- which is the
    precise failure it was built to prevent, reintroduced by its own error handling.
    """


class SpoolBacklogError(SpoolError):
    """Entries have been undrainable long enough to be an incident.

    A RAISE AND NOT A RETURN VALUE. Silent accumulation is how "the runners are working" and "no
    verdicts since Tuesday" coexist for a week, and a caller can ignore a report field simply by not
    reading it. It cannot ignore this.
    """


@dataclass(frozen=True, slots=True)
class SpoolEntry:
    """One recorded verdict, durable before anyone tries to publish it."""

    id: str
    job: Job
    verdict: str
    detail: str
    recorded_at: float

    def marker(self) -> str:
        """What identifies this entry in a published comment. The replay's whole basis."""
        return f'[spool:{self.id}]'

    def age(self, *, now: float) -> float:
        return now - self.recorded_at


@dataclass(slots=True)
class DrainReport:
    """What one drain did. `failed` carries `(entry id, reason)`."""

    published: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)


@runtime_checkable
class Publisher(Protocol):
    """Somewhere a recorded verdict can be sent. Deliberately two methods, not one.

    `is_published` exists because the spool cannot know, after a crash, whether its last publish
    landed. Only the destination can answer that, and a publisher that could not be asked would
    force the spool to guess -- which is duplication or loss, depending on which way it guesses.
    """

    def publish(self, entry: SpoolEntry) -> None:
        """Make the destination reflect `entry`. MUST converge rather than append."""
        ...

    def is_published(self, entry: SpoolEntry) -> bool:
        """Is `entry` fully reflected at the destination already?"""
        ...


class Spool:
    """A directory of verdicts awaiting publication.

    Layout is two directories and one atomic rename between them::

        <root>/pending/<id>.json     recorded, not yet published
        <root>/published/<id>.json   published; kept as the audit trail

    THE MARK IS A RENAME because a rename is atomic and a rewrite is not. Marking by editing a flag
    inside the file would reintroduce, in the bookkeeping, exactly the partial-write hazard the
    entries themselves are protected from.

    Published entries are KEPT, not deleted: otherwise "we never recorded that" and "we published it
    and threw the evidence away" are indistinguishable afterwards.

    Args:
        root: the spool directory. Created on demand, including parents -- a spool that required its
            directory to pre-exist would silently do nothing on a fresh box.
        stale_after_seconds: how old a pending entry may get before `drain` raises.
    """

    def __init__(self, root: Path | str, *, stale_after_seconds: float = DEFAULT_STALE_AFTER_SECONDS) -> None:
        self.root = Path(root)
        self.pending_dir = self.root / 'pending'
        self.published_dir = self.root / 'published'
        self.stale_after_seconds = stale_after_seconds
        self.pending_dir.mkdir(parents=True, exist_ok=True)
        self.published_dir.mkdir(parents=True, exist_ok=True)

    # -- recording ---------------------------------------------------------------------------

    def record(self, job: Job, *, verdict: str, detail: str) -> SpoolEntry:
        """Put a verdict on disk. Returns once it is durable.

        Raises:
            ValueError: `verdict` is outside :data:`~agent_swarm.store.VERDICTS`. VALIDATED BEFORE
                THE WRITE: an unpublishable entry recorded now is one that sits in the spool
                forever, tripping the backlog alarm for a reason the alarm does not name.
        """
        if verdict not in VERDICTS:
            msg = f'verdict must be one of {sorted(VERDICTS)}, got {verdict!r}'
            raise ValueError(msg)

        # A random id, not a clock or a hash of the job: sixteen threads recording in the same
        # millisecond must not overwrite each other, and each one's `record` returned successfully
        # so the loss would be invisible.
        entry = SpoolEntry(id=uuid.uuid4().hex, job=job, verdict=verdict, detail=detail, recorded_at=time.time())
        _atomic_write(self.pending_dir / f'{entry.id}{_ENTRY_SUFFIX}', _encode(entry))
        return entry

    def pending(self) -> list[SpoolEntry]:
        """Every unpublished entry, oldest first.

        Raises:
            SpoolCorruptError: an entry file cannot be read. See the class docstring for why this is
                not a skip.
        """
        entries = [_decode(path) for path in sorted(self.pending_dir.glob(f'*{_ENTRY_SUFFIX}'))]
        return sorted(entries, key=lambda e: e.recorded_at)

    def mark_published(self, entry: SpoolEntry) -> None:
        """Move `entry` out of the pending set. Atomic; safe to call on an already-marked entry."""
        source = self.pending_dir / f'{entry.id}{_ENTRY_SUFFIX}'
        if source.exists():
            os.replace(source, self.published_dir / f'{entry.id}{_ENTRY_SUFFIX}')

    # -- draining ----------------------------------------------------------------------------

    def drain(self, publisher: Publisher, *, now: float | None = None) -> DrainReport:
        """Publish everything pending, then complain if anything is stuck.

        A per-entry failure does NOT abort the drain: one poisoned entry must not hold every later
        verdict hostage, and the backlog alarm would then blame the wrong thing.

        Raises:
            SpoolBacklogError: entries remain pending past `stale_after_seconds`. Raised AFTER the
                work, so a drain that clears the backlog is silent and one that cannot is not.
        """
        report = DrainReport()
        for entry in self.pending():
            try:
                if not publisher.is_published(entry):
                    publisher.publish(entry)
                self.mark_published(entry)
            except Exception as exc:  # noqa: BLE001 -- ANY publish failure must leave the entry pending
                report.failed.append((entry.id, repr(exc)))
            else:
                report.published.append(entry.id)

        stale = self.stale_entries(now=now)
        if stale:
            oldest = max(entry.age(now=now if now is not None else time.time()) for entry in stale)
            msg = (
                f'{len(stale)} verdict(s) stuck in {self.pending_dir}, oldest {oldest:.0f}s '
                f'(limit {self.stale_after_seconds:.0f}s): {sorted(e.id for e in stale)}'
            )
            raise SpoolBacklogError(msg)
        return report

    def stale_entries(self, *, now: float | None = None) -> list[SpoolEntry]:
        """Pending entries older than the limit. Readable WITHOUT publishing anything, so a monitor
        can ask the question without taking the action."""
        moment = time.time() if now is None else now
        return [entry for entry in self.pending() if entry.age(now=moment) > self.stale_after_seconds]


class ForgePublisher:
    """Publishes spooled verdicts through a `ForgeStore`, idempotently.

    IDEMPOTENCE IS THE FORGE'S ANSWER, NOT THE SPOOL'S. After a crash the spool cannot know whether
    its last `POST` landed, so this asks the destination: the entry's marker is echoed into the
    comment body and scanned for on replay.

    "PUBLISHED" MEANS THE WHOLE THING. `record_verdict` is comment -> label -> close and is not
    atomic, so the marker alone does not mean done -- a crash between the comment and the label
    leaves `verdict()` answering `None`. `is_published` therefore requires the marker AND the
    verdict label AND the closed state, and `publish` finishes whatever is missing instead of
    posting again.
    """

    def __init__(self, store: ForgeStore) -> None:
        self.store = store

    def publish(self, entry: SpoolEntry) -> None:
        number = self.store.work_item_number(entry.job, create=True)
        if self._marker_present(number, entry):
            # The comment landed and the process died before the rest. Finish it; do not repeat it.
            self._apply_verdict_state(number, entry)
            return
        self.store.record_verdict(entry.job, verdict=entry.verdict, detail=f'{entry.detail}\n\n{entry.marker()}')

    def is_published(self, entry: SpoolEntry) -> bool:
        number = self.store.work_item_number(entry.job)
        if number is None:
            return False
        if not self._marker_present(number, entry):
            return False
        return (
            VERDICT_LABELS[entry.verdict] in self.store.forge.labels(number)
            and self.store.forge.state(number) == 'closed'
        )

    def _marker_present(self, number: int, entry: SpoolEntry) -> bool:
        return any(entry.marker() in comment.body for comment in self.store.forge.comments(number))

    def _apply_verdict_state(self, number: int, entry: SpoolEntry) -> None:
        wanted = VERDICT_LABELS[entry.verdict]
        present = self.store.forge.labels(number)
        for label in present:
            if label in VERDICT_LABELS.values() and label != wanted:
                self.store.forge.remove_label(number, label)
        if wanted not in present:
            self.store.forge.add_label(number, wanted)
        if self.store.forge.state(number) != 'closed':
            self.store.forge.close_work_item(number)


# --------------------------------------------------------------------------------------------


def _encode(entry: SpoolEntry) -> bytes:
    """Readable JSON, so an operator recovering a box can read a verdict without this package.

    ONLY THE IDENTITY FIELDS OF THE JOB ARE STORED. `ram_gib`, `exclusivity`, `solo_seconds` and
    `ceiling_seconds` price a job for `admission`; they are not part of `claim_key`, so they cannot
    change where a verdict is published. Storing them would invite a reader to trust a stale price.
    """
    payload = {
        'id': entry.id,
        'recorded_at': entry.recorded_at,
        'verdict': entry.verdict,
        'detail': entry.detail,
        'job': {
            'id': entry.job.id,
            'kind': entry.job.kind.value,
            'shard': entry.job.shard,
            'n_shards': entry.job.n_shards,
        },
    }
    return json.dumps(payload, indent=2, sort_keys=True).encode('utf-8')


def _decode(path: Path) -> SpoolEntry:
    try:
        raw: Any = json.loads(path.read_text(encoding='utf-8'))
        job = raw['job']
        return SpoolEntry(
            id=str(raw['id']),
            job=Job(id=str(job['id']), kind=JobKind(job['kind']), shard=job['shard'], n_shards=job['n_shards']),
            verdict=str(raw['verdict']),
            detail=str(raw['detail']),
            recorded_at=float(raw['recorded_at']),
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        msg = f'unreadable spool entry {path}: {exc}'
        raise SpoolCorruptError(msg) from exc


def _atomic_write(path: Path, data: bytes) -> None:
    """Write `path` so that no reader ever sees a partial one.

    The scratch file carries a suffix the reader does not glob, so a crash mid-write leaves litter
    rather than a truncated entry -- and litter is not corruption, so it must not trip the alarm.
    """
    scratch = path.with_name(path.name.replace(_ENTRY_SUFFIX, _SCRATCH_SUFFIX))
    with open(scratch, 'wb') as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(scratch, path)
    _fsync_directory(path.parent)


def _fsync_directory(directory: Path) -> None:
    """Make the directory entry itself durable, where the platform allows it.

    BEST EFFORT, AND THAT LIMIT IS REAL: Windows has no directory fsync, so on Windows this is a
    no-op and the spool survives `kill -9` but not necessarily a power cut. Said here rather than
    implied by its absence.
    """
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)
