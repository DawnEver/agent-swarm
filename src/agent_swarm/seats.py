"""A FLEET-WIDE resource with N holders: the floating licence, and anything shaped like one.

WHAT WAS ALREADY SOLVED, AND WHY IT IS NOT THIS
===============================================

`admission.classes_conflict` plus `exclusive.lock_path_for_class` give a job class
(`vendor:femm`) a MACHINE-scoped mutex, so two FEMM jobs cannot overlap on one box. That is a
complete answer to a per-machine constraint and it is not an answer to a per-FLEET one: a floating
licence with four seats is one resource shared by every machine, and a file in `%TEMP%` cannot see
another host. Ten boxes each holding their own local lock is ten concurrent FEMM sessions against a
four-seat licence -- every lock working perfectly, the constraint violated anyway.

**THE SCOPE OF THE OLD GUARD IS THEREFORE NAMED HERE, WHERE A READER LOOKING FOR FLEET EXCLUSION
ARRIVES.** `exclusive` says "NOT A LOCK ACROSS MACHINES" in its own docstring, which is true and is
not enough: a scope claim that is correct in the file that makes it still lets someone route around
it from another file, because they never read that one.

**THE ARBITRATION IS NOT HERE.** It is `agent_swarm.claim`, at `slots=N`, the same code the job
store runs at `slots=1` -- including the proof that the generalisation from 1 to N is free, the
measured numbers and the deployment precondition. This module is the thin part: a tool name, a seat
COUNT the caller declares, and the item to arbitrate on. It was first written as a second copy of
the store's claim, and the copy is deleted rather than kept in step.

N SEATS, AND THE PACKAGE DOES NOT KNOW WHAT ANY OF THEM ARE FOR
===============================================================

`SeatCatalog.seats(tool) -> int` is CONFIGURATION THE CALLER SUPPLIES. Nothing here knows that FEMM
is a solver, that a site bought four seats, or that seats exist for licences rather than for, say, a
test rig with three physical dynamometers. A default count would be the worst version of that
ignorance: `1` silently serialises a four-seat site, and any larger number invents capacity that was
never purchased. So an undeclared tool RAISES (:class:`UnknownTool`) and the operator finds out at
the first acquire rather than at the licence audit.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from agent_swarm.claim import Arbiter, Held, Holders
from agent_swarm.forge import Forge
from agent_swarm.forge_store import ITEM_TITLE_ROOT, NOT_VISIBLE, NotVisible, Role

#: How long a seat survives without a beat. MUCH SHORTER than a job claim's three hours, and that
#: difference is the heartbeat's whole return: a seat is beaten while it is held, so the lease only
#: has to outlast a network hiccup rather than the work itself. A holder that stops beating loses
#: its seat within this, which on a four-seat licence is the difference between a crashed run
#: costing the fleet five minutes and costing it an afternoon.
DEFAULT_SEAT_LEASE_SECONDS = 300.0

_SEAT_SEGMENT = 'seat'


class UnknownTool(LookupError):
    """`seats()` was asked about a tool nobody declared.

    RAISED RATHER THAN DEFAULTED, and the alternatives are both worse in ways that do not show up
    until it matters. Defaulting to 1 serialises a site that paid for four. Defaulting to a larger
    number invents capacity, and the failure is a licence-server refusal in the middle of a
    25-minute run -- attributed to the solver, not to the scheduler that over-admitted.
    """


@runtime_checkable
class SeatCatalog(Protocol):
    """How many seats a tool has. The one thing this module must be told and can never derive."""

    def seats(self, tool: str) -> int:
        """Seats for `tool`. Raises :class:`UnknownTool` if it is not declared."""
        ...


@dataclass(frozen=True, slots=True)
class DeclaredSeats:
    """A seat catalogue from a plain mapping -- the shape a TOML file deserialises into.

    VALIDATED AT CONSTRUCTION, not at the acquire, which is the same ruling `Arbiter` makes about
    its slot count: a zero or negative number makes every acquire refuse, and a fleet where nothing
    can take a seat reads exactly like a licence that is fully booked. The operator would look at
    the licence server.
    """

    counts: Mapping[str, int]

    def __post_init__(self) -> None:
        for tool, count in self.counts.items():
            if not tool:
                msg = 'a seat count must name its tool; an empty name is not a tool'
                raise ValueError(msg)
            if count <= 0:
                msg = (
                    f'{tool!r} declares {count} seats. A non-positive count refuses every acquire, '
                    f'which is indistinguishable from a fully-booked licence -- remove the tool '
                    f'instead, or state the seats it really has.'
                )
                raise ValueError(msg)

    def seats(self, tool: str) -> int:
        try:
            return self.counts[tool]
        except KeyError as exc:
            msg = (
                f'no seat count is declared for {tool!r}; declared tools are '
                f'{sorted(self.counts)}. This package cannot guess one -- a default would either '
                f'serialise a multi-seat licence or invent capacity nobody bought.'
            )
            raise UnknownTool(msg) from exc


def seat_item_title(namespace: str, tool: str) -> str:
    """The ONE spelling of a seat pool's work item.

    Here rather than at the two call sites -- provisioning and lookup -- for the reason
    `ForgeStore._item_title` gives: two spellings of one identity are two things free to drift, and
    the symptom of drift is a pool that provisions one item and arbitrates on another, i.e. every
    seat granted twice.
    """
    return f'{ITEM_TITLE_ROOT} {namespace}/{_SEAT_SEGMENT}/{tool}'


def provision_seat_item(forge: Forge, *, namespace: str, tool: str, role: Role) -> int:
    """Create the work item a tool's seats are arbitrated on. THE SUBMITTER'S JOB, exactly once.

    THE SAME STRUCTURAL FIX AS `ForgeStore.register`, for the same race. If runners created the item
    on demand, sixteen of them would read an empty list, create sixteen items, and each would
    arbitrate a full set of N seats on its own -- 16N seats granted against a licence with N,
    with the protocol working flawlessly on each of sixteen wrong items. Convergence by re-reading
    does not fix it: on GitHub the list was measured stale 22/22, and it did not return even the
    reader's own just-created issue.

    Raises:
        PermissionError: a runner may not create. Not a convention -- the role is checked.
    """
    if role is Role.RUNNER:
        msg = (
            f'a {Role.RUNNER.value} may not provision the seat item for {tool!r}: concurrent '
            f'creation is how N seats become 16N. The submitter creates it once.'
        )
        raise PermissionError(msg)
    return forge.create_work_item(
        title=seat_item_title(namespace, tool),
        body=f'Seat pool for `{tool}`. Each live comment is one holder.',
        labels=(),
    )


def find_seat_item(forge: Forge, *, namespace: str, tool: str) -> int | NotVisible:
    """The seat item's number, or :data:`~agent_swarm.forge_store.NOT_VISIBLE`.

    NEVER ``None``, and that is `forge_store.NotVisible`'s whole reason for existing: every caller
    that saw a ``None`` did the natural thing and created one. Here that would be the 16N failure
    above, reached through the branch a reader writes without thinking about it.
    """
    title = seat_item_title(namespace, tool)
    numbers = [item.number for item in forge.list_work_items(state='open') if item.title == title]
    return min(numbers) if numbers else NOT_VISIBLE


class SeatPool:
    """N concurrent holders of one fleet-wide resource. A tool name and a count over `Arbiter`.

    THIN ON PURPOSE, AND THE THINNESS IS THE DESIGN. Everything that decides who holds is in
    `agent_swarm.claim`; what is left here is the only thing the core must not know -- WHAT the
    resource is and HOW MANY of it a site bought. If this class ever grows a second copy of a
    post-read-arbitrate, that is the defect returning, not a specialisation.

    Args:
        forge: storage. Passed straight to the arbiter.
        tool: what the seats are for. AN OPAQUE TOKEN: it names an item and keys a catalogue lookup,
            and nothing here interprets it.
        item_number: the work item the seats live on, from `provision_seat_item` or
            `find_seat_item`. REQUIRED, and not resolved lazily on first use: a lazy resolve is the
            branch where a runner that cannot see the item creates one.
        catalog: how many seats `tool` has. Configuration, supplied by the caller.
        lease_seconds: how long a seat survives without a beat.
    """

    def __init__(
        self,
        forge: Forge,
        *,
        tool: str,
        item_number: int,
        catalog: SeatCatalog,
        lease_seconds: float = DEFAULT_SEAT_LEASE_SECONDS,
    ) -> None:
        if not tool:
            msg = 'a seat pool must name its tool; an empty name would pool unrelated resources'
            raise ValueError(msg)
        self.forge = forge
        self.tool = tool
        self.item_number = item_number
        self.catalog = catalog
        self.lease_seconds = lease_seconds

    def acquire(self, *, owner: str) -> Held | None:
        """Take a seat, or ``None`` because this instant there was none.

        **``None`` MEANS "NOT NOW", NEVER "THIS TOOL HAS NO SEATS".** The two are distinguishable at
        the call site because the second one RAISES: an undeclared tool is `UnknownTool` and a
        backend that cannot read its own write is `claim.ArbitrationUnsound`. So a `None` is always
        the ordinary, retryable answer, and no caller has to guess which situation it is in.

        THE SEAT COUNT IS READ ON EVERY ACQUIRE, not cached at construction. A site that buys two
        more seats edits its configuration; a pool that had cached the old number would go on
        rationing to the old count until every process on the fleet restarted, and nothing would say
        why.

        Raises:
            UnknownTool: nobody declared how many seats `tool` has.
        """
        return self._arbiter().take(owner=owner)

    def holders(self, *, now: float | None = None) -> Holders:
        """Who holds a seat, as far as one read can tell. Read :class:`~agent_swarm.claim.Holders`
        before deriving anything from it -- there is deliberately no free-seat count.

        FOR A STATUS DISPLAY, NOT FOR A DECISION. The decision is `acquire`, which is the only
        operation whose answer the server arbitrates rather than this process computing it.
        """
        return self._arbiter().holders(now=now)

    def _arbiter(self) -> Arbiter:
        return Arbiter(
            self.forge,
            item_number=self.item_number,
            slots=self.catalog.seats(self.tool),
            lease_seconds=self.lease_seconds,
        )
