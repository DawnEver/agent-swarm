"""The store: where jobs live, how they are claimed, and where verdicts are recorded.

CLAIMING IS COMPARE-AND-SWAP, AND THAT IS THE POINT OF THIS INTERFACE.

motronics' `ci_tick.claim()` -- the code this layer was extracted from -- is push-then-arbitrate,
not CAS. Every runner pushes its own ref, then all of them re-read the full claim set and apply a
deterministic winner rule. Its own docstring is candid: "pushes succeed and the push no longer
decides anything." That argument holds only if every runner observes the FULL set at resolve time.
Runner A can push, read `{A}` before B's push is visible, declare itself the winner, and proceed
while B reads `{A, B}` and computes a different one. Both run the same job.

The design's answer is not a cleverer arbitration -- it is to stop arbitrating. A store that offers
an atomic compare-and-swap (an Issue assignee set only if unset; a ref created only if absent)
removes the window structurally rather than narrowing it.

So `try_claim` is specified as CAS and nothing else: **it returns False rather than resolving a
tie**. An implementation that pushes and then arbitrates does not satisfy this contract, and the
threaded test in `tests/test_store.py` is what tells the two apart -- a sequential test passes for
both designs, which is why the old defect survived so long.

ONE VERDICT VOCABULARY. `gate.py`'s PASS / FAIL / INCONCLUSIVE is the acceptance interface for
everything: the worker's definition of done, the runner's report, the lead's merge input, the
board's column driver. A store that accepted arbitrary strings would let a caller invent a fourth
state that nothing knows how to act on -- and INCONCLUSIVE is emphatically not a soft FAIL.
"""

from __future__ import annotations

import threading
from typing import Protocol, runtime_checkable

from agent_swarm.job import Job

#: The only words a verdict may take. gate.py's three values, unchanged, for both job kinds.
VERDICTS = frozenset({'PASS', 'FAIL', 'INCONCLUSIVE'})


@runtime_checkable
class Store(Protocol):
    """What the job layer needs from a backing store. Gitea, GitHub and memory all satisfy it.

    Deliberately small. Everything here is either an ATOMIC operation the layer cannot implement
    itself, or a read. Anything that can be decided from values belongs in `admission`, not here --
    a store that starts making decisions is the second scheduler this design refuses.
    """

    def try_claim(self, job: Job, *, owner: str) -> bool:
        """Atomically take ``job`` for ``owner``. ``False`` if anyone already holds it.

        MUST be a compare-and-swap. Returning ``True`` to two callers -- even transiently, even if
        a later rule would pick one -- violates this contract.
        """
        ...

    def claim_owner(self, job: Job) -> str | None:
        """Who holds ``job``, or ``None``."""
        ...

    def release(self, job: Job, *, owner: str) -> None:
        """Release ``job`` if ``owner`` holds it. A non-owner's release is a no-op, never a steal."""
        ...

    def record_verdict(self, job: Job, *, verdict: str, detail: str) -> None:
        """Record the outcome. ``verdict`` must be one of :data:`VERDICTS`."""
        ...

    def verdict(self, job: Job) -> str | None:
        """The recorded verdict, or ``None`` if the job has not been answered."""
        ...


class InMemoryStore:
    """A reference store. Real, atomic, and process-local.

    ITS PURPOSE IS THE CONTRACT, NOT PRODUCTION. It exists so `tests/test_store.py` can state what
    a store must do before any adapter exists -- an adapter written first would have defined the
    contract by whatever it happened to do, which is how the push-then-arbitrate claim became the
    de-facto specification in the first place.

    The lock is what makes `try_claim` a genuine CAS within a process, so the threaded test is
    testing a real property rather than a lucky interleaving.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._claims: dict[str, str] = {}
        self._verdicts: dict[str, tuple[str, str]] = {}

    def try_claim(self, job: Job, *, owner: str) -> bool:
        key = job.claim_key()
        with self._lock:
            if key in self._claims:
                # INCLUDING when the holder IS `owner`. A runner that lost track of its own claim
                # must not silently re-take it and reset the lease -- that is how a hung run keeps
                # a job locked forever while looking freshly claimed.
                return False
            self._claims[key] = owner
            return True

    def claim_owner(self, job: Job) -> str | None:
        with self._lock:
            return self._claims.get(job.claim_key())

    def release(self, job: Job, *, owner: str) -> None:
        key = job.claim_key()
        with self._lock:
            # OWNER-CHECKED. A stranger releasing a live claim frees it for a second runner, which
            # is the very failure the CAS was adopted to remove -- reintroduced through the back
            # door, and invisible because nothing errors.
            if self._claims.get(key) == owner:
                del self._claims[key]

    def record_verdict(self, job: Job, *, verdict: str, detail: str) -> None:
        if verdict not in VERDICTS:
            msg = f'verdict must be one of {sorted(VERDICTS)}, got {verdict!r}'
            raise ValueError(msg)
        with self._lock:
            self._verdicts[job.claim_key()] = (verdict, detail)

    def verdict(self, job: Job) -> str | None:
        with self._lock:
            found = self._verdicts.get(job.claim_key())
        return found[0] if found else None
