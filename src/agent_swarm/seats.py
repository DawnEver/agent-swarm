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

import contextlib
from collections.abc import Callable, Generator, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from agent_swarm.admission import VENDOR_PREFIX
from agent_swarm.claim import Arbiter, ArbitrationUnsound, Beater, Held, Holders, LeaseLost, beat_interval
from agent_swarm.forge import Forge, ForgeError
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

    def is_unlimited(self, tool: str) -> bool:
        """Whether this tool has no fleet-wide limit. DECLARED, never inferred from silence."""
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

    #: Tools with NO fleet-wide limit, DECLARED rather than inferred from absence.
    #:
    #: WHY ABSENCE CANNOT MEAN THIS. Without the list, a tool nobody declared is indistinguishable
    #: from a tool deliberately exempted, and the two failures are wildly asymmetric: an omission
    #: read as "unlimited" over-subscribes a floating licence and is discovered at an audit, or in
    #: the middle of a 25-minute solve, attributed to the solver. So `seats()` still RAISES for
    #: anything in neither list, and a site states its unbounded tools on purpose.
    #:
    #: THIS ARRIVED FROM A CONSUMER, 2026-08-12. motronics wrapped this class to add exactly this,
    #: because a perpetual local FEMM install has no fleet limit while JMAG is floating. A catalogue
    #: that can only express a COUNT forces every such site to write that wrapper -- which is the
    #: shape this package keeps deleting, one repository further out.
    unlimited: Sequence[str] = ()

    def __post_init__(self) -> None:
        both = sorted(set(self.counts) & set(self.unlimited))
        if both:
            msg = (
                f'{both} are declared BOTH with a seat count and as unlimited. One is wrong and '
                f'nothing here can tell which, so neither is assumed: read as unlimited when it has '
                f'a count, the licence is over-subscribed; the reverse serialises a fleet for no '
                f'reason.'
            )
            raise ValueError(msg)
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

    def is_unlimited(self, tool: str) -> bool:
        """Whether this tool was DECLARED to have no fleet-wide limit. Silence is not a declaration."""
        return tool in self.unlimited

    def seats(self, tool: str) -> int:
        if tool in self.unlimited:
            msg = (
                f'{tool!r} is declared unlimited, so it has no seat count. Asking for one means a '
                f'caller skipped the `is_unlimited` branch, which would ration a tool this site '
                f'declared unbounded.'
            )
            raise UnknownTool(msg)
        try:
            return self.counts[tool]
        except KeyError as exc:
            msg = (
                f'no seat count is declared for {tool!r}; declared tools are '
                f'{sorted(self.counts)} and unlimited tools are {sorted(self.unlimited)}. This '
                f'package cannot guess one -- a default would either serialise a multi-seat licence '
                f'or invent capacity nobody bought. Declare a count, or declare it unlimited.'
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


# ==================================================================================================
# HOLDING ONE, FOR THE DURATION OF SOME WORK
# ==================================================================================================
#
# WHY THIS IS HERE AND NOT IN EACH CONSUMER, which is where it was until 2026-08-12. `SeatPool`
# gives a caller `acquire`, and every caller then had to write the same decisions itself: derive a
# tool from a job class, skip the whole thing for an unbounded tool, keep the lease alive for work
# far longer than it, tell "no seat right now" from "this tool is declared nowhere" from "the forge
# is down", release on the way out even when the work raised, and report which of those happened.
#
# motronics wrote all of it -- and MEASURED, the file was 359 lines of which exactly 2 named that
# project. It was classified as that project's code on the strength of those 2. The classification
# was the defect and the file was the evidence; what stayed behind is the seat COUNTS, the namespace
# and the unreachable-forge DECISION, which is genuinely a site's to make.

#: What a seat hold can report. FIVE VALUES rather than a boolean, because a caller acting on this
#: needs to tell "there was nothing to arbitrate" from "there was, and we could not".
SEATS_NOT_APPLICABLE = 'n/a'  # the work names no seated tool; no fleet resource is involved
SEATS_UNLIMITED = 'unlimited'  # a tool DECLARED to have no fleet-wide limit
SEATS_HELD = 'held'  # arbitrated, and the lease was still ours at the end
SEATS_UNARBITRATED = 'unarbitrated'  # the forge could not be reached, and the caller chose PROCEED
SEATS_LOST = 'lost'  # taken, then the lease lapsed mid-work -- the resource was over-committed

#: The two answers to "the forge is unreachable". THE CALLER CHOOSES, and there is no default.
#:
#: A DEFAULT HERE WOULD BE THIS PACKAGE DECIDING SOMEBODY'S LICENCE POLICY. `REFUSE` is right for a
#: site whose licence server audits; `PROCEED` for one that would rather not stop local work during
#: an outage -- and both are defensible, which is exactly why neither may be assumed.
REFUSE_WHEN_UNREACHABLE = 'refuse'
PROCEED_WHEN_UNREACHABLE = 'proceed'


class SeatRefused(RuntimeError):
    """This work may not start: the seats are taken, undeclared, or unreachable-and-refused.

    A REFUSAL, NOT A FAILURE. Nothing about the work has been measured, and a caller that records
    this as a failure puts an infrastructure state on the work's record.
    """


def tool_of(job_class: str) -> str | None:
    """The tool a job class names, or None when the class names no seated tool.

    DERIVED FROM `admission.VENDOR_PREFIX` rather than from a second list of tool names. A list here
    would be another spelling of the vocabulary `admission` already owns, and the drift symptom is a
    tool that takes a machine lock and no seat -- which looks exactly like correct behaviour.
    """
    return job_class[len(VENDOR_PREFIX) :] if job_class.startswith(VENDOR_PREFIX) else None


@contextlib.contextmanager
def hold_for_class(
    job_class: str,
    *,
    owner: str,
    namespace: str,
    catalog: SeatCatalog,
    forge: Forge | Callable[[], Forge],
    when_unreachable: str,
    lease_seconds: float = DEFAULT_SEAT_LEASE_SECONDS,
) -> Generator[Callable[[], str]]:
    """Hold a fleet seat for the duration of a block. Yields a callable giving the seat STATE.

    THE STATE IS A CALLABLE, not a value, and that is not ceremony: the interesting transition --
    the lease lapsing -- happens DURING the body, so a value captured at entry would report `held`
    for work that lost its seat ten minutes in. The caller reads it after the body, which is the
    only moment at which the answer is complete.

    `forge` may be a FACTORY, so a caller whose forge construction can itself fail -- reading a
    declared repository, say -- is not forced to build one before finding out whether it needs one.
    An unbounded tool must not pay a round trip, or a config read, for its exemption.

    Args:
        job_class: e.g. `vendor:jmag`. Anything naming no seated tool yields `n/a` immediately.
        when_unreachable: `REFUSE_WHEN_UNREACHABLE` or `PROCEED_WHEN_UNREACHABLE`. NO DEFAULT.

    Raises:
        SeatRefused: no seat now, the tool is declared nowhere, no pool exists, the arbitration is
            unsound, or the forge is unreachable and the caller chose to refuse.
    """
    if when_unreachable not in (REFUSE_WHEN_UNREACHABLE, PROCEED_WHEN_UNREACHABLE):
        msg = (
            f'{when_unreachable!r} is not an unreachable-forge policy; expected '
            f'{REFUSE_WHEN_UNREACHABLE!r} or {PROCEED_WHEN_UNREACHABLE!r}. There is no default '
            f'because both are defensible and the choice belongs to whoever owns the licence.'
        )
        raise ValueError(msg)

    tool = tool_of(job_class)
    if tool is None:
        yield lambda: SEATS_NOT_APPLICABLE
        return

    if catalog.is_unlimited(tool):
        # NO FORGE CONTACT AT ALL on this path, and it is the property worth stating: declaring a
        # tool unlimited must not make local work depend on a remote being up. An exemption that
        # cost a round trip would be a declaration meant to REMOVE a constraint that added one.
        yield lambda: SEATS_UNLIMITED
        return

    try:
        seats_for_tool = catalog.seats(tool)
    except UnknownTool as exc:
        raise SeatRefused(str(exc)) from exc

    def _unreachable(exc: Exception):
        if when_unreachable == REFUSE_WHEN_UNREACHABLE:
            msg = f'the forge is unreachable, so {tool!r} seats cannot be arbitrated: {exc}'
            raise SeatRefused(msg) from exc
        return lambda: SEATS_UNARBITRATED

    try:
        resolved = forge() if callable(forge) else forge
        item = find_seat_item(resolved, namespace=namespace, tool=tool)
    except (ForgeError, OSError) as exc:
        yield _unreachable(exc)
        return

    if item is NOT_VISIBLE:
        msg = (
            f'no seat pool exists for {tool!r} in {namespace!r}. A runner may not create one -- '
            f'concurrent creation is how {seats_for_tool} seats become {seats_for_tool} PER RUNNER '
            f'-- so the submitter provisions it once, with `provision_seat_item`.'
        )
        raise SeatRefused(msg)

    pool = SeatPool(resolved, tool=tool, item_number=item, catalog=catalog, lease_seconds=lease_seconds)
    try:
        held = pool.acquire(owner=owner)
    except ArbitrationUnsound as exc:
        # NOT AN OUTAGE, AND IT MUST NOT BE FILED AS ONE. This means the backend did not return its
        # own completed write -- the precondition the whole protocol rests on. A fleet arbitrating
        # on a store like that grants one seat to several holders, each of them certain. Reported as
        # an outage, an operator looks at the network and the double-grant goes on.
        msg = (
            f'the forge did not return its own write while arbitrating {tool!r} seats: {exc}. This '
            f'is not an outage -- it is the precondition the protocol requires.'
        )
        raise SeatRefused(msg) from exc
    except (ForgeError, OSError) as exc:
        yield _unreachable(exc)
        return

    if held is None:
        holders = pool.holders()
        msg = (
            f'all {seats_for_tool} fleet seat(s) for {tool!r} are held right now '
            f'({holders.occupied} visible holder(s): {sorted(c.owner for c in holders)}). This is '
            f'"not now", not "never" -- a seat frees within {lease_seconds:.0f}s of a holder stopping.'
        )
        raise SeatRefused(msg)

    # THE CADENCE COMES FROM `beat_interval`, NEVER FROM A NUMBER CHOSEN HERE. The consumer this was
    # lifted out of derived `lease / 8` of its own, which is the second-spelling defect that
    # function's docstring exists to forbid -- written by someone who had read it.
    with Beater(held.renew, interval=beat_interval(lease_seconds)) as beater:
        try:
            yield lambda: SEATS_LOST if beater.lost else SEATS_HELD
        finally:
            # OWNER-CHECKED AND IDEMPOTENT in the claim layer, and a no-op if the lease was already
            # lost -- so giving back a seat we no longer hold cannot evict whoever got it next.
            with contextlib.suppress(LeaseLost, ForgeError, OSError):
                held.release()
