"""A local `claim_key -> work item number` cache, so existence can be decided by a FRESH read.

WHY THIS COSTS REAL MONEY WITHOUT IT
====================================

`ForgeStore.verdict` answers from a LIST query, and a list is the one read measured stale on GitHub
(22/22, up to 6.36 s). A stale miss reads as "not answered yet", and the loop then re-runs a gate
that already passed. That is a 25-minute re-run, every time it fires, on a fleet that is meant to
run unattended -- a running tax rather than an edge case.

The only read measured fresh on BOTH forges is `GET /issues/{number}` -- a primary-key read, not a
filter (GitHub 0/22 stale, Gitea fresh). But you can only issue it if you already know the number.
This index is what remembers it.

IT IS A CACHE AND IT SAYS SO IN ITS TYPES
=========================================

* **A hit is a HYPOTHESIS**, authoritative only once the by-number read confirms the title matches.
  The forge remains the source of truth; this only shortens the question asked of it.
* **A miss is UNKNOWN, never "absent"** -- `NOT_INDEXED`, the same discipline as `NOT_VISIBLE`. If a
  type can express "absent", something will eventually branch on it and create a duplicate, which
  is precisely the bug that made all of this necessary.
* **A cold or deleted index costs re-runs, never correctness.** Every path degrades to the
  list-based lookup that exists today.

WHAT IT MUST NEVER BECOME: a second source of truth about VERDICTS. It maps keys to numbers, and
that is all. If it ever cached verdict CONTENT, a stale entry would stop costing a re-run and start
returning a wrong answer -- the failure direction would invert, and a wrong green is far worse than
a repeated run. `IndexedLookup` carries a number and nothing else, on purpose.

DURABILITY reuses the spool's lesson rather than re-learning it: `durable.atomic_write`, so a crash
mid-write leaves a scratch file rather than a truncated index. The Windows limitation is the same
one and is NAMED there: `durable.DIRECTORY_FSYNC_AVAILABLE` is False on Windows, so this survives
`kill -9` completely and a power cut only best-effort.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Self

from agent_swarm.durable import atomic_write


class IndexCorruptError(RuntimeError):
    """The index file cannot be read, or an entry points at the wrong work item.

    LOUD RATHER THAN SILENT, and the distinction from a plain miss is the point. A miss is ordinary
    and costs a list lookup. An entry resolving to an item that belongs to a DIFFERENT key means two
    keys have been crossed, and a store that quietly treated that as a miss would keep operating
    with a mapping that is actively wrong -- writing verdicts onto someone else's work item.
    """


class NotIndexed:
    """ "I have no entry for this key." NEVER "there is no such item."

    The same re-typing as `forge_store.NOT_VISIBLE`, for the same reason: `if number is None:
    create()` is the line that produced eight duplicate work items, and the cheapest way to stop it
    being written again is to make `None` un-returnable.
    """

    _instance: ClassVar[NotIndexed | None] = None

    def __new__(cls) -> Self:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return 'NOT_INDEXED'

    def __bool__(self) -> bool:
        return False


#: "No entry." Never "no such item".
NOT_INDEXED = NotIndexed()


@dataclass(frozen=True, slots=True)
class IndexedLookup:
    """A remembered number, and NOTHING ELSE.

    Deliberately not a place to hang a verdict, a state or a title. The moment this type could carry
    an answer, a stale entry would start returning wrong answers instead of costing an extra read --
    and the whole point of the index is that its failure mode stays cheap.
    """

    number: int


class ItemIndex:
    """A JSON file mapping `claim_key -> work item number`.

    Args:
        path: where to persist. Its directory is created on demand -- an index that required its
            directory to pre-exist would silently do nothing on a fresh box, which is the cold-cache
            case pretending to be a working one.

    Raises:
        IndexCorruptError: the file exists and is not readable as an index. A DELETED file is a cold
            cache and is fine; a CORRUPT one is an incident, and treating it as empty would hide
            whatever damaged it. Same stance as the spool's corrupt entry.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._entries: dict[str, int] = self._load()

    def _load(self) -> dict[str, int]:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding='utf-8'))
            return {str(key): int(value) for key, value in raw.items()}
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, AttributeError, TypeError, ValueError) as exc:
            msg = f'unreadable work-item index {self.path}: {exc}'
            raise IndexCorruptError(msg) from exc

    def get(self, key: str) -> IndexedLookup | NotIndexed:
        """What number we last saw for `key`. A HYPOTHESIS -- the caller must confirm it."""
        number = self._entries.get(key)
        return IndexedLookup(number=number) if number is not None else NOT_INDEXED

    def put(self, key: str, number: int) -> None:
        """Remember `key -> number`, durably before returning."""
        self._entries[key] = number
        self._flush()

    def forget(self, key: str) -> None:
        """Drop an entry. HOW THE INDEX SELF-CORRECTS: a hypothesis the forge refused must not be
        offered again, or every lookup pays for the same wrong answer forever."""
        if self._entries.pop(key, None) is not None:
            self._flush()

    def _flush(self) -> None:
        atomic_write(self.path, json.dumps(self._entries, indent=2, sort_keys=True).encode('utf-8'))

    def __len__(self) -> int:
        return len(self._entries)
